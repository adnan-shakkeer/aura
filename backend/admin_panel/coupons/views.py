from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from admin_panel.admin_auth.decorators import admin_required

from .models import Coupon, normalize_coupon_code


PAGE_SIZE = 10
COUPON_TIME_ZONE_NAME = "Asia/Kolkata"
COUPON_TIME_ZONE = ZoneInfo(COUPON_TIME_ZONE_NAME)
COUPON_TIME_ZONE_LABEL = "IST (Asia/Kolkata)"


def _parse_decimal(raw, *, field, errors, allow_blank=False):
    raw = (raw or "").strip()
    if not raw:
        if allow_blank:
            return None
        errors[field] = "This field is required."
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        errors[field] = "Please enter a valid amount."
        return None


def _parse_datetime(raw, *, field, errors):
    raw = (raw or "").strip()
    if not raw:
        errors[field] = "This field is required."
        return None

    value = parse_datetime(raw)
    if value is None:
        errors[field] = "Please enter a valid date and time."
        return None

    if timezone.is_naive(value):
        value = timezone.make_aware(value, COUPON_TIME_ZONE)
    else:
        value = value.astimezone(COUPON_TIME_ZONE)

    return value.astimezone(dt_timezone.utc)


def _parse_optional_positive_int(raw, *, field, errors):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        errors[field] = "Please enter a whole number."
        return None
    if value < 1:
        errors[field] = "This value must be at least 1."
        return None
    return value


def _form_values(coupon=None):
    if coupon is None:
        return {
            "code": "",
            "description": "",
            "discount_type": Coupon.DiscountType.PERCENTAGE,
            "discount_value": "",
            "minimum_purchase": "0.00",
            "max_discount_amount": "",
            "valid_from": "",
            "valid_until": "",
            "total_usage_limit": "",
            "per_user_limit": "1",
            "is_active": True,
        }

    return {
        "code": coupon.code,
        "description": coupon.description,
        "discount_type": coupon.discount_type,
        "discount_value": str(coupon.discount_value),
        "minimum_purchase": str(coupon.minimum_purchase),
        "max_discount_amount": (
            ""
            if coupon.max_discount_amount is None
            else str(coupon.max_discount_amount)
        ),
        "valid_from": coupon.valid_from,
        "valid_until": coupon.valid_until,
        "total_usage_limit": (
            ""
            if coupon.total_usage_limit is None
            else coupon.total_usage_limit
        ),
        "per_user_limit": (
            ""
            if coupon.per_user_limit is None
            else coupon.per_user_limit
        ),
        "is_active": coupon.is_active,
    }


def _coupon_from_request(request, *, coupon=None):
    errors = {}
    data = {
        "code": normalize_coupon_code(request.POST.get("code", "")),
        "description": (
            request.POST.get("description", "").strip()
            if "description" in request.POST
            else (coupon.description if coupon and coupon.pk else "")
        ),
        "discount_type": request.POST.get("discount_type", "").strip(),
        "discount_value": _parse_decimal(
            request.POST.get("discount_value"),
            field="discount_value",
            errors=errors,
        ),
        "minimum_purchase": _parse_decimal(
            request.POST.get("minimum_purchase"),
            field="minimum_purchase",
            errors=errors,
        ),
        "max_discount_amount": _parse_decimal(
            request.POST.get("max_discount_amount"),
            field="max_discount_amount",
            errors=errors,
            allow_blank=True,
        ),
        "valid_from": _parse_datetime(
            request.POST.get("valid_from"),
            field="valid_from",
            errors=errors,
        ),
        "valid_until": _parse_datetime(
            request.POST.get("valid_until"),
            field="valid_until",
            errors=errors,
        ),
        "total_usage_limit": _parse_optional_positive_int(
            request.POST.get("total_usage_limit"),
            field="total_usage_limit",
            errors=errors,
        ),
        "per_user_limit": _parse_optional_positive_int(
            request.POST.get("per_user_limit"),
            field="per_user_limit",
            errors=errors,
        ),
        "is_active": request.POST.get("is_active") in (
            "on",
            "true",
            "True",
            "1",
        ),
    }

    if not data["code"]:
        errors["code"] = "Coupon code is required."
    elif Coupon.objects.filter(code__iexact=data["code"]).exclude(
        pk=getattr(coupon, "pk", None)
    ).exists():
        errors["code"] = "A coupon with this code already exists."

    if data["discount_type"] not in Coupon.DiscountType.values:
        errors["discount_type"] = "Please select a valid discount type."

    if data["discount_type"] == Coupon.DiscountType.FIXED:
        data["max_discount_amount"] = None

    if coupon is None:
        coupon = Coupon()

    for field_name, value in data.items():
        setattr(coupon, field_name, value)

    if not errors:
        try:
            coupon.full_clean()
        except ValidationError as exc:
            errors.update(exc.message_dict)

    errors = {
        key: (value[0] if isinstance(value, (list, tuple)) else value)
        for key, value in errors.items()
    }
    return coupon, errors, data


