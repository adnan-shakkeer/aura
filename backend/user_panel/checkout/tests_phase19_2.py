"""Phase 19.2 checkout coupon UI and session-isolation tests."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.coupons.models import Coupon
from admin_panel.products.models import Product, ProductVariant
from user_panel.address.models import Address
from user_panel.cart.models import Cart, CartItem
from user_panel.orders.models import Order

from .views import (
    COUPON_SESSION_KEY,
    DIRECT_COUPON_SESSION_KEY,
    DIRECT_INTENT_SESSION_KEY,
)


User = get_user_model()

CHECKOUT_URL = reverse("checkout:checkout_review")
APPLY_URL = reverse("checkout:apply_coupon")
REMOVE_URL = reverse("checkout:remove_coupon")
AVAILABLE_URL = reverse("checkout:available_coupons")
BUY_NOW_REVIEW_URL = reverse("checkout:buy_now_review")
PLACE_ORDER_URL = reverse("checkout:place_order")


def make_user(username="phase19-2-checkout"):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pass1234",
    )


def make_variant(*, sku="SKU-19-2-CHECKOUT", price="1200.00", stock=20):
    category = Category.objects.create(
        name=f"Category {sku}",
        description="Category description",
    )
    brand = Brand.objects.create(
        category=category,
        name=f"Brand {sku}",
        description="Brand description",
        listing_image="brands/listing/test.jpg",
        listing_description="Listing description",
        hero_image="brands/hero/test.jpg",
        hero_description="Hero description",
    )
    product = Product.objects.create(
        brand=brand,
        name=f"Product {sku}",
        description="Product description",
        fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
    )
    return ProductVariant.objects.create(
        product=product,
        size="50ml",
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )


def make_address(user):
    return Address.objects.create(
        user=user,
        full_name="Phase Nineteen Checkout",
        phone_number="9876543210",
        address_line1="192 Checkout Street",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        address_type="HOME",
        is_default=True,
    )


def make_coupon(**overrides):
    now = timezone.now()
    values = {
        "code": "SAVE10",
        "description": "Ten percent off",
        "discount_type": Coupon.DiscountType.PERCENTAGE,
        "discount_value": Decimal("10.00"),
        "minimum_purchase": Decimal("0.00"),
        "max_discount_amount": Decimal("300.00"),
        "valid_from": now - timedelta(days=1),
        "valid_until": now + timedelta(days=30),
        "total_usage_limit": None,
        "per_user_limit": 1,
        "is_active": True,
    }
    values.update(overrides)
    return Coupon.objects.create(**values)


class CheckoutCouponTestBase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.variant = make_variant()
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        self.item = CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )
        self.client.force_login(self.user)

    def set_session(self, **values):
        session = self.client.session
        session.update(values)
        session.save()

    def direct_intent(self, quantity=1):
        return {
            "variant_id": self.variant.pk,
            "quantity": quantity,
            "product_slug": self.variant.product.slug,
            "iat": int(timezone.now().timestamp()),
        }

    def apply(self, code, *, source="cart"):
        return self.client.post(
            APPLY_URL,
            {"coupon_code": code, "source": source},
        )


class CheckoutCouponApplicationTests(CheckoutCouponTestBase):
    def test_apply_valid_coupon_sets_cart_session_and_shows_applied_state(self):
        make_coupon(code="SAVE10")

        response = self.apply("save10")
        follow = self.client.get(response.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.session[COUPON_SESSION_KEY],
            "SAVE10",
        )
        self.assertEqual(follow.context["review"].coupon_code, "SAVE10")
        self.assertContains(follow, "Coupon Applied")
        self.assertContains(follow, "Replace")
        self.assertContains(follow, "Remove")

    def test_second_valid_coupon_replaces_existing_coupon(self):
        make_coupon(code="SAVE10")
        make_coupon(
            code="SAVE20",
            discount_value=Decimal("20.00"),
            max_discount_amount=Decimal("500.00"),
        )
        self.apply("SAVE10")

        response = self.apply("SAVE20")
        follow = self.client.get(response.url)

        self.assertEqual(
            self.client.session[COUPON_SESSION_KEY],
            "SAVE20",
        )
        self.assertContains(follow, "changed to SAVE20")
        self.assertContains(follow, "Coupon Applied")
        self.assertContains(follow, "SAVE20")

    def test_invalid_replacement_keeps_existing_coupon(self):
        make_coupon(code="SAVE10")
        self.apply("SAVE10")

        response = self.apply("DOESNOTEXIST")
        follow = self.client.get(response.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.session[COUPON_SESSION_KEY],
            "SAVE10",
        )
        self.assertContains(follow, "not valid")
        self.assertContains(follow, "Coupon Applied")
        self.assertContains(follow, "SAVE10")

    def test_remove_only_clears_the_active_flow_coupon(self):
        self.set_session(
            **{
                COUPON_SESSION_KEY: "CARTCODE",
                DIRECT_COUPON_SESSION_KEY: "DIRECTCODE",
            }
        )

        response = self.client.post(
            REMOVE_URL,
            {"source": "cart"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(COUPON_SESSION_KEY, self.client.session)
        self.assertEqual(
            self.client.session[DIRECT_COUPON_SESSION_KEY],
            "DIRECTCODE",
        )

    def test_direct_apply_does_not_change_cart_coupon(self):
        make_coupon(code="SAVE10")
        make_coupon(code="DIRECT20", discount_value=Decimal("20.00"))
        self.set_session(
            **{
                COUPON_SESSION_KEY: "CARTCODE",
                DIRECT_INTENT_SESSION_KEY: self.direct_intent(),
            }
        )

        response = self.apply("DIRECT20", source="direct")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.session[DIRECT_COUPON_SESSION_KEY],
            "DIRECT20",
        )
        self.assertEqual(
            self.client.session[COUPON_SESSION_KEY],
            "CARTCODE",
        )


class CheckoutCouponSessionIsolationTests(CheckoutCouponTestBase):
    def test_cart_checkout_preserves_direct_coupon_session(self):
        self.set_session(
            **{
                DIRECT_COUPON_SESSION_KEY: "DIRECTCODE",
                DIRECT_INTENT_SESSION_KEY: self.direct_intent(),
            }
        )

        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.session[DIRECT_COUPON_SESSION_KEY],
            "DIRECTCODE",
        )

    def test_buy_now_review_preserves_cart_coupon_session(self):
        self.set_session(
            **{
                COUPON_SESSION_KEY: "CARTCODE",
                DIRECT_INTENT_SESSION_KEY: self.direct_intent(),
            }
        )

        response = self.client.get(BUY_NOW_REVIEW_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.session[COUPON_SESSION_KEY],
            "CARTCODE",
        )

    def test_stale_cart_coupon_is_cleared_when_review_rejects_it(self):
        self.set_session(**{COUPON_SESSION_KEY: "MISSING"})

        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["review"].coupon_code, "")
        self.assertNotIn(COUPON_SESSION_KEY, self.client.session)

    def test_stale_direct_coupon_is_cleared_when_review_rejects_it(self):
        self.set_session(
            **{
                DIRECT_COUPON_SESSION_KEY: "MISSING",
                DIRECT_INTENT_SESSION_KEY: self.direct_intent(),
            }
        )

        response = self.client.get(BUY_NOW_REVIEW_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["review"].coupon_code, "")
        self.assertNotIn(DIRECT_COUPON_SESSION_KEY, self.client.session)

    def test_invalid_coupon_at_placement_clears_only_its_flow(self):
        coupon = make_coupon(code="SAVE10")
        self.set_session(
            **{
                DIRECT_COUPON_SESSION_KEY: "DIRECTCODE",
                DIRECT_INTENT_SESSION_KEY: self.direct_intent(),
            }
        )
        self.apply("SAVE10")
        self.client.get(CHECKOUT_URL)
        coupon.is_active = False
        coupon.save(update_fields=["is_active", "updated_at"])

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 0)
        self.assertNotIn(COUPON_SESSION_KEY, self.client.session)
        self.assertEqual(
            self.client.session[DIRECT_COUPON_SESSION_KEY],
            "DIRECTCODE",
        )

        follow = self.client.get(response.url)
        self.assertContains(follow, "no longer available")


class AvailableCouponsEndpointTests(CheckoutCouponTestBase):
    def test_endpoint_marks_only_applicable_best_deal(self):
        low = make_coupon(
            code="LOW5",
            discount_value=Decimal("5.00"),
            max_discount_amount=Decimal("500.00"),
        )
        best = make_coupon(
            code="BEST20",
            discount_value=Decimal("20.00"),
            max_discount_amount=Decimal("500.00"),
        )
        below = make_coupon(
            code="BIG50",
            discount_value=Decimal("50.00"),
            minimum_purchase=Decimal("5000.00"),
            max_discount_amount=Decimal("5000.00"),
        )

        response = self.client.get(
            AVAILABLE_URL,
            {"source": "cart"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        results = {row["code"]: row for row in payload["results"]}

        self.assertEqual(payload["eligible_amount"], "2400.00")
        self.assertTrue(results[best.code]["best_deal"])
        self.assertTrue(results[best.code]["applicable"])
        self.assertFalse(results[low.code]["best_deal"])
        self.assertFalse(results[below.code]["best_deal"])
        self.assertFalse(results[below.code]["applicable"])
        self.assertEqual(
            results[below.code]["state_label"],
            "MIN ORDER NOT MET",
        )

    def test_endpoint_requires_authentication(self):
        self.client.logout()

        response = self.client.get(AVAILABLE_URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("user_auth:login"), response.url)
