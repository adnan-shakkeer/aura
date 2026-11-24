"""Admin Sales Report views.

Read-only reporting over existing Order, OrderItem, PaymentAttempt, Refund and
WalletTransaction records. No new models or migrations are introduced.

Financial definitions follow the existing authoritative snapshot architecture:

* Gross Sales   = sum(Order.subtotal) for paid qualifying orders
* Discounts     = sum(Order.discount_total) for paid qualifying orders
* Net Sales     = Gross Sales - Discounts - cancelled item value
                  - approved return value (via get_order_financials logic)
* Collected Revenue = sum(payment_collected_amount) for PAID qualifying orders
                      - sum(REFUND.amount_paise) for COMPLETED refunds on those orders
* Total Orders  = all orders in the date range
* Paid Orders   = orders with payment_status == PAID in the date range
* Average Collected per Paid Order = Collected Revenue / Paid Orders

Order date is driven by Order.created_at (Asia/Kolkata timezone).
"""

import csv
import logging
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache

from admin_panel.admin_auth.decorators import admin_required
from user_panel.orders.financials import get_order_financials
from user_panel.orders.models import Order, OrderItem, ReturnItem
from user_panel.payments.models import Refund
from user_panel.wallet.models import WalletTransaction

logger = logging.getLogger(__name__)

ZERO = 0.00

PAYMENT_METHOD_CHOICES = (
    ("COD", "Cash on Delivery"),
    ("RAZORPAY", "Razorpay"),
    ("WALLET", "AURA Wallet"),
)

ORDER_STATUS_CHOICES = (
    ("PENDING", "Pending"),
    ("SHIPPED", "Shipped"),
    ("OUT_FOR_DELIVERY", "Out for Delivery"),
    ("DELIVERED", "Delivered"),
    ("CANCELLED", "Cancelled"),
)

PAYMENT_STATUS_CHOICES = (
    ("UNPAID", "Unpaid"),
    ("PENDING", "Payment pending"),
    ("FAILED", "Payment failed"),
    ("EXPIRED", "Payment expired"),
    ("PAID", "Paid"),
)

PRESET_RANGES = {
    "today": ("Today", lambda tz: _date_range_today(tz)),
    "yesterday": ("Yesterday", lambda tz: _date_range_yesterday(tz)),
    "last_7_days": ("Last 7 Days", lambda tz: _date_range_last_n_days(tz, 7)),
    "last_30_days": ("Last 30 Days", lambda tz: _date_range_last_n_days(tz, 30)),
    "this_month": ("This Month", lambda tz: _date_range_this_month(tz)),
    "last_month": ("Last Month", lambda tz: _date_range_last_month(tz)),
    "this_year": ("This Year", lambda tz: _date_range_this_year(tz)),
}


def _date_range_today(tz):
    today = timezone.localtime(timezone.now(), tz).date()
    return today, today


def _date_range_yesterday(tz):
    yesterday = timezone.localtime(timezone.now(), tz).date() - timedelta(days=1)
    return yesterday, yesterday


def _date_range_last_n_days(tz, n):
    today = timezone.localtime(timezone.now(), tz).date()
    start = today - timedelta(days=n - 1)
    return start, today


def _date_range_this_month(tz):
    now = timezone.localtime(timezone.now(), tz)
    start = now.replace(day=1).date()
    return start, now.date()


def _date_range_last_month(tz):
    now = timezone.localtime(timezone.now(), tz)
    first_of_this_month = now.replace(day=1)
    last_of_last_month = first_of_this_month - timedelta(days=1)
    start = last_of_last_month.replace(day=1)
    return start, last_of_last_month.date()


def _date_range_this_year(tz):
    now = timezone.localtime(timezone.now(), tz)
    start = now.replace(month=1, day=1).date()
    return start, now.date()


def _parse_date(value):
    if not value:
        return None
    try:
        return parse_date(value)
    except (ValueError, TypeError):
        return None


def _build_date_filter(start_date, end_date, tz):
    """Return a Q filter for Order.created_at within [start_date, end_date]
    inclusive, using Asia/Kolkata timezone boundaries."""
    if not start_date or not end_date:
        return Q()
    start_dt = timezone.make_aware(
        datetime.combine(start_date, time.min), tz
    )
    end_dt = timezone.make_aware(
        datetime.combine(end_date, time.max), tz
    )
    return Q(created_at__gte=start_dt, created_at__lte=end_dt)


