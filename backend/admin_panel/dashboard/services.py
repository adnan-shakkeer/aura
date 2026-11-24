"""Dashboard aggregation services.

All KPI and chart data aggregations for the Admin Dashboard.
Revenue uses ``payment_collected_at`` for time-scoping (money-when-collected).
Orders / Customers use ``created_at`` / ``date_joined``.

IMPORTANT: Never join Order and OrderItem in a single aggregation query —
the resulting cartesian product inflates revenue figures. Two separate
queries are merged in Python instead.
"""

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db.models import Count, DecimalField, Q, Sum
from django.db.models.functions import (
    Coalesce,
    TruncDay,
    TruncMonth,
    TruncWeek,
)

from admin_panel.brands.models import Brand
from admin_panel.products.models import Product
from user_panel.orders.models import Order, OrderItem

ZERO = Decimal("0.00")
IST = ZoneInfo("Asia/Kolkata")
ALLOWED_PERIODS = {7, 30, 90, 365}


# ---------------------------------------------------------------------------
# KPI aggregations
# ---------------------------------------------------------------------------

def get_dashboard_stats(since, until):
    """Return a dict of KPI values for the dashboard overview.

    ``since`` / ``until`` are timezone-aware datetimes that bound the
    selected period.  Customers and Products are always cumulative counts
    (not period-filtered).
    """
    User = get_user_model()

    # --- Revenue -----------------------------------------------------------
    # Gross revenue: all PAID orders whose payment was collected in [since, until).
    # Uses Order table only (no OrderItem join) to avoid multiplication.
    revenue_agg = Order.objects.filter(
        payment_status=Order.PaymentStatus.PAID,
        payment_collected_at__range=(since, until),
    ).aggregate(
        revenue=Coalesce(
            Sum("payment_collected_amount"),
            ZERO,
            output_field=DecimalField(),
        ),
        paid_count=Count("id"),
    )
    total_revenue = revenue_agg["revenue"]
    paid_count = revenue_agg["paid_count"]
    avg_order_value = (total_revenue / paid_count) if paid_count > 0 else None

    # --- Orders ------------------------------------------------------------
    total_orders = Order.objects.filter(
        created_at__range=(since, until),
    ).count()
    pending_orders = Order.objects.filter(
        status=Order.OrderStatus.PENDING,
        created_at__range=(since, until),
    ).count()

    # --- Customers ---------------------------------------------------------
    # Cumulative: exclude staff and superusers, no is_active filter
    # (consistent with admin_panel/users/views.py base queryset).
    base_customer_qs = User.objects.filter(
        is_staff=False,
        is_superuser=False,
    )
    total_customers = base_customer_qs.count()
    new_customers = base_customer_qs.filter(
        date_joined__range=(since, until),
    ).count()

    # --- Products & Brands -------------------------------------------------
    total_products = Product.objects.filter(is_deleted=False).count()
    brand_count = Brand.objects.filter(is_deleted=False).count()

    return {
        "total_revenue": total_revenue,
        "avg_order_value": avg_order_value,
        "total_orders": total_orders,
        "pending_orders": pending_orders,
        "total_customers": total_customers,
        "new_customers": new_customers,
        "total_products": total_products,
        "brand_count": brand_count,
    }


# ---------------------------------------------------------------------------
# Chart data
# ---------------------------------------------------------------------------

def _get_trunc_config(period_days):
    """Return (TruncFunction, strftime_format) based on the period length."""
    if period_days <= 30:
        return TruncDay, "%d %b"
    elif period_days <= 90:
        return TruncWeek, "%d %b"
    else:
        return TruncMonth, "%b %Y"


def _generate_period_spine(since, until, trunc_fn, tzinfo):
    """Generate a de-duplicated, ordered list of truncated period keys
    covering [since, until] using daily steps, then de-dup via a set.

    This matches the keys produced by Django's Trunc functions so that
    revenue_map / units_map lookups align correctly.
    """
    periods = []
    seen = set()
    cursor = since.astimezone(tzinfo).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = until.astimezone(tzinfo)

    while cursor <= end:
        # Apply the same truncation Django uses so the key matches
        key = _apply_trunc(cursor, trunc_fn, tzinfo)
        if key not in seen:
            seen.add(key)
            periods.append(key)
        cursor += timedelta(days=1)

    return periods


def _apply_trunc(dt, trunc_fn, tzinfo):
    """Apply a Trunc-equivalent in Python for period spine generation."""
    local = dt.astimezone(tzinfo)
    if trunc_fn is TruncDay:
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif trunc_fn is TruncWeek:
        # ISO week starts Monday
        days_since_monday = local.weekday()
        monday = local - timedelta(days=days_since_monday)
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    elif trunc_fn is TruncMonth:
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def get_sales_chart_data(since, until):
    """Return chart labels, revenue series, and units series.

    Two separate queries are used and merged in Python to avoid cartesian
    product inflation when joining Order and OrderItem tables.

    Revenue is scoped by ``payment_collected_at`` (same as the KPI card).
    Units Sold is scoped by ``order__created_at`` for the same period.
    """
    period_days = max(1, (until - since).days)
    trunc_fn, label_fmt = _get_trunc_config(period_days)

    # Query 1: Revenue by truncated period (Order table only)
    revenue_qs = (
        Order.objects
        .filter(
            payment_status=Order.PaymentStatus.PAID,
            payment_collected_at__range=(since, until),
        )
        .annotate(period=trunc_fn("payment_collected_at", tzinfo=IST))
        .values("period")
        .annotate(revenue=Sum("payment_collected_amount"))
        .order_by("period")
    )

    # Query 2: Units sold by truncated period (OrderItem table)
    # Exclude cancelled items; exclude items with approved returns.
    # Uses Q() filter to avoid excluding items with no return at all.
    units_qs = (
        OrderItem.objects
        .filter(
            order__payment_status=Order.PaymentStatus.PAID,
            order__created_at__range=(since, until),
            cancelled_at__isnull=True,
        )
        .filter(
            Q(return_item__isnull=True)
            | ~Q(return_item__status="APPROVED")
        )
        .annotate(period=trunc_fn("order__created_at", tzinfo=IST))
        .values("period")
        .annotate(units=Sum("quantity"))
        .order_by("period")
    )

    revenue_map = {
        row["period"]: float(row["revenue"] or 0)
        for row in revenue_qs
        if row["period"] is not None
    }
    units_map = {
        row["period"]: int(row["units"] or 0)
        for row in units_qs
        if row["period"] is not None
    }

    all_periods = _generate_period_spine(since, until, trunc_fn, IST)

    labels = []
    revenue_series = []
    units_series = []

    for p in all_periods:
        labels.append(p.strftime(label_fmt))
        revenue_series.append(revenue_map.get(p, 0))
        units_series.append(units_map.get(p, 0))

    has_data = bool(revenue_map or units_map)

    return {
        "labels": labels,
        "revenue": revenue_series,
        "units": units_series,
        "has_data": has_data,
    }

