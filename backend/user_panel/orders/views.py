from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .cancellation import cancel_order, cancel_order_item
from .exceptions import (
    ItemAlreadyCancelledError,
    ItemNotCancellableError,
    OrderItemNotFoundError,
    OrderPermissionError,
    OrderServiceError,
    ReturnAlreadyRequestedError,
    ReturnNotAllowedError,
    ReturnReasonRequiredError,
)
from .financials import get_order_financials
from .invoices import (
    build_invoice_context,
    build_invoice_filename,
    is_invoice_available,
    render_invoice_pdf,
)
from .models import Order, OrderItem, OrderStatusEvent, ReturnItem
from .returns import request_return, request_return_order


MY_ORDERS_PAGE_SIZE = 8
MAX_ORDER_SEARCH_LENGTH = 100


def _build_order_cards(orders):
    """Build the compact order summaries used by the My Orders listing."""
    cards = []

    for order in orders:
        items = list(order.items.all())
        first_item = items[0] if items else None
        image = None

        if first_item is not None:
            images = list(first_item.variant.images.all())
            image = images[0] if images else None

        cards.append(
            {
                "order": order,
                "first_item": first_item,
                "image": image,
                "extra_item_count": max(len(items) - 1, 0),
                "total_quantity": sum(item.quantity for item in items),
            }
        )

    return cards


def _build_order_timeline(order):
    """Construct fulfilment timeline milestones without inferring unrecorded events."""
    events_by_status = {}
    for event in order.status_events.all():
        events_by_status[event.status] = event

    if order.status == Order.OrderStatus.CANCELLED:
        cancel_event = events_by_status.get(Order.OrderStatus.CANCELLED)
        return [
            {
                "key": "ordered",
                "label": "Ordered",
                "is_completed": True,
                "is_current": False,
                "timestamp": order.created_at,
                "note": "Order placed successfully.",
            },
            {
                "key": "cancelled",
                "label": "Cancelled",
                "is_completed": True,
                "is_current": True,
                "timestamp": cancel_event.created_at if cancel_event else None,
                "note": cancel_event.note if cancel_event else "Order cancelled.",
            },
        ]

    steps_def = [
        ("ordered", "Ordered", None),
        (Order.OrderStatus.SHIPPED, "Shipped", Order.OrderStatus.SHIPPED),
        (
            Order.OrderStatus.OUT_FOR_DELIVERY,
            "Out for Delivery",
            Order.OrderStatus.OUT_FOR_DELIVERY,
        ),
        (Order.OrderStatus.DELIVERED, "Delivered", Order.OrderStatus.DELIVERED),
    ]

    timeline = []
    # Ordered is always completed at order.created_at
    timeline.append(
        {
            "key": "ordered",
            "label": "Ordered",
            "is_completed": True,
            "is_current": (order.status == Order.OrderStatus.PENDING),
            "timestamp": order.created_at,
            "note": "Order placed successfully.",
        }
    )

    for step_key, label, status_val in steps_def[1:]:
        event = events_by_status.get(status_val)
        is_completed = event is not None
        is_current = order.status == status_val
        timeline.append(
            {
                "key": step_key.lower(),
                "label": label,
                "is_completed": is_completed,
                "is_current": is_current,
                "timestamp": event.created_at if event else None,
                "note": event.note if event else "",
            }
        )

    return timeline


