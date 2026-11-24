import json
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from admin_panel.admin_auth.decorators import admin_required
from user_panel.orders.models import Order

from .services import ALLOWED_PERIODS, get_dashboard_stats, get_sales_chart_data

_PERIOD_OPTIONS = [
    (7,   "7 Days"),
    (30,  "30 Days"),
    (90,  "90 Days"),
    (365, "1 Year"),
]


@admin_required
@never_cache
def dashboard_overview(request):
    """Render the main Admin Dashboard overview with live database metrics."""

    # --- Period validation -------------------------------------------------
    try:
        period_days = int(request.GET.get("period", "30"))
    except (ValueError, TypeError):
        period_days = 30
    if period_days not in ALLOWED_PERIODS:
        period_days = 30

    until = timezone.now()
    since = until - timedelta(days=period_days)

    # --- KPI aggregations --------------------------------------------------
    stats = get_dashboard_stats(since, until)

    # --- Chart data --------------------------------------------------------
    chart_data = get_sales_chart_data(since, until)
    chart_data_json = json.dumps(chart_data, cls=DjangoJSONEncoder)

    # --- Recent Orders (8 rows, snapshot total only) -----------------------
    recent_orders = (
        Order.objects
        .select_related("user")
        .only(
            "order_number",
            "created_at",
            "total",
            "status",
            "user__first_name",
            "user__last_name",
            "user__email",
        )
        .order_by("-created_at")[:8]
    )

    context = {
        # Period selector
        "period_days": period_days,
        "period_options": _PERIOD_OPTIONS,
        "since": since,
        "until": until,
        # KPI cards
        "total_revenue": stats["total_revenue"],
        "avg_order_value": stats["avg_order_value"],
        "total_orders": stats["total_orders"],
        "pending_orders": stats["pending_orders"],
        "total_customers": stats["total_customers"],
        "new_customers": stats["new_customers"],
        "total_products": stats["total_products"],
        "brand_count": stats["brand_count"],
        # Chart
        "chart_data_json": chart_data_json,
        # Recent orders table
        "recent_orders": recent_orders,
    }

    return render(request, "dashboard/index.html", context)