def _get_filtered_orders(request, tz):
    """Return the filtered base queryset of orders within the date range."""
    start_date = _parse_date(request.GET.get("start_date"))
    end_date = _parse_date(request.GET.get("end_date"))

    # Apply preset range if no custom dates provided
    if start_date is None and end_date is None:
        preset = request.GET.get("preset", "last_30_days")
        if preset in PRESET_RANGES:
            start_date, end_date = PRESET_RANGES[preset][1](tz)

    date_filter = _build_date_filter(start_date, end_date, tz)

    orders_qs = Order.objects.filter(date_filter)

    # Payment method filter
    payment_method = request.GET.get("payment_method", "").strip().upper()
    if payment_method in dict(Order.PaymentMethod.choices):
        orders_qs = orders_qs.filter(payment_method=payment_method)

    # Order status filter
    order_status = request.GET.get("order_status", "").strip().upper()
    if order_status in dict(Order.OrderStatus.choices):
        orders_qs = orders_qs.filter(status=order_status)

    # Payment status filter
    payment_status = request.GET.get("payment_status", "").strip().upper()
    if payment_status in dict(Order.PaymentStatus.choices):
        orders_qs = orders_qs.filter(payment_status=payment_status)

    return orders_qs.order_by("-created_at", "-id"), start_date, end_date


def _compute_metrics(orders_qs, tz):
    """Compute all KPI metrics from the filtered orders queryset."""
    # Sub-queryset of paid orders
    paid_orders_qs = orders_qs.filter(payment_status=Order.PaymentStatus.PAID)

    # Total orders (all in range)
    total_orders = orders_qs.count()
    paid_orders = paid_orders_qs.count()

    # Gross Sales = sum of subtotal for paid orders
    gross_sales_agg = paid_orders_qs.aggregate(
        total=Sum("subtotal", output_field=DecimalField())
    )["total"] or 0
    gross_sales = float(gross_sales_agg)

    # Discounts = sum of discount_total for paid orders
    discounts_agg = paid_orders_qs.aggregate(
        total=Sum("discount_total", output_field=DecimalField())
    )["total"] or 0
    discounts = float(discounts_agg)

    # Collected Revenue (gross) = sum of payment_collected_amount for paid orders
    collected_gross_agg = paid_orders_qs.aggregate(
        total=Sum("payment_collected_amount", output_field=DecimalField())
    )["total"] or 0
    collected_gross = float(collected_gross_agg)

    # Completed refunds on these orders
    refunds_agg = Refund.objects.filter(
        status=Refund.Status.COMPLETED,
        order__in=paid_orders_qs,
    ).aggregate(total=Sum("amount_paise", output_field=DecimalField()))[
        "total"
    ] or 0
    refunds_total = float(refunds_agg) / 100.0

    collected_revenue = collected_gross - refunds_total
    if collected_revenue < 0:
        collected_revenue = 0.0

    # Net Sales: iterate paid orders with prefetched items to compute
    # per-order net sales (adjusted_total equivalent) using get_order_financials.
    orders_with_items = (
        paid_orders_qs.prefetch_related("items__return_item")
        .select_related("user")
    )

    net_sales = 0.0
    for order in orders_with_items:
        financials = get_order_financials(order)
        net_sales += float(financials.adjusted_total)

    average_collected = (
        collected_revenue / paid_orders if paid_orders > 0 else 0.0
    )

    return {
        "total_orders": total_orders,
        "paid_orders": paid_orders,
        "gross_sales": round(gross_sales, 2),
        "discounts": round(discounts, 2),
        "net_sales": round(net_sales, 2),
        "collected_gross": round(collected_gross, 2),
        "refunds_total": round(refunds_total, 2),
        "collected_revenue": round(collected_revenue, 2),
        "average_collected_per_paid_order": round(average_collected, 2),
    }


def _compute_payment_method_breakdown(orders_qs):
    """Compute per-payment-method breakdown from the filtered orders."""
    methods = {}
    for method_code, method_label in PAYMENT_METHOD_CHOICES:
        method_orders = orders_qs.filter(
            payment_method=method_code, payment_status=Order.PaymentStatus.PAID
        )
        order_count = method_orders.count()

        order_value_agg = method_orders.aggregate(
            total=Sum("total", output_field=DecimalField())
        )["total"] or 0
        order_value = float(order_value_agg)

        collected_agg = method_orders.aggregate(
            total=Sum("payment_collected_amount", output_field=DecimalField())
        )["total"] or 0
        collected_gross = float(collected_agg)

        refunds_agg = Refund.objects.filter(
            status=Refund.Status.COMPLETED,
            order__in=method_orders,
        ).aggregate(total=Sum("amount_paise", output_field=DecimalField()))[
            "total"
        ] or 0
        refunds_total = float(refunds_agg) / 100.0

        collected_revenue = collected_gross - refunds_total
        if collected_revenue < 0:
            collected_revenue = 0.0

        methods[method_code] = {
            "label": method_label,
            "order_count": order_count,
            "order_value": round(order_value, 2),
            "collected_revenue": round(collected_revenue, 2),
        }

    return methods


