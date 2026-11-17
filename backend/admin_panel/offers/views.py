from http import HTTPStatus

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from admin_panel.admin_auth.decorators import admin_required
from admin_panel.categories.models import Category
from admin_panel.offer_utils import parse_offer_percentage
from admin_panel.products.models import Product

from .models import (
    OFFER_TIME_ZONE_LABEL,
    Offer,
    offer_today,
)


PAGE_SIZE = 10


def _parse_positive_int(raw, *, field, errors, required=True):
    text = (raw or "").strip()

    if not text:
        if required:
            errors[field] = "This field is required."
        return None

    try:
        value = int(text)
    except (TypeError, ValueError):
        errors[field] = "Please enter a valid selection."
        return None

    if value < 1:
        errors[field] = "Please enter a valid selection."
        return None

    return value


def _parse_date(raw, *, field, errors):
    text = (raw or "").strip()

    if not text:
        errors[field] = "This field is required."
        return None

    value = parse_date(text)
    if value is None:
        errors[field] = "Please enter a valid date."
        return None

    return value


def _flatten_validation_errors(exc):
    return {
        field: (value[0] if isinstance(value, (list, tuple)) else value)
        for field, value in exc.message_dict.items()
    }


def _offer_from_request(request, *, offer=None):
    errors = {}
    data = {
        "name": request.POST.get("name", "").strip(),
        "target_type": request.POST.get("target_type", "").strip().upper(),
        "percentage": None,
        "valid_from": _parse_date(
            request.POST.get("valid_from"),
            field="valid_from",
            errors=errors,
        ),
        "valid_until": _parse_date(
            request.POST.get("valid_until"),
            field="valid_until",
            errors=errors,
        ),
        "is_active": request.POST.get("is_active") in (
            "on",
            "true",
            "True",
            "1",
        ),
    }

    if not data["name"]:
        errors["name"] = "Offer name is required."
    elif len(data["name"]) > 150:
        errors["name"] = "Offer name cannot exceed 150 characters."

    product = None
    category = None

    if data["target_type"] not in Offer.TargetType.values:
        errors["target_type"] = "Please select a valid offer target type."
    elif data["target_type"] == Offer.TargetType.PRODUCT:
        product_id = _parse_positive_int(
            request.POST.get("product"),
            field="product",
            errors=errors,
        )
        if product_id is not None:
            product = Product.objects.filter(
                pk=product_id,
                is_deleted=False,
            ).first()
            if product is None:
                errors["product"] = "Please select a valid product."
    elif data["target_type"] == Offer.TargetType.CATEGORY:
        category_id = _parse_positive_int(
            request.POST.get("category"),
            field="category",
            errors=errors,
        )
        if category_id is not None:
            category = Category.objects.filter(
                pk=category_id,
                is_deleted=False,
            ).first()
            if category is None:
                errors["category"] = "Please select a valid category."

    raw_percentage = request.POST.get("percentage", "").strip()
    if not raw_percentage:
        errors["percentage"] = "This field is required."
        percentage = None
    else:
        percentage, percentage_error = parse_offer_percentage(
            raw_percentage,
            allow_zero=False,
        )
        if percentage_error:
            errors["percentage"] = percentage_error
    data["percentage"] = percentage

    if offer is None:
        offer = Offer()

    for field_name, value in data.items():
        setattr(offer, field_name, value)

    offer.product = product
    offer.category = category

    if not errors:
        try:
            offer.full_clean()
        except ValidationError as exc:
            errors.update(_flatten_validation_errors(exc))

    return offer, errors


