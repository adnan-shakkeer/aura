"""Phase 19.2 coupon service, checkout, usage and display tests."""

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
from user_panel.orders.cancellation import cancel_order, cancel_order_item
from user_panel.orders.coupons import (
    discover_available_coupons,
    release_coupon_usage_for_order,
    validate_coupon,
)
from user_panel.orders.exceptions import (
    CouponPerUserLimitExceededError,
    CouponUsageLimitExceededError,
)
from user_panel.orders.invoices import build_invoice_context
from user_panel.orders.models import CouponUsage, Order
from user_panel.orders.returns import approve_return, request_return
from user_panel.orders.services import (
    STATUS_CREATED,
    place_direct_order,
    place_order,
    review_checkout,
    review_direct_checkout,
)


User = get_user_model()

ZERO = Decimal("0.00")


def make_user(username="phase19-2-user"):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pass1234",
    )


def make_admin(username="phase19-2-admin"):
    return User.objects.create_superuser(
        username=username,
        email=f"{username}@example.com",
        password="pass1234",
    )


def make_variant(*, sku="SKU-19-2", price="1200.00", stock=20):
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
        full_name="Phase Nineteen Two",
        phone_number="9876543210",
        address_line1="192 Coupon Street",
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
        "max_discount_amount": Decimal("150.00"),
        "valid_from": now - timedelta(days=1),
        "valid_until": now + timedelta(days=30),
        "total_usage_limit": None,
        "per_user_limit": 1,
        "is_active": True,
    }
    values.update(overrides)
    return Coupon.objects.create(**values)


def make_bare_order(user):
    return Order.objects.create(
        user=user,
        subtotal=ZERO,
        total=ZERO,
        delivery_full_name="Bare Order",
        delivery_phone_number="9876543210",
        delivery_address_line1="1 Test Street",
        delivery_city="Mumbai",
        delivery_state="Maharashtra",
        delivery_pincode="400001",
        delivery_address_type=Order.DeliveryAddressType.HOME,
    )


