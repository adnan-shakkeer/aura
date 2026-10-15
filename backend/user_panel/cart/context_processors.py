
"""Shared cart context for user-facing templates.

Provides the header Shopping Bag badge count on every page that uses
``RequestContext`` (i.e. every page rendered via ``django.shortcuts.render``).

The badge represents total units in the cart: SUM of ``quantity`` across
all ``CartItem`` records belonging to the current user's cart — the same
aggregate the cart views already use. Anonymous users get 0 without any
database query.
"""

from django.db.models import Sum

from .models import CartItem


def cart_bag_count(request):
    """Return ``{"cart_bag_count": <total units>}`` for the header badge."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"cart_bag_count": 0}

    try:
        total = (
            CartItem.objects.filter(cart__user=user).aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )
    except Exception:
        return {"cart_bag_count": 0}

    return {"cart_bag_count": total}