def _offer_list_queryset(request):
    today = offer_today()
    queryset = Offer.objects.filter(is_deleted=False).select_related(
        "product",
        "product__brand",
        "category",
    )

    search_query = request.GET.get("search", "").strip()
    if search_query:
        queryset = queryset.filter(
            Q(name__icontains=search_query)
            | Q(product__name__icontains=search_query)
            | Q(category__name__icontains=search_query)
        )

    target_filter = request.GET.get("target", "all").strip().lower()
    if target_filter not in {"all", "product", "category"}:
        target_filter = "all"
    if target_filter == "product":
        queryset = queryset.filter(target_type=Offer.TargetType.PRODUCT)
    elif target_filter == "category":
        queryset = queryset.filter(target_type=Offer.TargetType.CATEGORY)

    status_filter = request.GET.get("status", "all").strip().lower()
    if status_filter not in {
        "all",
        "active",
        "scheduled",
        "expired",
        "inactive",
    }:
        status_filter = "all"
    if status_filter == "active":
        queryset = queryset.filter(
            is_active=True,
            valid_from__lte=today,
            valid_until__gte=today,
        )
    elif status_filter == "scheduled":
        queryset = queryset.filter(
            is_active=True,
            valid_from__gt=today,
        )
    elif status_filter == "expired":
        queryset = queryset.filter(
            is_active=True,
            valid_until__lt=today,
        )
    elif status_filter == "inactive":
        queryset = queryset.filter(is_active=False)

    sort_by = request.GET.get("sort", "newest").strip().lower()
    sort_options = {
        "newest": "-created_at",
        "oldest": "created_at",
        "name-asc": "name",
        "name-desc": "-name",
        "percentage-high": "-percentage",
        "percentage-low": "percentage",
        "expiry": "valid_until",
    }
    if sort_by not in sort_options:
        sort_by = "newest"
    queryset = queryset.order_by(
        sort_options[sort_by],
        "-id",
    )

    return queryset, search_query, status_filter, target_filter, sort_by


@admin_required
@never_cache
def offer_list(request):
    (
        queryset,
        search_query,
        status_filter,
        target_filter,
        sort_by,
    ) = _offer_list_queryset(request)

    paginator = Paginator(queryset, PAGE_SIZE)
    try:
        offers = paginator.page(request.GET.get("page", 1))
    except (PageNotAnInteger, EmptyPage):
        offers = paginator.page(1)

    products = (
        Product.objects.filter(is_deleted=False)
        .select_related("brand")
        .order_by("name", "id")
    )
    categories = (
        Category.objects.filter(is_deleted=False).order_by("name", "id")
    )

    return render(
        request,
        "admin_panel/offers/offer_list.html",
        {
            "offers": offers,
            "products": products,
            "categories": categories,
            "search_query": search_query,
            "status_filter": status_filter,
            "target_filter": target_filter,
            "sort_by": sort_by,
            "total_count": paginator.count,
            "offer_timezone_label": OFFER_TIME_ZONE_LABEL,
        },
    )


@admin_required
@never_cache
@require_POST
def offer_create(request):
    offer, errors = _offer_from_request(request)

    if errors:
        return JsonResponse(
            {"success": False, "errors": errors},
            status=HTTPStatus.BAD_REQUEST,
        )

    offer.save()
    messages.success(
        request,
        f"Offer '{offer.name}' created successfully!",
    )
    return JsonResponse(
        {
            "success": True,
            "message": f"Offer '{offer.name}' created successfully!",
        },
        status=HTTPStatus.CREATED,
    )


@admin_required
@never_cache
@require_POST
def offer_edit(request, offer_id):
    offer = get_object_or_404(
        Offer,
        pk=offer_id,
        is_deleted=False,
    )
    offer, errors = _offer_from_request(request, offer=offer)

    if errors:
        return JsonResponse(
            {"success": False, "errors": errors},
            status=HTTPStatus.BAD_REQUEST,
        )

    offer.save()
    messages.success(
        request,
        f"Offer '{offer.name}' updated successfully!",
    )
    return JsonResponse(
        {
            "success": True,
            "message": f"Offer '{offer.name}' updated successfully!",
        },
        status=HTTPStatus.OK,
    )


@admin_required
@never_cache
@require_POST
def offer_toggle(request, offer_id):
    offer = get_object_or_404(
        Offer,
        pk=offer_id,
        is_deleted=False,
    )
    offer.is_active = not offer.is_active
    offer.save(update_fields=["is_active", "updated_at"])

    if offer.is_active:
        messages.success(request, f"Offer '{offer.name}' activated.")
    else:
        messages.success(request, f"Offer '{offer.name}' deactivated.")

    return redirect("offers:list")


@admin_required
@never_cache
@require_POST
def offer_delete(request, offer_id):
    offer = get_object_or_404(
        Offer,
        pk=offer_id,
        is_deleted=False,
    )
    offer.is_deleted = True
    offer.is_active = False
    offer.save(update_fields=["is_deleted", "is_active", "updated_at"])

    messages.success(
        request,
        f"Offer '{offer.name}' deleted successfully!",
    )
    return redirect("offers:list")