def _compute_chart_data(orders_qs, granularity, tz):
    """Compute daily or monthly chart data for revenue and units sold.

    Groups orders by date in Asia/Kolkata timezone. Revenue is collected
    revenue (payment_collected_amount for PAID orders minus completed
    refunds on those orders). Units sold is sum of eligible item quantities
    (non-cancelled, non-approved-returned).
    """
    # Prefetch items to avoid N+1 when computing units sold
    orders = list(
        orders_qs.prefetch_related("items__return_item").order_by("created_at")
    )

    # Build refund lookup by order
    refund_map = {}
    if orders:
        refunds = Refund.objects.filter(
            status=Refund.Status.COMPLETED,
            order__in=[o.pk for o in orders],
        ).values("order_id").annotate(total=Sum("amount_paise"))
        for r in refunds:
            refund_map[r["order_id"]] = float(r["total"]) / 100.0

    # Group by date
    daily_data = {}
    for order in orders:
        local_dt = timezone.localtime(order.created_at, tz)
        if granularity == "monthly":
            key = local_dt.strftime("%Y-%m")
            label = local_dt.strftime("%b %Y")
        else:
            key = local_dt.strftime("%Y-%m-%d")
            label = local_dt.strftime("%d %b")

        if key not in daily_data:
            daily_data[key] = {
                "key": key,
                "label": label,
                "revenue": 0.0,
                "units_sold": 0,
            }

        # Revenue: collected amount minus refunds for this order
        collected = float(order.payment_collected_amount or 0)
        order_refund = refund_map.get(order.pk, 0.0)
        daily_data[key]["revenue"] += collected - order_refund

        # Units sold: sum eligible item quantities
        for item in order.items.all():
            if item.cancelled_at is not None:
                continue
            return_item = getattr(item, "return_item", None)
            if return_item is not None and return_item.status == ReturnItem.Status.APPROVED:
                continue
            daily_data[key]["units_sold"] += item.quantity

    # Sort by key
    sorted_keys = sorted(daily_data.keys())
    chart_data = [daily_data[k] for k in sorted_keys]

    return chart_data


def _compute_best_selling_products(orders_qs):
    """Compute best-selling products from eligible OrderItem snapshots."""
    # Get all order IDs in the filtered paid set
    order_ids = list(orders_qs.values_list("pk", flat=True))

    if not order_ids:
        return []

    items = (
        OrderItem.objects.filter(order_id__in=order_ids)
        .select_related("variant__product__brand")
        .prefetch_related("return_item")
    )

    product_map = {}
    for item in items:
        if item.cancelled_at is not None:
            continue
        return_item = getattr(item, "return_item", None)
        if return_item is not None and return_item.status == ReturnItem.Status.APPROVED:
            continue

        key = item.variant_id
        if key not in product_map:
            product_map[key] = {
                "product_name": item.product_name,
                "brand_name": item.brand_name,
                "variant_size": item.variant_size,
                "sku": item.sku,
                "units_sold": 0,
                "product_revenue": 0.0,
            }

        product_map[key]["units_sold"] += item.quantity
        product_map[key]["product_revenue"] += float(item.paid_amount or 0)

    # Sort by units sold descending
    products = sorted(
        product_map.values(),
        key=lambda p: p["units_sold"],
        reverse=True,
    )[:10]

    for p in products:
        p["product_revenue"] = round(p["product_revenue"], 2)

    return products


def _build_transaction_rows(orders_qs, paginator_page):
    """Build detailed transaction rows for the current page."""
    rows = []
    for order in paginator_page:
        financials = get_order_financials(order)
        rows.append(
            {
                "order_number": order.order_number,
                "order_date": order.created_at,
                "customer": order.delivery_full_name
                or (order.user.get_full_name() if order.user else "")
                or (order.user.username if order.user else ""),
                "payment_method": order.get_payment_method_display(),
                "payment_status": order.get_payment_status_display(),
                "order_status": order.get_status_display(),
                "gross_amount": float(order.subtotal),
                "discount": float(order.discount_total),
                "net_amount": float(financials.adjusted_total),
            }
        )
    return rows