def _build_item_rows(order):
    """Build item display rows including snapshots, outcomes, and action eligibility."""
    rows = []
    for item in order.items.all():
        images = list(item.variant.images.all())
        image = images[0] if images else None

        return_item = getattr(item, "return_item", None)

        if item.cancelled_at is not None:
            outcome_status = "Cancelled"
            outcome_key = "cancelled"
            is_cancellable = False
            is_returnable = False
            rejection_note = ""
        elif return_item is not None:
            if return_item.status == ReturnItem.Status.REQUESTED:
                outcome_status = "Return Requested"
                outcome_key = "return_requested"
            elif return_item.status == ReturnItem.Status.APPROVED:
                outcome_status = "Return Approved"
                outcome_key = "return_approved"
            elif return_item.status == ReturnItem.Status.REJECTED:
                outcome_status = "Return Rejected"
                outcome_key = "return_rejected"
            else:
                outcome_status = return_item.get_status_display()
                outcome_key = return_item.status.lower()

            rejection_note = (
                (return_item.decision_note or "").strip()
                if return_item.status == ReturnItem.Status.REJECTED
                else ""
            )
            is_cancellable = False
            is_returnable = False
        else:
            outcome_status = order.get_status_display()
            outcome_key = order.status.lower()
            rejection_note = ""
            is_cancellable = order.status == Order.OrderStatus.PENDING
            is_returnable = order.status == Order.OrderStatus.DELIVERED

        rows.append(
            {
                "item": item,
                "image": image,
                "outcome_status": outcome_status,
                "outcome_key": outcome_key,
                "is_cancellable": is_cancellable,
                "is_returnable": is_returnable,
                "return_item": return_item,
                "rejection_note": rejection_note,
            }
        )

    return rows


@login_required
@never_cache
def order_confirmation(request, order_number):
    """Render the persisted confirmation for one of the customer's orders."""
    order = get_object_or_404(
        Order.objects.prefetch_related("items__variant__images"),
        order_number=order_number,
        user=request.user,
    )

    items = list(order.items.all())
    line_rows = []
    for item in items:
        images = list(item.variant.images.all())
        line_rows.append(
            {
                "item": item,
                "image": images[0] if images else None,
            }
        )

    context = {
        "order": order,
        "line_rows": line_rows,
        "total_quantity": sum(item.quantity for item in items),
    }
    return render(request, "orders/order_confirmation.html", context)


@login_required
@never_cache
@require_GET
def my_orders(request):
    """Render the authenticated customer's order history."""
    search_query = request.GET.get("q", "").strip()[:MAX_ORDER_SEARCH_LENGTH]

    orders = Order.objects.filter(user=request.user).prefetch_related(
        Prefetch(
            "items",
            queryset=OrderItem.objects.select_related("variant")
            .prefetch_related("variant__images")
            .order_by("id"),
        )
    )

    if search_query:
        orders = orders.filter(order_number__icontains=search_query)

    paginator = Paginator(orders, MY_ORDERS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)
    if not search_query:
        params.pop("q", None)
    base_query = params.urlencode()

    context = {
        "order_cards": _build_order_cards(page_obj.object_list),
        "page_obj": page_obj,
        "search_query": search_query,
        "base_query": base_query,
        "active_nav": "orders",
    }
    return render(request, "orders/my_orders.html", context)


@login_required
@never_cache
@require_GET
def order_detail(request, order_number):
    """Render the individual order details page with line-level outcomes and timeline."""
    order = get_object_or_404(
        Order.objects.filter(user=request.user).prefetch_related(
            Prefetch(
                "items",
                queryset=OrderItem.objects.select_related("variant", "return_item")
                .prefetch_related("variant__images")
                .order_by("id"),
            ),
            Prefetch(
                "status_events",
                queryset=OrderStatusEvent.objects.order_by("created_at", "id"),
            ),
        ),
        order_number=order_number,
    )

    financials = get_order_financials(order)
    timeline_steps = _build_order_timeline(order)
    item_rows = _build_item_rows(order)

    has_active_items = any(row["item"].cancelled_at is None for row in item_rows)
    is_order_cancellable = (
        order.status == Order.OrderStatus.PENDING
    ) and has_active_items
    is_order_returnable = (
        order.status == Order.OrderStatus.DELIVERED
    ) and any(row["is_returnable"] for row in item_rows)

    context = {
        "order": order,
        "financials": financials,
        "timeline_steps": timeline_steps,
        "item_rows": item_rows,
        "is_order_cancellable": is_order_cancellable,
        "is_order_returnable": is_order_returnable,
        "can_download_invoice": is_invoice_available(order),
        "active_nav": "orders",
    }
    return render(request, "orders/order_detail.html", context)


