from django.shortcuts import render
from django.views.decorators.cache import never_cache
from admin_panel.admin_auth.decorators import admin_required

# Note: Once your models are created, you will replace the hardcoded values with actual Django queries:
# from orders.models import Order
# from products.models import Product
# from accounts.models import User

@admin_required
@never_cache
def dashboard_overview(request):
    """
    Renders the main Admin Dashboard overview with metrics and recent orders.
    """
    # 1. Total Revenue
    # total_revenue = Order.objects.filter(status='completed').aggregate(Sum('total_price'))['total_price__sum'] or 0
    total_revenue = 842650

    # 2. Total Orders & Average Order Value
    # total_orders = Order.objects.count()
    # avg_order_value = Order.objects.aggregate(Avg('total_price'))['total_price__avg'] or 0
    total_orders = 1248
    avg_order_value = 675

    # 3. Total Customers
    # total_customers = User.objects.filter(is_staff=False, is_superuser=False).count()
    total_customers = 864

    # 4. Total Products
    # total_products = Product.objects.count()
    total_products = 126

    # 5. Recent Orders List (including explicit action_url for the ACTION column)
    # recent_orders = Order.objects.select_related('user').order_by('-created_at')[:5]
    recent_orders = [
        {
            'order_id': '#AUR102415',
            'customer': 'Isabella Saint-Germain',
            'date': 'Oct 12, 2026',
            'total': '12,400',
            'status': 'SHIPPED',
            'action_url': '/admin-panel/orders/AUR102415/',
        },
        {
            'order_id': '#AUR102416',
            'customer': 'Sebastian Blackwood',
            'date': 'Oct 12, 2026',
            'total': '8,900',
            'status': 'PROCESSING',
            'action_url': '/admin-panel/orders/AUR102416/',
        },
        {
            'order_id': '#AUR102417',
            'customer': 'Margot Devereux',
            'date': 'Oct 11, 2026',
            'total': '24,150',
            'status': 'PENDING',
            'action_url': '/admin-panel/orders/AUR102417/',
        },
        {
            'order_id': '#AUR102418',
            'customer': 'Julian Vane',
            'date': 'Oct 11, 2026',
            'total': '15,000',
            'status': 'DELIVERED',
            'action_url': '/admin-panel/orders/AUR102418/',
        },
        {
            'order_id': '#AUR102419',
            'customer': 'Clara Fontaine',
            'date': 'Oct 10, 2026',
            'total': '6,200',
            'status': 'CANCELLED',
            'action_url': '/admin-panel/orders/AUR102419/',
        },
    ]

    context = {
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_order_value': avg_order_value,
        'total_customers': total_customers,
        'total_products': total_products,
        'recent_orders': recent_orders,
    }

    return render(request, 'dashboard/index.html', context)