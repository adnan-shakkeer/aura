"""Admin Returns Management views."""

from django.contrib import messages
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from admin_panel.admin_auth.decorators import admin_required
from user_panel.orders.exceptions import (
    AdminPermissionError,
    ReturnAlreadyDecidedError,
    ReturnDecisionNoteRequiredError,
    ReturnIntegrityError,
    ReturnNotAllowedError,
    ReturnNotFoundError,
    ReturnQuantityMismatchError,
)
from user_panel.orders.models import ReturnItem
from user_panel.orders.returns import approve_return, reject_return


@admin_required
@never_cache
def return_list(request):
    """Admin Returns Listing — one row per ReturnItem."""

    # ── 1. Query Parameters ───────────────────────────────────────────────────
    search_query = request.GET.get("search", "").strip()
    status_filter = request.GET.get("status", "all").strip().lower()

    # ── 2. KPI counts via a single conditional-aggregation query ─────────────
    kpis = ReturnItem.objects.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=ReturnItem.Status.REQUESTED)),
        approved=Count("id", filter=Q(status=ReturnItem.Status.APPROVED)),
        rejected=Count("id", filter=Q(status=ReturnItem.Status.REJECTED)),
    )

    # ── 3. Base queryset with all needed relations ────────────────────────────
    qs = (
        ReturnItem.objects.select_related(
            "return_request",
            "order_item__order__user",
            "order_item__variant__product__brand",
            "decided_by",
        )
        .prefetch_related("order_item__variant__images")
        .all()
    )

    # ── 4. Status filter ──────────────────────────────────────────────────────
    _STATUS_MAP = {
        "pending": ReturnItem.Status.REQUESTED,
        "approved": ReturnItem.Status.APPROVED,
        "rejected": ReturnItem.Status.REJECTED,
    }
    if status_filter in _STATUS_MAP:
        qs = qs.filter(status=_STATUS_MAP[status_filter])

    # ── 5. Search ─────────────────────────────────────────────────────────────
    if search_query:
        qs = qs.filter(
            Q(return_request__request_number__icontains=search_query)
            | Q(order_item__order__order_number__icontains=search_query)
            | Q(order_item__product_name__icontains=search_query)
            | Q(order_item__order__delivery_full_name__icontains=search_query)
            | Q(order_item__order__user__email__icontains=search_query)
        ).distinct()

    # ── 6. Ordering: REQUESTED first, then by most-recent requested_at ────────
    qs = qs.annotate(
        status_order=Case(
            When(status=ReturnItem.Status.REQUESTED, then=Value(0)),
            When(status=ReturnItem.Status.APPROVED, then=Value(1)),
            When(status=ReturnItem.Status.REJECTED, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by("status_order", "-requested_at")

    # ── 7. Pagination ─────────────────────────────────────────────────────────
    paginator = Paginator(qs, 10)
    page_number = request.GET.get("page", 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    status_tabs = [
        ("all", "All"),
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    context = {
        "page_obj": page_obj,
        "kpis": kpis,
        "search_query": search_query,
        "status_filter": status_filter,
        "status_tabs": status_tabs,
        "ReturnItemStatus": ReturnItem.Status,
    }
    return render(request, "admin_panel/returns/return_list.html", context)


@admin_required
@never_cache
@require_POST
def approve_return_view(request, return_item_id):
    """Approve a pending return and restore stock."""
    ri = get_object_or_404(ReturnItem, pk=return_item_id)
    product_name = ri.order_item.product_name
    qty = ri.quantity
    note = (request.POST.get("note", "") or "").strip()

    try:
        approve_return(ri.pk, admin=request.user, note=note)
        messages.success(
            request,
            f"Return for '{product_name}' approved. {qty} unit(s) restored to inventory.",
            extra_tags="admin",
        )
    except ReturnAlreadyDecidedError:
        messages.error(
            request,
            f"Return for '{product_name}' has already been decided.",
            extra_tags="admin",
        )
    except ReturnNotAllowedError as exc:
        messages.error(request, str(exc), extra_tags="admin")
    except ReturnQuantityMismatchError as exc:
        messages.error(request, str(exc), extra_tags="admin")
    except ReturnIntegrityError as exc:
        messages.error(request, str(exc), extra_tags="admin")
    except ReturnNotFoundError:
        messages.error(request, "Return item not found.", extra_tags="admin")
    except AdminPermissionError:
        messages.error(request, "Admin permission required.", extra_tags="admin")

    referer = request.META.get("HTTP_REFERER", "")
    if referer:
        return redirect(referer)
    return redirect("admin_returns:list")


@admin_required
@never_cache
@require_POST
def reject_return_view(request, return_item_id):
    """Reject a pending return with a mandatory note."""
    ri = get_object_or_404(ReturnItem, pk=return_item_id)
    product_name = ri.order_item.product_name
    note = (request.POST.get("note", "") or "").strip()

    if not note:
        messages.error(
            request,
            "A rejection note is required. The return was not rejected.",
            extra_tags="admin",
        )
        referer = request.META.get("HTTP_REFERER", "")
        if referer:
            return redirect(referer)
        return redirect("admin_returns:list")

    try:
        reject_return(ri.pk, admin=request.user, note=note)
        messages.success(
            request,
            f"Return for '{product_name}' rejected.",
            extra_tags="admin",
        )
    except ReturnDecisionNoteRequiredError:
        messages.error(
            request,
            "A rejection note is required. The return was not rejected.",
            extra_tags="admin",
        )
    except ReturnAlreadyDecidedError:
        messages.error(
            request,
            f"Return for '{product_name}' has already been decided.",
            extra_tags="admin",
        )
    except ReturnNotFoundError:
        messages.error(request, "Return item not found.", extra_tags="admin")
    except AdminPermissionError:
        messages.error(request, "Admin permission required.", extra_tags="admin")

    referer = request.META.get("HTTP_REFERER", "")
    if referer:
        return redirect(referer)
    return redirect("admin_returns:list")
