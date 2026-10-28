from django.conf import settings
from django.db import models

from admin_panel.products.models import ProductVariant


class WishlistItem(models.Model):
    """A variant saved to a customer's wishlist.

    The variant reference is nullable so a hard-deleted variant does not
    break the wishlist. Identity snapshots are captured at save time and
    are used only when the variant has been hard-deleted.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wishlist_items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wishlist_items",
    )

    product_name = models.CharField(max_length=200, blank=True)
    brand_name = models.CharField(max_length=100, blank=True)
    variant_size = models.CharField(max_length=50, blank=True)

    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "variant"],
                name="unique_wishlist_variant_per_user",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.variant_id and not (
            self.product_name and self.brand_name and self.variant_size
        ):
            variant = ProductVariant.objects.select_related(
                "product__brand"
            ).get(pk=self.variant_id)

            if not self.product_name:
                self.product_name = variant.product.name
            if not self.brand_name:
                self.brand_name = variant.product.brand.name
            if not self.variant_size:
                self.variant_size = variant.size

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product_name or 'Unknown product'} - {self.variant_size or 'Unknown size'}"
