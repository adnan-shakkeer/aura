from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache

from .models import Order


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
