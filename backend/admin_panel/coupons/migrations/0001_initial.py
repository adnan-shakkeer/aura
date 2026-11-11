# Initial migration for admin_panel.coupons Coupon model

import django.db.models.expressions
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
from django.db.models.functions import Lower


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Coupon",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "code",
                    models.CharField(max_length=40),
                ),
                (
                    "description",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "discount_type",
                    models.CharField(
                        choices=[("PERCENTAGE", "Percentage"), ("FIXED", "Fixed Amount")],
                        max_length=20,
                    ),
                ),
                (
                    "discount_value",
                    models.DecimalField(decimal_places=2, max_digits=10),
                ),
                (
                    "minimum_purchase",
                    models.DecimalField(
                        decimal_places=2, default=0.0, max_digits=12
                    ),
                ),
                (
                    "max_discount_amount",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=12, null=True
                    ),
                ),
                (
                    "valid_from",
                    models.DateTimeField(),
                ),
                (
                    "valid_until",
                    models.DateTimeField(),
                ),
                (
                    "total_usage_limit",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "per_user_limit",
                    models.PositiveIntegerField(blank=True, default=1, null=True),
                ),
                (
                    "is_active",
                    models.BooleanField(default=True),
                ),
                (
                    "is_deleted",
                    models.BooleanField(default=False),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "verbose_name": "Coupon",
                "verbose_name_plural": "Coupons",
            },
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=~models.Q(code=""),
                name="coupon_code_not_empty",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=models.Q(discount_value__gt=0),
                name="coupon_discount_value_gt_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=models.Q(minimum_purchase__gte=0),
                name="coupon_minimum_purchase_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(max_discount_amount__isnull=True)
                    | models.Q(max_discount_amount__gt=0)
                ),
                name="coupon_max_discount_positive_or_null",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(discount_type="FIXED")
                    | models.Q(max_discount_amount__isnull=False)
                ),
                name="coupon_percentage_requires_cap",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(discount_type="FIXED")
                    | models.Q(discount_value__lte=100)
                ),
                name="coupon_percentage_lte_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(discount_type="PERCENTAGE")
                    | models.Q(minimum_purchase__gt=models.F("discount_value"))
                ),
                name="coupon_fixed_min_purchase_gt_discount",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=models.Q(valid_until__gte=models.F("valid_from")),
                name="coupon_valid_until_gte_valid_from",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(total_usage_limit__isnull=True)
                    | models.Q(total_usage_limit__gte=1)
                ),
                name="coupon_total_usage_limit_gte_1_or_null",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(per_user_limit__isnull=True)
                    | models.Q(per_user_limit__gte=1)
                ),
                name="coupon_per_user_limit_gte_1_or_null",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.UniqueConstraint(
                Lower("code"),
                name="unique_coupon_code_ci",
            ),
        ),
    ]
