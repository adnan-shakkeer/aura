"""Phase 19.1: pricing snapshots, discounts and financial adjustments."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant
from user_panel.address.models import Address
from user_panel.cart.models import Cart, CartItem
from user_panel.orders.financials import get_order_financials
from user_panel.orders.invoices import build_invoice_context
from user_panel.orders.models import Order, OrderItem
from user_panel.orders.returns import approve_return, request_return
from user_panel.orders.services import (
    REASON_PRICE_CHANGED,
    STATUS_CREATED,
    STATUS_REVIEW_REQUIRED,
    place_direct_order,
    place_order,
    review_checkout,
    review_direct_checkout,
)

User = get_user_model()

ZERO = Decimal("0.00")


def make_user(username="phase19-user", **overrides):
    values = {
        "username": username,
        "email": f"{username}@example.com",
        "password": "pass1234",
    }
    values.update(overrides)
    return User.objects.create_user(**values)


def make_catalogue(
    *,
    sku="SKU-19-1",
    price="1200.00",
    stock=10,
    product_offer="0.00",
    category_offer="0.00",
):
    category = Category.objects.create(
        name=f"Category {sku}",
        description="Category description",
        offer_percentage=Decimal(category_offer),
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
        offer_percentage=Decimal(product_offer),
    )
    variant = ProductVariant.objects.create(
        product=product,
        size="50ml",
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )
    return category, brand, product, variant


def make_address(user):
    return Address.objects.create(
        user=user,
        full_name="Phase Nineteen",
        phone_number="9876543210",
        address_line1="19 Pricing Street",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        address_type="HOME",
    )


class CheckoutOfferTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.category, self.brand, self.product, self.variant = make_catalogue(
            product_offer="10.00"
        )
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        self.cart_item = CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )

    def test_review_and_place_persist_offer_snapshots(self):
        review = review_checkout(self.user, self.address.pk)

        self.assertEqual(review.subtotal, Decimal("2400.00"))
        self.assertEqual(review.offer_discount_total, Decimal("240.00"))
        self.assertEqual(review.coupon_discount_total, ZERO)
        self.assertEqual(review.discount_total, Decimal("240.00"))
        self.assertEqual(review.total, Decimal("2160.00"))

        line = review.lines[0]
        self.assertEqual(line.offer_source, OrderItem.OfferSource.PRODUCT)
        self.assertEqual(line.offer_percentage, Decimal("10.00"))
        self.assertEqual(line.offer_discount, Decimal("240.00"))
        self.assertEqual(line.paid_amount, Decimal("2160.00"))

        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_CREATED)
        order = result.order
        self.assertEqual(order.subtotal, Decimal("2400.00"))
        self.assertEqual(order.offer_discount_total, Decimal("240.00"))
        self.assertEqual(order.coupon_discount_total, ZERO)
        self.assertEqual(order.discount_total, Decimal("240.00"))
        self.assertEqual(order.total, Decimal("2160.00"))

        item = order.items.get()
        self.assertEqual(item.offer_source, OrderItem.OfferSource.PRODUCT)
        self.assertEqual(item.offer_percentage_applied, Decimal("10.00"))
        self.assertEqual(item.offer_discount_allocated, Decimal("240.00"))
        self.assertEqual(item.coupon_discount_allocated, ZERO)
        self.assertEqual(item.discount_allocated, Decimal("240.00"))
        self.assertEqual(item.paid_amount, Decimal("2160.00"))

    def test_offer_change_requires_review_before_placing(self):
        review = review_checkout(self.user, self.address.pk)

        self.product.offer_percentage = Decimal("20.00")
        self.product.save(update_fields=["offer_percentage"])

        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_PRICE_CHANGED)
        self.assertEqual(result.review.offer_discount_total, Decimal("480.00"))
        self.assertEqual(result.review.total, Decimal("1920.00"))
        self.assertFalse(
            Order.objects.filter(idempotency_key=review.submission_id).exists()
        )

    def test_direct_offer_change_requires_review_before_placing(self):
        review = review_direct_checkout(
            self.user,
            self.variant.pk,
            2,
            self.address.pk,
        )

        self.category.offer_percentage = Decimal("25.00")
        self.category.save(update_fields=["offer_percentage"])

        result = place_direct_order(
            self.user,
            self.address.pk,
            review.token,
        )

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_PRICE_CHANGED)
        self.assertEqual(result.review.offer_discount_total, Decimal("600.00"))
        self.assertFalse(
            Order.objects.filter(idempotency_key=review.submission_id).exists()
        )


class ZeroOfferRegressionTests(TestCase):
    def test_order_without_offers_keeps_existing_financial_outcome(self):
        user = make_user("phase19-zero")
        _, _, _, variant = make_catalogue(sku="SKU-19-ZERO")
        address = make_address(user)
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, variant=variant, quantity=2)

        review = review_checkout(user, address.pk)
        result = place_order(user, address.pk, review.token)

        self.assertEqual(result.status, STATUS_CREATED)
        order = result.order
        self.assertEqual(order.subtotal, Decimal("2400.00"))
        self.assertEqual(order.offer_discount_total, ZERO)
        self.assertEqual(order.coupon_discount_total, ZERO)
        self.assertEqual(order.discount_total, ZERO)
        self.assertEqual(order.total, Decimal("2400.00"))

        item = order.items.get()
        self.assertEqual(item.offer_source, OrderItem.OfferSource.NONE)
        self.assertEqual(item.discount_allocated, ZERO)
        self.assertEqual(item.paid_amount, Decimal("2400.00"))


class SnapshotFreezeTests(TestCase):
    def setUp(self):
        self.user = make_user("phase19-freeze")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            sku="SKU-19-FREEZE",
            product_offer="10.00",
        )
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )
        review = review_checkout(self.user, self.address.pk)
        self.order = place_order(
            self.user,
            self.address.pk,
            review.token,
        ).order

    def test_catalogue_changes_do_not_alter_placed_order(self):
        self.product.offer_percentage = Decimal("50.00")
        self.product.save(update_fields=["offer_percentage"])
        self.variant.price = Decimal("9999.00")
        self.variant.save(update_fields=["price"])
        self.category.offer_percentage = Decimal("40.00")
        self.category.save(update_fields=["offer_percentage"])

        self.order.refresh_from_db()
        item = self.order.items.get()
        self.assertEqual(self.order.offer_discount_total, Decimal("240.00"))
        self.assertEqual(self.order.total, Decimal("2160.00"))
        self.assertEqual(item.line_total, Decimal("2400.00"))
        self.assertEqual(item.offer_discount_allocated, Decimal("240.00"))
        self.assertEqual(item.paid_amount, Decimal("2160.00"))

        context = build_invoice_context(self.order)
        self.assertEqual(context.subtotal, Decimal("2400.00"))
        self.assertEqual(context.offer_discount_total, Decimal("240.00"))
        self.assertEqual(context.coupon_discount_total, ZERO)
        self.assertEqual(context.discount_total, Decimal("240.00"))
        self.assertEqual(context.total, Decimal("2160.00"))

    def test_order_detail_uses_persisted_snapshot(self):
        self.client.force_login(self.user)
        url = reverse(
            "orders:order_detail",
            kwargs={"order_number": self.order.order_number},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Offer Discount")
        self.assertContains(response, "-₹240.00")


class FinancialAdjustmentTests(TestCase):
    def setUp(self):
        self.user = make_user("phase19-fin")
        self.admin = make_user(
            "phase19-admin",
            is_staff=True,
            is_superuser=True,
        )
        self.category, self.brand, self.product, self.variant = make_catalogue(
            sku="SKU-19-FIN",
            price="1200.00",
            product_offer="10.00",
        )
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )
        review = review_checkout(self.user, self.address.pk)
        self.order = place_order(
            self.user,
            self.address.pk,
            review.token,
        ).order
        self.item = self.order.items.get()

    def test_cancellation_uses_paid_amount_snapshot(self):
        self.item.cancelled_at = timezone.now()
        self.item.save(update_fields=["cancelled_at"])

        financials = get_order_financials(self.order)

        self.assertEqual(financials.original_total, Decimal("2160.00"))
        self.assertEqual(financials.cancelled_total, Decimal("2160.00"))
        self.assertEqual(financials.adjusted_total, ZERO)

    def test_approved_return_uses_paid_amount_snapshot(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status", "updated_at"])

        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Changed my mind",
        )
        approve_return(
            return_request.items.get().pk,
            admin=self.admin,
        )

        financials = get_order_financials(self.order)

        self.assertEqual(financials.original_total, Decimal("2160.00"))
        self.assertEqual(
            financials.approved_return_total,
            Decimal("2160.00"),
        )
        self.assertEqual(financials.adjusted_total, ZERO)

    def test_admin_list_adjustment_uses_paid_amount_snapshot(self):
        self.item.cancelled_at = timezone.now()
        self.item.save(update_fields=["cancelled_at"])

        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_orders:list"))

        self.assertEqual(response.status_code, 200)
        row = next(
            row
            for row in response.context["order_rows"]
            if row["order"].pk == self.order.pk
        )
        self.assertEqual(
            row["financials"].cancelled_total,
            Decimal("2160.00"),
        )
        self.assertEqual(row["financials"].adjusted_total, ZERO)
        self.assertEqual(row["order"].calc_adjusted_total, ZERO)
