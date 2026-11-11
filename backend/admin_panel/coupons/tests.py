"""Phase 19.2 coupon model and custom admin tests."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Coupon


User = get_user_model()


def make_coupon(**overrides):
    now = timezone.now()
    values = {
        "code": "SAVE10",
        "description": "Ten percent off",
        "discount_type": Coupon.DiscountType.PERCENTAGE,
        "discount_value": Decimal("10.00"),
        "minimum_purchase": Decimal("0.00"),
        "max_discount_amount": Decimal("500.00"),
        "valid_from": now - timedelta(days=1),
        "valid_until": now + timedelta(days=30),
        "total_usage_limit": None,
        "per_user_limit": 1,
        "is_active": True,
    }
    values.update(overrides)
    return Coupon.objects.create(**values)


def make_admin(username="coupon-admin"):
    return User.objects.create_superuser(
        username=username,
        email=f"{username}@example.com",
        password="pass1234",
    )


class CouponModelTests(TestCase):
    def test_code_is_normalized_and_saved_uppercase(self):
        coupon = make_coupon(code="  save10  ")

        coupon.refresh_from_db()

        self.assertEqual(coupon.code, "SAVE10")

    def test_percentage_coupon_requires_maximum_discount(self):
        now = timezone.now()
        coupon = Coupon(
            code="NOCAP",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            minimum_purchase=Decimal("0.00"),
            max_discount_amount=None,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=1),
        )

        with self.assertRaises(ValidationError) as raised:
            coupon.full_clean()

        self.assertIn("max_discount_amount", raised.exception.message_dict)

    def test_fixed_coupon_minimum_purchase_must_exceed_discount(self):
        now = timezone.now()
        coupon = Coupon(
            code="FLAT500",
            discount_type=Coupon.DiscountType.FIXED,
            discount_value=Decimal("500.00"),
            minimum_purchase=Decimal("500.00"),
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=1),
        )

        with self.assertRaises(ValidationError) as raised:
            coupon.full_clean()

        self.assertIn("minimum_purchase", raised.exception.message_dict)

    def test_validity_boundaries_are_inclusive(self):
        start = timezone.now()
        end = start + timedelta(days=1)
        coupon = make_coupon(
            code="BOUNDARY",
            valid_from=start,
            valid_until=end,
        )

        self.assertTrue(coupon.is_valid_at(start))
        self.assertTrue(coupon.is_valid_at(end))

    def test_null_per_user_limit_is_allowed(self):
        coupon = make_coupon(code="UNLIMITED", per_user_limit=None)

        coupon.full_clean()

        self.assertIsNone(coupon.per_user_limit)


class CouponAdminViewTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.client.force_login(self.admin)

    def _form_data(self, **overrides):
        data = {
            "code": "new10",
            "description": "New coupon",
            "discount_type": Coupon.DiscountType.PERCENTAGE,
            "discount_value": "10.00",
            "minimum_purchase": "0.00",
            "max_discount_amount": "250.00",
            "valid_from": "2026-01-01T00:00",
            "valid_until": "2026-12-31T23:59",
            "total_usage_limit": "100",
            "per_user_limit": "1",
            "is_active": "on",
        }
        data.update(overrides)
        return data

    def test_create_coupon_persists_normalized_code(self):
        response = self.client.post(
            reverse("coupons:create"),
            self._form_data(),
        )

        self.assertRedirects(response, reverse("coupons:list"))
        coupon = Coupon.objects.get(code="NEW10")
        self.assertEqual(coupon.discount_value, Decimal("10.00"))
        self.assertEqual(coupon.max_discount_amount, Decimal("250.00"))
        self.assertEqual(coupon.total_usage_limit, 100)

    def test_duplicate_code_is_rejected(self):
        make_coupon(code="NEW10")

        response = self.client.post(
            reverse("coupons:create"),
            self._form_data(code="new10"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(Coupon.objects.filter(code="NEW10").count(), 1)

    def test_edit_form_renders_datetime_local_values(self):
        coupon = make_coupon(code="EDITME", is_active=False)
        expected_from = timezone.localtime(coupon.valid_from).strftime(
            "%Y-%m-%dT%H:%M"
        )
        expected_until = timezone.localtime(coupon.valid_until).strftime(
            "%Y-%m-%dT%H:%M"
        )

        response = self.client.get(
            reverse("coupons:edit", args=[coupon.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{expected_from}"')
        self.assertContains(response, f'value="{expected_until}"')
        self.assertNotContains(response, "if values.valid_from")

    def test_toggle_and_soft_delete(self):
        coupon = make_coupon(code="TOGGLEME", is_active=True)

        self.client.post(reverse("coupons:toggle", args=[coupon.pk]))
        coupon.refresh_from_db()
        self.assertFalse(coupon.is_active)

        self.client.post(reverse("coupons:delete", args=[coupon.pk]))
        coupon.refresh_from_db()
        self.assertTrue(coupon.is_deleted)
        self.assertFalse(coupon.is_active)

    def test_list_shows_coupon_details(self):
        make_coupon(
            code="LISTME",
            description="Listed coupon",
            per_user_limit=2,
        )

        response = self.client.get(reverse("coupons:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LISTME")
        self.assertContains(response, "10.00")
        self.assertContains(response, "500.00")