def _csv_escape(value):
    """Escape a value for safe CSV output, guarding against formula injection."""
    if value is None:
        return ""
    text = str(value)
    # Prevent spreadsheet formula injection: if text starts with dangerous chars,
    # prefix with a tab to neutralize it.
    if text and text[0] in ("=", "+", "-", "@"):
        text = "\t" + text
    return text


@admin_required
@never_cache
def sales_report(request):
    """Admin Sales Report page.

    Aggregates order data with date, payment method, order status and payment
    status filters. Provides KPI cards, chart data, payment breakdown,
    best-selling products and paginated transaction table.
    """
    tz = timezone.get_current_timezone()

    orders_qs, start_date, end_date = _get_filtered_orders(request, tz)

    metrics = _compute_metrics(orders_qs, tz)
    payment_breakdown = _compute_payment_method_breakdown(orders_qs)

    granularity = request.GET.get("granularity", "daily")
    if granularity not in ("daily", "monthly"):
        granularity = "daily"
    chart_data = _compute_chart_data(orders_qs, granularity, tz)
    best_selling = _compute_best_selling_products(orders_qs)

    # Pagination for detailed transactions
    from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator

    paginator = Paginator(orders_qs, 25)
    page_number = request.GET.get("page", 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    transaction_rows = _build_transaction_rows(orders_qs, page_obj)

    # Build filter context for template
    start_date_str = start_date.isoformat() if start_date else ""
    end_date_str = end_date.isoformat() if end_date else ""

    # Build current query string for CSV export link (exclude 'export' and 'page')
    current_params = request.GET.copy()
    current_params.pop("export", None)
    current_params.pop("page", None)
    current_query_string = current_params.urlencode()

    context = {
        "metrics": metrics,
        "payment_breakdown": payment_breakdown,
        "chart_data": chart_data,
        "chart_granularity": granularity,
        "best_selling_products": best_selling,
        "transaction_rows": transaction_rows,
        "page_obj": page_obj,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "payment_method": request.GET.get("payment_method", ""),
        "order_status": request.GET.get("order_status", ""),
        "payment_status": request.GET.get("payment_status", ""),
        "payment_method_choices": PAYMENT_METHOD_CHOICES,
        "order_status_choices": ORDER_STATUS_CHOICES,
        "payment_status_choices": PAYMENT_STATUS_CHOICES,
        "preset_ranges": PRESET_RANGES,
        "active_preset": request.GET.get("preset", "last_30_days"),
        "current_query_string": current_query_string,
    }

    # CSV export
    if request.GET.get("export") == "csv":
        return _csv_export(orders_qs, start_date, end_date)

    return render(request, "admin_panel/reports/sales_report.html", context)


def _csv_export(orders_qs, start_date, end_date):
    """Stream CSV export of filtered transaction records."""
    from django.utils.encoding import smart_str

    rows = []
    for order in orders_qs.select_related("user"):
        financials = get_order_financials(order)
        rows.append(
            [
                order.order_number,
                timezone.localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
                order.delivery_full_name
                or (order.user.get_full_name() if order.user else "")
                or (order.user.username if order.user else ""),
                order.get_payment_method_display(),
                order.get_payment_status_display(),
                order.get_status_display(),
                f"{float(order.subtotal):.2f}",
                f"{float(order.discount_total):.2f}",
                f"{float(financials.adjusted_total):.2f}",
            ]
        )

    start_str = start_date.isoformat() if start_date else "start"
    end_str = end_date.isoformat() if end_date else "end"
    filename = f"sales_report_{start_str}_{end_str}.csv"

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(
        [
            "Order Number",
            "Order Date",
            "Customer",
            "Payment Method",
            "Payment Status",
            "Order Status",
            "Gross Amount",
            "Discount",
            "Net Amount",
        ]
    )
    for row in rows:
        writer.writerow([_csv_escape(cell) for cell in row])

    return response


@admin_required
@never_cache
def sales_report_csv(request):
    """Dedicated CSV export endpoint for the sales report."""
    tz = timezone.get_current_timezone()
    orders_qs, start_date, end_date = _get_filtered_orders(request, tz)
    return _csv_export(orders_qs, start_date, end_date)
