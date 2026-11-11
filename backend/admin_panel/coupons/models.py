from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


ZERO = Decimal("0.00")


class Coupon(models.Model):
    """A single-use-per-order discount code.

    Coupon business rules are intentionally explicit in the model so the
    custom admin can rely on one validation source. Usage rows live in
    ``user_panel.orders.CouponUsage`` and are deliberately not owned here.
    """

    class DiscountType(models.TextChoices):
        PERCENTAGE = "PERCENTAGE", "Percentage"
        FIXED = "FIXED", "Fixed Amount"

    code = models.CharField(max_length=40)
    description = models.CharField(max_length=255, blank=True, default="")
    discount_type = models.CharField(
        max_length=20,
        choices=DiscountType.choices,
    )
    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    minimum_purchase = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
    )
    max_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    total_usage_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    per_user_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=1,
    )
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Coupon"
        verbose_name_plural = "Coupons"
        constraints = [
            models.UniqueConstraint(
                Lower("code"),
                name="unique_coupon_code_ci",
            ),
            models.CheckConstraint(
                condition=~models.Q(code=""),
                name="coupon_code_not_empty",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_value__gt=0),
                name="coupon_discount_value_gt_0",
            ),
            models.CheckConstraint(
                condition=models.Q(minimum_purchase__gte=0),
                name="coupon_minimum_purchase_gte_0",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(max_discount_amount__isnull=True)
                    | models.Q(max_discount_amount__gt=0)
                ),
                name="coupon_max_discount_positive_or_null",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(discount_type="FIXED")
                    | models.Q(max_discount_amount__isnull=False)
                ),
                name="coupon_percentage_requires_cap",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(discount_type="FIXED")
                    | models.Q(discount_value__lte=100)
                ),
                name="coupon_percentage_lte_100",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(discount_type="PERCENTAGE")
                    | models.Q(minimum_purchase__gt=models.F("discount_value"))
                ),
                name="coupon_fixed_min_purchase_gt_discount",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__gte=models.F("valid_from")),
                name="coupon_valid_until_gte_valid_from",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(total_usage_limit__isnull=True)
                    | models.Q(total_usage_limit__gte=1)
                ),
                name="coupon_total_usage_limit_gte_1_or_null",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(per_user_limit__isnull=True)
                    | models.Q(per_user_limit__gte=1)
                ),
                name="coupon_per_user_limit_gte_1_or_null",
            ),
        ]

    def clean(self):
        super().clean()

        self.code = normalize_coupon_code(self.code)
        errors = {}

        if not self.code:
            errors["code"] = "Coupon code is required."

        if self.discount_type == self.DiscountType.PERCENTAGE:
            if self.discount_value is None or self.discount_value <= ZERO:
                errors["discount_value"] = (
                    "Percentage discount must be greater than zero."
                )
            elif self.discount_value > Decimal("100"):
                errors["discount_value"] = (
                    "Percentage discount cannot exceed 100%."
                )

            if (
                self.max_discount_amount is None
                or self.max_discount_amount <= ZERO
            ):
                errors["max_discount_amount"] = (
                    "A maximum discount amount is required for "
                    "percentage coupons."
                )
        elif self.discount_type == self.DiscountType.FIXED:
            self.max_discount_amount = None

            if self.discount_value is None or self.discount_value <= ZERO:
                errors["discount_value"] = (
                    "Fixed discount must be greater than zero."
                )
            elif (
                self.minimum_purchase is not None
                and self.minimum_purchase <= self.discount_value
            ):
                errors["minimum_purchase"] = (
                    "Minimum order amount must be greater than the "
                    "fixed discount amount."
                )
        else:
            errors["discount_type"] = "Please select a valid discount type."

        if (
            self.minimum_purchase is None
            or self.minimum_purchase < ZERO
        ):
            errors["minimum_purchase"] = (
                "Minimum order amount cannot be negative."
            )

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

        for field_name in ("total_usage_limit", "per_user_limit"):
            value = getattr(self, field_name)
            if value is not None and value < 1:
                errors[field_name] = "Usage limits must be at least 1."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.code = normalize_coupon_code(self.code)
        super().save(*args, **kwargs)

    @property
    def status(self):
        now = timezone.now()

        if self.valid_until is not None and now > self.valid_until:
            return "Expired"
        if not self.is_active:
            return "Inactive"
        if self.valid_from is not None and now < self.valid_from:
            return "Scheduled"
        return "Active"

    def is_valid_at(self, moment=None):
        moment = moment or timezone.now()
        return (
            not self.is_deleted
            and self.is_active
            and self.valid_from <= moment <= self.valid_until
        )

    def __str__(self):
        return self.code


def normalize_coupon_code(value):
    return (value or "").strip().upper()