class CouponCheckoutTests(TestCase):
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

    def _review_and_place(self, coupon):
        review = review_checkout(
            self.user,
            self.address.pk,
            coupon_code=coupon.code if coupon else None,
        )
        return review, place_order(self.user, self.address.pk, review.token)

    def test_review_and_place_apply_percentage_coupon(self):
        coupon = make_coupon()

        review, result = self._review_and_place(coupon)

        self.assertEqual(review.coupon_code, "SAVE10")
        self.assertEqual(review.coupon_discount_total, Decimal("150.00"))
        self.assertEqual(review.total, Decimal("2250.00"))

        self.assertEqual(result.status, STATUS_CREATED)
        order = result.order
        self.assertEqual(order.coupon_code, "SAVE10")
        self.assertEqual(
            order.coupon_discount_total,
            Decimal("150.00"),
        )
        self.assertEqual(order.discount_total, Decimal("150.00"))
        self.assertEqual(order.total, Decimal("2250.00"))

        item = order.items.get()
        self.assertEqual(
            item.coupon_discount_allocated,
            Decimal("150.00"),
        )
        self.assertEqual(item.paid_amount, Decimal("2250.00"))

        usage = CouponUsage.objects.get(order=order)
        self.assertEqual(usage.status, CouponUsage.Status.CONSUMED)
        self.assertEqual(usage.coupon_code, "SAVE10")
        self.assertEqual(
            usage.discount_amount,
            Decimal("150.00"),
        )

    def test_direct_checkout_applies_coupon_without_touching_cart(self):
        coupon = make_coupon(
            code="DIRECT10",
            max_discount_amount=Decimal("120.00"),
        )

        review = review_direct_checkout(
            self.user,
            self.variant.pk,
            2,
            self.address.pk,
            coupon_code=coupon.code,
        )
        result = place_direct_order(
            self.user,
            self.address.pk,
            review.token,
        )

        self.assertEqual(review.coupon_code, "DIRECT10")
        self.assertEqual(review.coupon_discount_total, Decimal("120.00"))
        self.assertEqual(result.status, STATUS_CREATED)
        order = result.order
        self.assertEqual(order.coupon_code, "DIRECT10")
        self.assertEqual(
            order.coupon_discount_total,
            Decimal("120.00"),
        )
        self.assertEqual(
            CouponUsage.objects.get(order=order).status,
            CouponUsage.Status.CONSUMED,
        )
        self.assertEqual(
            CartItem.objects.get(pk=self.item.pk).quantity,
            2,
        )

    def test_null_per_user_limit_allows_repeat_use(self):
        coupon = make_coupon(per_user_limit=None)

        first_review, first_result = self._review_and_place(coupon)
        self.assertEqual(first_result.status, STATUS_CREATED)
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )

        second_review = review_checkout(
            self.user,
            self.address.pk,
            coupon_code=coupon.code,
        )
        second_result = place_order(
            self.user,
            self.address.pk,
            second_review.token,
        )

        self.assertEqual(first_review.coupon_code, "SAVE10")
        self.assertEqual(second_review.coupon_code, "SAVE10")
        self.assertEqual(second_result.status, STATUS_CREATED)
        self.assertEqual(
            CouponUsage.objects.filter(
                coupon=coupon,
                status=CouponUsage.Status.CONSUMED,
            ).count(),
            2,
        )

    def test_default_per_user_limit_blocks_repeat_use(self):
        coupon = make_coupon()

        _, result = self._review_and_place(coupon)
        self.assertEqual(result.status, STATUS_CREATED)

        with self.assertRaises(CouponPerUserLimitExceededError):
            validate_coupon(
                coupon,
                self.user,
                Decimal("2400.00"),
            )

    def test_total_limit_counts_only_active_usages(self):
        coupon = make_coupon(
            total_usage_limit=1,
            per_user_limit=None,
        )

        _, result = self._review_and_place(coupon)
        self.assertEqual(result.status, STATUS_CREATED)

        with self.assertRaises(CouponUsageLimitExceededError):
            validate_coupon(
                coupon,
                self.user,
                Decimal("2400.00"),
            )

        release_coupon_usage_for_order(result.order)

        validate_coupon(coupon, self.user, Decimal("2400.00"))

        usage = CouponUsage.objects.get(order=result.order)
        self.assertEqual(usage.status, CouponUsage.Status.RELEASED)

    def test_discovery_marks_only_applicable_best_deal(self):
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
        below_minimum = make_coupon(
            code="BIG50",
            discount_value=Decimal("50.00"),
            minimum_purchase=Decimal("5000.00"),
            max_discount_amount=Decimal("5000.00"),
        )

        results = {
            row["code"]: row
            for row in discover_available_coupons(
                self.user,
                Decimal("2400.00"),
            )
        }

        self.assertTrue(results[best.code]["best_deal"])
        self.assertFalse(results[low.code]["best_deal"])
        self.assertFalse(results[below_minimum.code]["best_deal"])
        self.assertFalse(results[below_minimum.code]["applicable"])

    def test_below_minimum_coupon_is_never_best_deal(self):
        make_coupon(
            code="ONLYBIG",
            discount_value=Decimal("90.00"),
            minimum_purchase=Decimal("5000.00"),
            max_discount_amount=Decimal("5000.00"),
        )

        results = discover_available_coupons(
            self.user,
            Decimal("2400.00"),
        )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["applicable"])
        self.assertFalse(results[0]["best_deal"])

    def test_discovery_excludes_exhausted_coupon(self):
        coupon = make_coupon(code="GONE", total_usage_limit=1)
        order = make_bare_order(self.user)
        CouponUsage.objects.create(
            coupon=coupon,
            order=order,
            user=self.user,
            coupon_code=coupon.code,
            status=CouponUsage.Status.CONSUMED,
            discount_amount=ZERO,
        )

        codes = [
            row["code"]
            for row in discover_available_coupons(
                self.user,
                Decimal("2400.00"),
            )
        ]

        self.assertNotIn("GONE", codes)