@login_required
@never_cache
@require_GET
def download_invoice(request, order_number):
    """Return the original-purchase PDF invoice for a delivered order."""
    order = get_object_or_404(
        Order.objects.select_related("user").prefetch_related("items"),
        order_number=order_number,
        user=request.user,
    )

    if not is_invoice_available(order):
        return HttpResponseForbidden(
            "Invoice is available only for delivered orders."
        )

    context = build_invoice_context(order)
    pdf_bytes = render_invoice_pdf(context)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="{build_invoice_filename(order)}"'
    )
    response["Content-Length"] = str(len(pdf_bytes))
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@never_cache
@require_POST
def cancel_order_view(request, order_number):
    """Process customer whole-order cancellation for all remaining active items."""
    order = get_object_or_404(
        Order,
        order_number=order_number,
        user=request.user,
    )

    reason = request.POST.get("reason", "").strip()

    try:
        cancel_order(request.user, order.pk, reason=reason)
        messages.success(
            request, f"Order #{order.order_number} has been cancelled."
        )
    except (
        ItemNotCancellableError,
        ItemAlreadyCancelledError,
        OrderPermissionError,
        OrderItemNotFoundError,
        OrderServiceError,
    ) as exc:
        messages.error(request, str(exc))

    return redirect("orders:order_detail", order_number=order.order_number)


@login_required
@never_cache
@require_POST
def cancel_order_item_view(request, order_number, item_id):
    """Process customer cancellation for a single OrderItem."""
    order = get_object_or_404(
        Order,
        order_number=order_number,
        user=request.user,
    )
    item = get_object_or_404(
        OrderItem,
        pk=item_id,
        order=order,
    )

    reason = request.POST.get("reason", "").strip()

    try:
        cancel_order_item(request.user, item.pk, reason=reason)
        messages.success(
            request, f"Item '{item.product_name}' has been cancelled."
        )
    except (
        ItemNotCancellableError,
        ItemAlreadyCancelledError,
        OrderPermissionError,
        OrderItemNotFoundError,
        OrderServiceError,
    ) as exc:
        messages.error(request, str(exc))

    return redirect("orders:order_detail", order_number=order.order_number)


@login_required
@never_cache
@require_POST
def request_return_item_view(request, order_number, item_id):
    """Process customer return request for a single delivered OrderItem."""
    order = get_object_or_404(
        Order,
        order_number=order_number,
        user=request.user,
    )
    item = get_object_or_404(
        OrderItem,
        pk=item_id,
        order=order,
    )

    reason = request.POST.get("reason", "").strip()

    try:
        request_return(request.user, item.pk, reason=reason)
        messages.success(
            request,
            f"Return requested for '{item.product_name}'. Our team will review your request.",
        )
    except (
        ReturnReasonRequiredError,
        ReturnNotAllowedError,
        ReturnAlreadyRequestedError,
        OrderPermissionError,
        OrderItemNotFoundError,
        OrderServiceError,
    ) as exc:
        messages.error(request, str(exc))

    return redirect("orders:order_detail", order_number=order.order_number)


@login_required
@never_cache
@require_POST
def request_return_order_view(request, order_number):
    """Process customer whole-order return request for all remaining eligible items."""
    order = get_object_or_404(
        Order,
        order_number=order_number,
        user=request.user,
    )

    reason = request.POST.get("reason", "").strip()

    try:
        request_return_order(request.user, order.pk, reason=reason)
        messages.success(
            request,
            f"Return requested for all eligible items in Order #{order.order_number}. Our team will review your request.",
        )
    except (
        ReturnReasonRequiredError,
        ReturnNotAllowedError,
        ReturnAlreadyRequestedError,
        OrderPermissionError,
        OrderItemNotFoundError,
        OrderServiceError,
    ) as exc:
        messages.error(request, str(exc))

    return redirect("orders:order_detail", order_number=order.order_number)
