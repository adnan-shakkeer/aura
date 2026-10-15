
"""User-side cart operations.

Handles cart retrieval, variant additions, quantity updates,
item removal, and checkout eligibility validation.

Cart visibility and purchasability are separate:
unavailable items remain in the cart until the customer removes them,
but their quantities cannot be changed.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from admin_panel.products.models import ProductVariant

from .models import Cart, CartItem, MAX_QUANTITY_PER_VARIANT


class CartServiceError(Exception):
    """Raised when a cart operation violates a business rule."""


def _require_active_user(user):
    """Ensure cart operations are performed by an active authenticated user."""
    if not user or not user.is_authenticated:
        raise CartServiceError("Please log in to use your cart.")

    if not user.is_active:
        raise CartServiceError("Your account is not active.")


def get_cart_for_user(user):
    """Return the authenticated user's cart, creating it when necessary."""
    _require_active_user(user)
    cart, _created = Cart.objects.get_or_create(user=user)
    return cart


def _get_eligible_variant(variant_id, *, lock=False):
    """Return a variant whose entire catalogue hierarchy is eligible."""
    queryset = ProductVariant.objects.select_related(
        "product__brand__category"
    )

    if lock:
        queryset = queryset.select_for_update()

    try:
        return queryset.get(
            pk=variant_id,
            is_deleted=False,
            is_listed=True,
            product__is_deleted=False,
            product__is_listed=True,
            product__brand__is_deleted=False,
            product__brand__is_listed=True,
            product__brand__category__is_deleted=False,
            product__brand__category__is_listed=True,
        )
    except (
        ProductVariant.DoesNotExist,
        ValidationError,
        ValueError,
        TypeError,
    ):
        raise CartServiceError(
            "This product variant is unavailable."
        ) from None


def _validate_quantity(quantity):
    """Return a valid whole-number quantity within the configured limit."""
    if isinstance(quantity, bool):
        raise CartServiceError("Please provide a valid quantity.")

    if isinstance(quantity, int):
        value = quantity
    elif isinstance(quantity, str) and quantity.strip().isdecimal():
        value = int(quantity.strip())
    else:
        raise CartServiceError("Please provide a valid quantity.")

    if not 1 <= value <= MAX_QUANTITY_PER_VARIANT:
        raise CartServiceError(
            f"Quantity must be between 1 and "
            f"{MAX_QUANTITY_PER_VARIANT}."
        )

    return value


def _validate_stock(variant, quantity):
    """Ensure the requested quantity is available in stock."""
    if variant.stock < 1:
        raise CartServiceError("This variant is currently out of stock.")

    if quantity > variant.stock:
        raise CartServiceError(
            f"Only {variant.stock} unit(s) are currently available."
        )


@transaction.atomic
def add_variant_to_cart(user, variant_id, quantity=1):
    """Add a variant or increase its existing cart-item quantity."""
    _require_active_user(user)
    quantity = _validate_quantity(quantity)

    cart = get_cart_for_user(user)
    cart = Cart.objects.select_for_update().get(pk=cart.pk)

    variant = _get_eligible_variant(variant_id, lock=True)
    _validate_stock(variant, quantity)

    item = CartItem.objects.select_for_update().filter(
        cart=cart,
        variant=variant,
    ).first()

    if item:
        quantity = _validate_quantity(item.quantity + quantity)
        _validate_stock(variant, quantity)

        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
    else:
        item = CartItem.objects.create(
            cart=cart,
            variant=variant,
            quantity=quantity,
        )

    cart.save(update_fields=["updated_at"])
    return item


@transaction.atomic
def update_cart_item_quantity(user, item_id, quantity):
    """Update quantity only when the variant is eligible and in stock."""
    _require_active_user(user)
    quantity = _validate_quantity(quantity)

    cart = Cart.objects.select_for_update().filter(user=user).first()
    if cart is None:
        raise CartServiceError("Your cart could not be found.")

    item = CartItem.objects.select_for_update().filter(
        pk=item_id,
        cart=cart,
    ).first()
    if item is None:
        raise CartServiceError("This cart item could not be found.")

    variant = _get_eligible_variant(item.variant_id, lock=True)
    _validate_stock(variant, quantity)

    item.quantity = quantity
    item.save(update_fields=["quantity", "updated_at"])
    cart.save(update_fields=["updated_at"])
    return item


@transaction.atomic
def remove_cart_item(user, item_id):
    """Remove an item from the authenticated user's cart."""
    _require_active_user(user)

    cart = Cart.objects.select_for_update().filter(user=user).first()
    if cart is None:
        raise CartServiceError("Your cart could not be found.")

    item = CartItem.objects.select_for_update().filter(
        pk=item_id,
        cart=cart,
    ).first()
    if item is None:
        raise CartServiceError("This cart item could not be found.")

    item.delete()
    cart.save(update_fields=["updated_at"])


@transaction.atomic
def validate_cart_for_checkout(user):
    """Return checkout problems for unavailable or insufficiently stocked items."""
    _require_active_user(user)

    cart = Cart.objects.filter(user=user).first()
    if cart is None:
        return ["Your cart is empty."]

    items = list(
        cart.items.select_related(
            "variant__product__brand__category"
        ).order_by("added_at", "id")
    )
    if not items:
        return ["Your cart is empty."]

    problems = []

    for item in items:
        try:
            variant = _get_eligible_variant(item.variant_id, lock=True)
        except CartServiceError:
            problems.append(
                f"{item.variant} is no longer available. "
                "Remove it from your cart."
            )
            continue

        if variant.stock < item.quantity:
            if variant.stock == 0:
                problems.append(
                    f"{variant} is out of stock. Remove it from your cart."
                )
            else:
                problems.append(
                    f"Only {variant.stock} unit(s) of {variant} remain available."
                )

    return problems