class CouponCancellationTests(TestCase):
    def setUp(self):
        self.user = make_user("phase19-2-cancel")
        self.variant = make_variant(sku="SKU-19-2-CANCEL")
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        self.item = CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=1,
        )
        self.coupon = make_coupon(code="CANCEL10")

    def _place(self):
        review = review_checkout(
            self.user,
            self.address.pk,
            coupon_code=self.coupon.code,
        )
        result = place_order(self.user, self.address.pk, review.token)
        self.assertEqual(result.status, STATUS_CREATED)
        return result.order

    def test_full_order_cancellation_releases_coupon(self):
        order = self._place()

        cancel_order(self.user, order.pk, reason="Changed my mind")

        usage = CouponUsage.objects.get(order=order)
        self.assertEqual(usage.status, CouponUsage.Status.RELEASED)
        self.assertIsNotNone(usage.released_at)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.OrderStatus.CANCELLED)

    def test_partial_item_cancellation_does_not_release_coupon(self):
        second_variant = make_variant(
            sku="SKU-19-2-CANCEL-2",
            price="800.00",
        )
        CartItem.objects.create(
            cart=self.cart,
            variant=second_variant,
            quantity=1,
        )
        order = self._place()
        first_item = order.items.order_by("id").first()

        cancel_order_item(
            self.user,
            first_item.pk,
            reason="Remove one item",
        )

        usage = CouponUsage.objects.get(order=order)
        self.assertEqual(usage.status, CouponUsage.Status.CONSUMED)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.OrderStatus.PENDING)

    def test_return_request_and_approval_do_not_release_coupon(self):
        order = self._place()
        order.status = Order.OrderStatus.DELIVERED
        order.save(update_fields=["status", "updated_at"])
        item = order.items.get()

        request_return(self.user, item.pk, reason="Not as expected")
        return_item = item.return_item
        approve_return(
            return_item.pk,
            admin=make_admin(),
            note="Approved after inspection",
        )

        usage = CouponUsage.objects.get(order=order)
        self.assertEqual(usage.status, CouponUsage.Status.CONSUMED)


class CouponHistoricalDisplayTests(TestCase):
    def setUp(self):
        self.user = make_user("phase19-2-display")
        self.variant = make_variant(sku="SKU-19-2-DISPLAY")
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )
        self.coupon = make_coupon(code="HISTORY10")
        review = review_checkout(
            self.user,
            self.address.pk,
            coupon_code=self.coupon.code,
        )
        result = place_order(self.user, self.address.pk, review.token)
        self.assertEqual(result.status, STATUS_CREATED)
        self.order = result.order
        self.client.force_login(self.user)

    def test_confirmation_and_order_detail_show_coupon_code(self):
        confirmation = self.client.get(
            reverse(
                "orders:order_confirmation",
                args=[self.order.order_number],
            )
        )
        detail = self.client.get(
            reverse(
                "orders:order_detail",
                args=[self.order.order_number],
            )
        )

        for response in (confirmation, detail):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Coupon Discount (HISTORY10)")
            self.assertContains(response, "150.00")

    def test_admin_order_detail_shows_coupon_code(self):
        admin = make_admin("phase19-2-display-admin")
        self.client.force_login(admin)

        response = self.client.get(
            reverse(
                "admin_orders:detail",
                args=[self.order.order_number],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Coupon Discount (HISTORY10)")

    def test_invoice_context_includes_coupon_code(self):
        context = build_invoice_context(self.order)

        self.assertEqual(context.coupon_code, "HISTORY10")
        self.assertEqual(
            context.coupon_discount_total,
            Decimal("150.00"),
        )
