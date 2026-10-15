
from django.conf import settings
from django.db import models
from django.db.models import Q

from admin_panel.products.models import ProductVariant


MAX_QUANTITY_PER_VARIANT = 10


class Cart(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cart",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart for {self.user}"


class CartItem(models.Model):
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        related_name="cart_items",
    )

    quantity = models.PositiveIntegerField(default=1)

    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["added_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "variant"],
                name="unique_variant_per_cart",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=1, quantity__lte=MAX_QUANTITY_PER_VARIANT),
                name="cart_item_quantity_1_to_10",
            ),
        ]

    def __str__(self):
        return f"{self.variant} x {self.quantity}"
