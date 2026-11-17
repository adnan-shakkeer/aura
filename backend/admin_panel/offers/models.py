from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from admin_panel.offer_utils import OFFER_MAX, OFFER_QUANTUM


OFFER_TIME_ZONE_NAME = "Asia/Kolkata"
OFFER_TIME_ZONE = ZoneInfo(OFFER_TIME_ZONE_NAME)
OFFER_TIME_ZONE_LABEL = "IST (Asia/Kolkata)"


def offer_today():
    """Return the current offer date in the admin-facing IST timezone."""
    return timezone.now().astimezone(OFFER_TIME_ZONE).date()


class Offer(models.Model):
    """A percentage discount applied to one product or one category.

    Offers are authoritative for storefront pricing. Soft-deleted offers are
    excluded by every resolution and listing query. Multiple offers may target
    the same product/category; the highest eligible percentage wins, with a
    product offer winning an equal tie against a category offer.
    """

    class TargetType(models.TextChoices):
        PRODUCT = "PRODUCT", "Product"
        CATEGORY = "CATEGORY", "Category"

    name = models.CharField(max_length=150)
    target_type = models.CharField(
        max_length=10,
        choices=TargetType.choices,
    )
    product = models.ForeignKey(
        "products.Product",
        on_delete=models.PROTECT,
        related_name="offers",
        null=True,
        blank=True,
    )
    category = models.ForeignKey(
        "categories.Category",
        on_delete=models.PROTECT,
        related_name="offers",
        null=True,
        blank=True,
    )
    percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[
            MinValueValidator(OFFER_QUANTUM),
            MaxValueValidator(OFFER_MAX),
        ],
    )
    valid_from = models.DateField()
    valid_until = models.DateField()
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Offer"
        verbose_name_plural = "Offers"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(name=""),
                name="offer_name_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        target_type="PRODUCT",
                        product__isnull=False,
                        category__isnull=True,
                    )
                    | models.Q(
                        target_type="CATEGORY",
                        product__isnull=True,
                        category__isnull=False,
                    )
                ),
                name="offer_target_matches_type",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    percentage__gte=OFFER_QUANTUM,
                    percentage__lte=OFFER_MAX,
                ),
                name="offer_percentage_between_0_and_99",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__gte=models.F("valid_from")),
                name="offer_valid_until_gte_valid_from",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "is_deleted",
                    "is_active",
                    "target_type",
                    "valid_from",
                    "valid_until",
                ],
                name="offer_lookup_idx",
            ),
            models.Index(
                fields=["product", "is_deleted", "is_active"],
                name="offer_product_idx",
            ),
            models.Index(
                fields=["category", "is_deleted", "is_active"],
                name="offer_category_idx",
            ),
        ]

    def clean(self):
        super().clean()

        self.name = (self.name or "").strip()
        errors = {}

        if not self.name:
            errors["name"] = "Offer name is required."

        if self.target_type not in self.TargetType.values:
            errors["target_type"] = "Please select a valid offer target type."
        elif self.target_type == self.TargetType.PRODUCT:
            if not self.product_id:
                errors["product"] = "Please select a product."
            if self.category_id:
                errors["category"] = (
                    "A product offer cannot also target a category."
                )
        elif self.target_type == self.TargetType.CATEGORY:
            if not self.category_id:
                errors["category"] = "Please select a category."
            if self.product_id:
                errors["product"] = (
                    "A category offer cannot also target a product."
                )

        if (
            self.percentage is None
            or self.percentage < OFFER_QUANTUM
            or self.percentage > OFFER_MAX
        ):
            errors["percentage"] = "Offer must be between 0.01% and 99%."

        if self.valid_from is None:
            errors["valid_from"] = "Valid from date is required."
        if self.valid_until is None:
            errors["valid_until"] = "Valid until date is required."
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until < self.valid_from
        ):
            errors["valid_until"] = (
                "Valid until cannot be earlier than valid from."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        super().save(*args, **kwargs)

    @property
    def status(self):
        if not self.is_active:
            return "Inactive"

        today = offer_today()
        if self.valid_from is not None and today < self.valid_from:
            return "Scheduled"
        if self.valid_until is not None and today > self.valid_until:
            return "Expired"
        return "Active"

    @property
    def target_label(self):
        if self.target_type == self.TargetType.PRODUCT and self.product_id:
            return self.product.name
        if self.target_type == self.TargetType.CATEGORY and self.category_id:
            return self.category.name
        return "—"

    def is_valid_on(self, day=None):
        day = day or offer_today()
        return (
            not self.is_deleted
            and self.is_active
            and self.valid_from <= day <= self.valid_until
        )

    def __str__(self):
        return self.name
