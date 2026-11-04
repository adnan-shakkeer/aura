
from decimal import Decimal
from django.contrib import messages
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from admin_panel.admin_auth.decorators import admin_required
from user_panel.orders.exceptions import (
    AdminPermissionError,
    InvalidOrderStatusError,
    InvalidOrderStatusTransitionError,
    OrderNotFoundError,
)
from user_panel.orders.financials import get_order_financials
from user_panel.orders.models import Order, OrderItem, ReturnItem
from user_panel.orders.order_lifecycle import _ADMIN_TRANSITIONS, transition_order_status


@admin_required
@never_cache
def order_list(request):
    """Admin Orders Listing view.

    Displays one row per complete Order with KPI summary metrics, search,
    status filtering, sorting, pagination, and representative product preview.
    """
    search_query = request.GET.get("search", "").strip()
    status_filter = request.GET.get("status", "all").strip().upper()
    sort_by = request.GET.get("sort", "newest").strip().lower()

    # 1. Base Queryset with necessary relations to avoid N+1 queries
    # Annotate dynamic adjustment values to support sorting by actual adjusted amount
    orders_qs = (
        Order.objects.select_related("user")
        .prefetch_related(
            "items__variant__product__brand",
            "items__variant__images",
            "items__return_item",
        )
        .annotate(
            cancelled_val=Coalesce(
                Sum("items__line_total", filter=Q(items__cancelled_at__isnull=False)),
                Value(Decimal("0.00")),
                output_field=DecimalField(),
            ),
            approved_return_val=Coalesce(
                Sum("items__line_total", filter=Q(items__return_item__status="APPROVED")),
                Value(Decimal("0.00")),
                output_field=DecimalField(),
            ),
        )
        .annotate(
            calc_adjusted_total=Greatest(
                F("total") - F("cancelled_val") - F("approved_return_val"),
                Value(Decimal("0.00")),
                output_field=DecimalField(),
            )
        )
        .all()
    )

    # 2. KPI Summary Calculations (from actual database records)
    total_orders_count = Order.objects.count()
    pending_shipments_count = Order.objects.filter(
        status=Order.OrderStatus.PENDING
    ).count()

    # Revenue represents actual collected payment from delivered COD orders
    revenue_agg = Order.objects.filter(
        status=Order.OrderStatus.DELIVERED,
        payment_status=Order.PaymentStatus.PAID,
    ).aggregate(total=Sum("payment_collected_amount"))["total"]
    total_revenue = revenue_agg if revenue_agg is not None else Decimal("0.00")

    # Returned / Cancelled: orders that are cancelled or have cancelled items or return requests
    returned_cancelled_count = (
        Order.objects.filter(
            Q(status=Order.OrderStatus.CANCELLED)
            | Q(items__cancelled_at__isnull=False)
            | Q(return_requests__isnull=False)
        )
        .distinct()
        .count()
    )

    # 3. Apply Search
    if search_query:
        orders_qs = orders_qs.filter(
            Q(order_number__icontains=search_query)
            | Q(delivery_full_name__icontains=search_query)
            | Q(user__first_name__icontains=search_query)
            | Q(user__last_name__icontains=search_query)
            | Q(user__email__icontains=search_query)
            | Q(user__username__icontains=search_query)
            | Q(items__product_name__icontains=search_query)
            | Q(items__sku__icontains=search_query)
        ).distinct()

    # 4. Apply Status Filter
    if status_filter and status_filter != "ALL":
        if status_filter in Order.OrderStatus.values:
            orders_qs = orders_qs.filter(status=status_filter)

    # 5. Sorting
    sort_mapping = {
        "newest": "-created_at",
        "oldest": "created_at",
        "total_high": "-calc_adjusted_total",
        "total_low": "calc_adjusted_total",
    }
    order_field = sort_mapping.get(sort_by, "-created_at")
    orders_qs = orders_qs.order_by(order_field)

    # 6. Pagination (10 per page)
    paginator = Paginator(orders_qs, 10)
    page_number = request.GET.get("page", 1)
    try:
        orders_page = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        orders_page = paginator.page(1)

    # 7. Build row presentation wrappers (pre-calculating preview and financials)
    order_rows = []
    for order in orders_page:
        items = list(order.items.all())
        total_items_count = len(items)
        representative_item = items[0] if items else None
        financials = get_order_financials(order)

        # Image preview fallback
        rep_image = None
        if representative_item and representative_item.variant:
            images = list(representative_item.variant.images.all())
            if images and images[0].image:
                rep_image = images[0].image.url

        order_rows.append({
            "order": order,
            "financials": financials,
            "total_items_count": total_items_count,
            "more_items_count": max(total_items_count - 1, 0),
            "representative_item": representative_item,
            "rep_image": rep_image,
        })

    context = {
        "order_rows": order_rows,
        "page_obj": orders_page,
        "search_query": search_query,
        "status_filter": status_filter,
        "sort_by": sort_by,
        "total_count": paginator.count,
        # KPIs
        "kpi_total_orders": total_orders_count,
        "kpi_pending_shipments": pending_shipments_count,
        "kpi_total_revenue": total_revenue,
        "kpi_returned_cancelled": returned_cancelled_count,
        "status_choices": Order.OrderStatus.choices,
    }
    return render(request, "admin_panel/orders/order_list.html", context)