def _coupon_list_queryset(request):
    now = timezone.now()
    queryset = Coupon.objects.filter(is_deleted=False).annotate(
        total_usage=Count(
            "usages",
            filter=Q(usages__status="CONSUMED"),
            distinct=True,
        )
    )

    search_query = request.GET.get("search", "").strip()
    if search_query:
        queryset = queryset.filter(code__icontains=search_query)

    status_filter = request.GET.get("status", "all").strip().lower()
    if status_filter == "active":
        queryset = queryset.filter(
            is_active=True,
            valid_from__lte=now,
            valid_until__gte=now,
        )
    elif status_filter == "expired":
        queryset = queryset.filter(valid_until__lt=now)
    elif status_filter == "scheduled":
        queryset = queryset.filter(
            is_active=True,
            valid_from__gt=now,
            valid_until__gte=now,
        )
    elif status_filter == "inactive":
        queryset = queryset.filter(
            is_active=False,
            valid_until__gte=now,
        )

    sort_by = request.GET.get("sort", "newest").strip().lower()
    sort_options = {
        "newest": "-created_at",
        "oldest": "created_at",
        "code-asc": "code",
        "code-desc": "-code",
        "expiry": "valid_until",
        "usage-high": "-total_usage",
        "usage-low": "total_usage",
    }
    queryset = queryset.order_by(sort_options.get(sort_by, "-created_at"))

    return queryset, search_query, status_filter, sort_by


@admin_required
@never_cache
def coupon_list(request):
    queryset, search_query, status_filter, sort_by = _coupon_list_queryset(
        request
    )
    paginator = Paginator(queryset, PAGE_SIZE)
    try:
        coupons = paginator.page(request.GET.get("page", 1))
    except (PageNotAnInteger, EmptyPage):
        coupons = paginator.page(1)

    return render(
        request,
        "admin_panel/coupons/coupon_list.html",
        {
            "coupons": coupons,
            "search_query": search_query,
            "status_filter": status_filter,
            "sort_by": sort_by,
            "total_count": paginator.count,
            "coupon_timezone": COUPON_TIME_ZONE_NAME,
            "coupon_timezone_label": COUPON_TIME_ZONE_LABEL,
        },
    )


@admin_required
@never_cache
def coupon_create(request):
    if request.method == "POST":
        coupon, errors, values = _coupon_from_request(request)
        if not errors:
            coupon.save()
            messages.success(
                request,
                f"Coupon '{coupon.code}' created successfully!",
            )
            return redirect("coupons:list")
    else:
        errors = {}
        values = _form_values(None)

    return render(
        request,
        "admin_panel/coupons/coupon_form.html",
        {
            "mode": "create",
            "errors": errors,
            "values": values,
            "discount_types": Coupon.DiscountType.choices,
            "coupon_timezone": COUPON_TIME_ZONE_NAME,
            "coupon_timezone_label": COUPON_TIME_ZONE_LABEL,
        },
    )


@admin_required
@never_cache
def coupon_edit(request, coupon_id):
    coupon = get_object_or_404(
        Coupon,
        pk=coupon_id,
        is_deleted=False,
    )

    if request.method == "POST":
        updated, errors, values = _coupon_from_request(
            request,
            coupon=coupon,
        )
        if not errors:
            updated.save()
            messages.success(
                request,
                f"Coupon '{updated.code}' updated successfully!",
            )
            return redirect("coupons:list")
    else:
        errors = {}
        values = _form_values(coupon)

    return render(
        request,
        "admin_panel/coupons/coupon_form.html",
        {
            "mode": "edit",
            "coupon": coupon,
            "errors": errors,
            "values": values,
            "discount_types": Coupon.DiscountType.choices,
            "coupon_timezone": COUPON_TIME_ZONE_NAME,
            "coupon_timezone_label": COUPON_TIME_ZONE_LABEL,
        },
    )


@admin_required
@never_cache
@require_POST
def coupon_toggle(request, coupon_id):
    coupon = get_object_or_404(
        Coupon,
        pk=coupon_id,
        is_deleted=False,
    )
    coupon.is_active = not coupon.is_active
    coupon.save(update_fields=["is_active", "updated_at"])

    if coupon.is_active:
        if coupon.valid_from > timezone.now():
            local_start = coupon.valid_from.astimezone(COUPON_TIME_ZONE)
            messages.success(
                request,
                (
                    f"Coupon '{coupon.code}' enabled. It will become "
                    f"available on {local_start:%d %b %Y, %I:%M %p} IST."
                ),
            )
        else:
            messages.success(
                request,
                f"Coupon '{coupon.code}' activated.",
            )
    else:
        messages.success(
            request,
            f"Coupon '{coupon.code}' deactivated.",
        )

    return redirect("coupons:list")


@admin_required
@never_cache
@require_POST
def coupon_delete(request, coupon_id):
    coupon = get_object_or_404(
        Coupon,
        pk=coupon_id,
        is_deleted=False,
    )
    coupon.is_deleted = True
    coupon.is_active = False
    coupon.save(update_fields=["is_deleted", "is_active", "updated_at"])
    messages.success(
        request,
        f"Coupon '{coupon.code}' deleted successfully!",
    )
    return redirect("coupons:list")