@admin_required
@never_cache
def order_detail(request, order_number):
    """Admin Order Details view.

    Presents order snapshot, customer info, delivery address snapshot,
    items with cancellation/return details, price breakdown, audit events,
    and status transition controls.
    """
    order = get_object_or_404(
        Order.objects.select_related("user", "payment_collected_by").prefetch_related(
            "items__variant__product__brand",
            "items__variant__images",
            "items__return_item",
            "status_events__changed_by",
        ),
        order_number=order_number,
    )

    financials = get_order_financials(order)

    # 1. Build item rows with persisted snapshots & return/cancel visibility
    item_rows = []
    for item in order.items.all():
        return_item = getattr(item, "return_item", None)
        image_url = None
        if item.variant:
            images = list(item.variant.images.all())
            if images and images[0].image:
                image_url = images[0].image.url

        item_rows.append({
            "item": item,
            "image_url": image_url,
            "is_cancelled": item.cancelled_at is not None,
            "cancelled_at": item.cancelled_at,
            "cancellation_reason": item.cancellation_reason,
            "return_item": return_item,
        })

    # 2. Status Transition Options
    # Determine allowed next statuses
    allowed_next_statuses = _ADMIN_TRANSITIONS.get(order.status, set())
    # Sort them in approved progression order
    progression_order = [
        Order.OrderStatus.SHIPPED,
        Order.OrderStatus.OUT_FOR_DELIVERY,
        Order.OrderStatus.DELIVERED,
    ]
    next_status_choices = [
        (s, dict(Order.OrderStatus.choices).get(s, s))
        for s in progression_order
        if s in allowed_next_statuses
    ]

    # 3. Status Events log (chronological)
    status_events = list(order.status_events.all().order_by("created_at"))

    # 4. Construct Conceptual Timeline Milestones
    # Map out events by status
    event_by_status = {ev.status: ev for ev in status_events}

    timeline = [
        {
            "key": "placed",
            "title": "Order Placed",
            "timestamp": order.created_at,
            "status": "completed",
            "subtitle": "Order placed by customer",
            "actor": order.user.get_full_name() or order.user.username if order.user else "Customer",
            "note": "",
        },
        {
            "key": "payment",
            "title": "Payment Confirmed",
            "timestamp": order.payment_collected_at if order.payment_status == Order.PaymentStatus.PAID else None,
            "status": "completed" if order.payment_status == Order.PaymentStatus.PAID else ("cancelled" if order.status == Order.OrderStatus.CANCELLED else "pending"),
            "subtitle": f"Cash on Delivery collected (₹{order.payment_collected_amount})" if order.payment_status == Order.PaymentStatus.PAID else ("Order cancelled before payment" if order.status == Order.OrderStatus.CANCELLED else "Awaiting COD collection upon delivery"),
            "actor": (order.payment_collected_by.get_full_name() or order.payment_collected_by.username) if order.payment_collected_by else None,
            "note": "",
        },
        {
            "key": "processing",
            "title": "Processing",
            "timestamp": None,
            "status": "cancelled" if order.status == Order.OrderStatus.CANCELLED else ("completed" if order.status in [Order.OrderStatus.SHIPPED, Order.OrderStatus.OUT_FOR_DELIVERY, Order.OrderStatus.DELIVERED] else "active"),
            "subtitle": "Processing cancelled" if order.status == Order.OrderStatus.CANCELLED else ("Order processed and packed" if order.status in [Order.OrderStatus.SHIPPED, Order.OrderStatus.OUT_FOR_DELIVERY, Order.OrderStatus.DELIVERED] else "Being prepared"),
            "actor": None,
            "note": "",
        },
        {
            "key": "shipped",
            "title": "Shipped",
            "timestamp": event_by_status.get(Order.OrderStatus.SHIPPED).created_at if Order.OrderStatus.SHIPPED in event_by_status else None,
            "status": "completed" if Order.OrderStatus.SHIPPED in event_by_status else ("cancelled" if order.status == Order.OrderStatus.CANCELLED else "pending"),
            "subtitle": "Order dispatched" if Order.OrderStatus.SHIPPED in event_by_status else ("Dispatched" if order.status in [Order.OrderStatus.SHIPPED, Order.OrderStatus.OUT_FOR_DELIVERY, Order.OrderStatus.DELIVERED] else "Pending"),
            "actor": event_by_status.get(Order.OrderStatus.SHIPPED).changed_by if Order.OrderStatus.SHIPPED in event_by_status else None,
            "note": event_by_status.get(Order.OrderStatus.SHIPPED).note if Order.OrderStatus.SHIPPED in event_by_status else "",
        },
        {
            "key": "out_for_delivery",
            "title": "Out for Delivery",
            "timestamp": event_by_status.get(Order.OrderStatus.OUT_FOR_DELIVERY).created_at if Order.OrderStatus.OUT_FOR_DELIVERY in event_by_status else None,
            "status": "completed" if Order.OrderStatus.OUT_FOR_DELIVERY in event_by_status else ("cancelled" if order.status == Order.OrderStatus.CANCELLED else "pending"),
            "subtitle": "In transit with courier" if Order.OrderStatus.OUT_FOR_DELIVERY in event_by_status else ("Out for delivery" if order.status in [Order.OrderStatus.OUT_FOR_DELIVERY, Order.OrderStatus.DELIVERED] else "Pending"),
            "actor": event_by_status.get(Order.OrderStatus.OUT_FOR_DELIVERY).changed_by if Order.OrderStatus.OUT_FOR_DELIVERY in event_by_status else None,
            "note": event_by_status.get(Order.OrderStatus.OUT_FOR_DELIVERY).note if Order.OrderStatus.OUT_FOR_DELIVERY in event_by_status else "",
        },
        {
            "key": "delivered",
            "title": "Delivered",
            "timestamp": event_by_status.get(Order.OrderStatus.DELIVERED).created_at if Order.OrderStatus.DELIVERED in event_by_status else None,
            "status": "completed" if Order.OrderStatus.DELIVERED in event_by_status else ("cancelled" if order.status == Order.OrderStatus.CANCELLED else "pending"),
            "subtitle": "Delivered to customer" if Order.OrderStatus.DELIVERED in event_by_status else ("Delivered" if order.status == Order.OrderStatus.DELIVERED else "Pending"),
            "actor": event_by_status.get(Order.OrderStatus.DELIVERED).changed_by if Order.OrderStatus.DELIVERED in event_by_status else None,
            "note": event_by_status.get(Order.OrderStatus.DELIVERED).note if Order.OrderStatus.DELIVERED in event_by_status else "",
        },
    ]

    context = {
        "order": order,
        "financials": financials,
        "item_rows": item_rows,
        "items_count": len(item_rows),
        "status_events": status_events,
        "timeline": timeline,
        "next_status_choices": next_status_choices,
        "can_update_status": bool(next_status_choices),
    }
    return render(request, "admin_panel/orders/order_detail.html", context)


@admin_required
@require_POST
@never_cache
def update_order_status(request, order_number):
    """Admin Order Status Transition action (POST only).

    Enforces permissions, validates next status against permitted transitions,
    and invokes transition_order_status to update status and auto-collect COD payment.
    """
    order = get_object_or_404(Order, order_number=order_number)
    new_status = request.POST.get("new_status", "").strip()
    note = request.POST.get("note", "").strip()

    if not new_status:
        messages.error(request, "Please select a valid order status.")
        return redirect("admin_orders:detail", order_number=order.order_number)

    try:
        transition_order_status(
            order.id,
            new_status,
            actor=request.user,
            note=note,
        )
        display_status = new_status.replace("_", " ").title()
        messages.success(
            request,
            f"Order #{order.order_number} status updated to {display_status}.",
        )
    except (InvalidOrderStatusTransitionError, InvalidOrderStatusError) as exc:
        messages.error(request, str(exc))
    except AdminPermissionError as exc:
        messages.error(request, str(exc))
    except OrderNotFoundError as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, f"Unable to update order status: {exc}")

    return redirect("admin_orders:detail", order_number=order.order_number)
