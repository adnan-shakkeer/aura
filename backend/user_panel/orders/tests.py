"""Focused tests for the checkout review / order-placement service."""

import re
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant
from user_panel.address.models import Address
from user_panel.cart.models import Cart, CartItem
from user_panel.cart.services import CartServiceError

from .models import Order, OrderItem
from .services import (
    EmptyCartError,
    ExpiredReviewTokenError,
    InsufficientStockError,
    InvalidAddressError,
    InvalidQuantityError,
    InvalidReviewTokenError,
    REASON_ADDRESS_CHANGED,
    REASON_CART_CHANGED,
    REASON_PRICE_CHANGED,
    REVIEW_TOKEN_MAX_AGE,
    REVIEW_TOKEN_SALT,
    STATUS_CREATED,
    STATUS_DUPLICATE,
    STATUS_REVIEW_REQUIRED,
    UnexpectedOrderFailure,
    VariantUnavailableError,
    _aggregate_state,
    place_order,
    review_checkout,
)

User = get_user_model()


def make_user(username="alice"):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pass1234",
    )


def make_catalogue(*, sku="SKU-1", price="1200.00", stock=5, name="Noir"):
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
        name=name,
        description="Product description",
        fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
    )
    variant = ProductVariant.objects.create(
        product=product,
        size="50ml",
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )
    return category, brand, product, variant


def make_address(user, **overrides):
    values = {
        "full_name": "Alice Doe",
        "phone_number": "9876543210",
        "address_line1": "12 Test Street",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pincode": "400001",
        "address_type": "HOME",
    }
    values.update(overrides)
    return Address.objects.create(user=user, **values)


def make_expired_token(payload):
    old_time = time.time() - REVIEW_TOKEN_MAX_AGE - 60
    with patch("django.core.signing.time.time", return_value=old_time):
        return signing.dumps(
            payload,
            salt=REVIEW_TOKEN_SALT,
            compress=True,
        )


def make_existing_order(order_number):
    return Order.objects.create(
        user=None,
        order_number=order_number,
        subtotal=Decimal("0.00"),
        total=Decimal("0.00"),
        delivery_full_name="Existing",
        delivery_phone_number="9999999999",
        delivery_address_line1="1 Existing Road",
        delivery_city="Delhi",
        delivery_state="Delhi",
        delivery_pincode="110001",
    )


class OrderServiceTestBase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        self.item = CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )


class SuccessfulOrderTests(OrderServiceTestBase):
    def test_review_returns_current_database_prices(self):
        review = review_checkout(self.user, self.address.pk)

        self.assertEqual(review.subtotal, Decimal("2400.00"))
        self.assertEqual(review.total, Decimal("2400.00"))
        self.assertEqual(review.discount_total, Decimal("0.00"))
        self.assertEqual(review.shipping_charge, Decimal("0.00"))
        self.assertEqual(review.tax_total, Decimal("0.00"))
        self.assertEqual(len(review.lines), 1)

        line = review.lines[0]
        self.assertEqual(line.variant_id, self.variant.pk)
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.unit_price, Decimal("1200.00"))
        self.assertEqual(line.line_total, Decimal("2400.00"))

    def test_successful_order_creation_and_snapshots(self):
        review = review_checkout(self.user, self.address.pk)
        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_CREATED)
        order = result.order
        self.assertIsNotNone(order)
        self.assertEqual(order.user_id, self.user.pk)
        self.assertEqual(order.status, Order.OrderStatus.PENDING)
        self.assertEqual(order.payment_method, Order.PaymentMethod.COD)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(order.subtotal, Decimal("2400.00"))
        self.assertEqual(order.discount_total, Decimal("0.00"))
        self.assertEqual(order.shipping_charge, Decimal("0.00"))
        self.assertEqual(order.tax_total, Decimal("0.00"))
        self.assertEqual(order.total, Decimal("2400.00"))
        self.assertEqual(order.idempotency_key, review.submission_id)
        self.assertRegex(order.order_number, r"^AURA-\d{8}-[0-9A-F]{8}$")

        self.assertEqual(order.delivery_full_name, self.address.full_name)
        self.assertEqual(order.delivery_phone_number, self.address.phone_number)
        self.assertEqual(order.delivery_address_line1, self.address.address_line1)
        self.assertEqual(order.delivery_address_line2, "")
        self.assertEqual(order.delivery_landmark, "")
        self.assertEqual(order.delivery_city, self.address.city)
        self.assertEqual(order.delivery_state, self.address.state)
        self.assertEqual(order.delivery_pincode, self.address.pincode)
        self.assertEqual(
            order.delivery_address_type,
            self.address.address_type,
        )

        items = list(order.items.all())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.variant_id, self.variant.pk)
        self.assertEqual(item.product_name, self.product.name)
        self.assertEqual(item.brand_name, self.brand.name)
        self.assertEqual(item.variant_size, self.variant.size)
        self.assertEqual(item.sku, self.variant.sku)
        self.assertEqual(item.unit_price, Decimal("1200.00"))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.line_total, Decimal("2400.00"))

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        self.assertEqual(self.cart.items.count(), 0)


class OrderConfirmationViewTests(OrderServiceTestBase):
    def setUp(self):
        super().setUp()
        review = review_checkout(self.user, self.address.pk)
        self.order = place_order(
            self.user,
            self.address.pk,
            review.token,
        ).order
        self.url = reverse(
            "orders:order_confirmation",
            args=[self.order.order_number],
        )

    def test_confirmation_displays_persisted_order_data(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Order Confirmed")
        self.assertContains(response, self.order.order_number)
        self.assertContains(response, "Cash on Delivery")
        self.assertContains(response, "Unpaid")
        self.assertContains(response, self.address.full_name)
        self.assertContains(response, self.address.phone_number)
        self.assertContains(response, self.address.address_line1)
        self.assertContains(response, self.address.city)
        self.assertContains(response, self.address.state)
        self.assertContains(response, self.address.pincode)
        self.assertContains(response, self.product.name)
        self.assertContains(response, self.brand.name)
        self.assertContains(response, self.variant.size)
        self.assertContains(response, self.variant.sku)
        self.assertContains(response, "2 × ₹1200.00")
        self.assertContains(response, "₹1200.00")
        self.assertContains(response, "₹2400.00")
        self.assertContains(response, "FREE")
        self.assertContains(response, "Discount")
        self.assertContains(response, "Tax")
        self.assertContains(response, "Continue Shopping")
        self.assertEqual(response.context["total_quantity"], 2)

    def test_confirmation_uses_snapshots_after_catalogue_changes(self):
        self.client.force_login(self.user)

        self.variant.price = Decimal("9999.00")
        self.variant.size = "100ml"
        self.variant.save(update_fields=["price", "size"])
        self.product.name = "Changed Product"
        self.product.save(update_fields=["name"])
        self.brand.name = "Changed Brand"
        self.brand.save(update_fields=["name"])

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noir")
        self.assertContains(response, "50ml")
        self.assertContains(response, "₹1200.00")
        self.assertContains(response, "₹2400.00")
        self.assertNotContains(response, "Changed Product")
        self.assertNotContains(response, "Changed Brand")
        self.assertNotContains(response, "₹9999.00")

    def test_other_user_cannot_view_confirmation(self):
        other = make_user("bob")
        self.client.force_login(other)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("user_auth:login"), response.url)

    def test_unknown_order_number_returns_404(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse(
                "orders:order_confirmation",
                args=["AURA-00000000-DOESNOT1"],
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_response_is_not_cached(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertIn("no-cache", response["Cache-Control"])


class CartValidationTests(OrderServiceTestBase):
    def test_empty_cart_rejected(self):
        user = make_user("empty")
        address = make_address(user)

        with self.assertRaises(EmptyCartError):
            review_checkout(user, address.pk)

        Cart.objects.create(user=user)
        with self.assertRaises(EmptyCartError):
            review_checkout(user, address.pk)

    def test_empty_cart_at_placement_rejected(self):
        review = review_checkout(self.user, self.address.pk)
        self.item.delete()

        with self.assertRaises(EmptyCartError):
            place_order(self.user, self.address.pk, review.token)

        self.assertEqual(Order.objects.count(), 0)

    def test_unavailable_catalogue_hierarchy_rejected(self):
        checks = [
            (self.variant, "is_listed", False),
            (self.variant, "is_deleted", True),
            (self.product, "is_listed", False),
            (self.product, "is_deleted", True),
            (self.brand, "is_listed", False),
            (self.brand, "is_deleted", True),
            (self.category, "is_listed", False),
            (self.category, "is_deleted", True),
        ]

        for obj, field, value in checks:
            with self.subTest(field=field, value=value):
                setattr(obj, field, value)
                obj.save()

                with self.assertRaises(VariantUnavailableError):
                    review_checkout(self.user, self.address.pk)

                setattr(obj, field, not value)
                obj.save()

    def test_insufficient_stock_on_review_rejects_whole_cart(self):
        self.variant.stock = 1
        self.variant.save()

        with self.assertRaises(InsufficientStockError):
            review_checkout(self.user, self.address.pk)

        self.variant.stock = 0
        self.variant.save()

        with self.assertRaises(InsufficientStockError):
            review_checkout(self.user, self.address.pk)

    def test_insufficient_stock_at_placement_creates_no_order(self):
        review = review_checkout(self.user, self.address.pk)

        self.variant.stock = 1
        self.variant.save()

        with self.assertRaises(InsufficientStockError):
            place_order(self.user, self.address.pk, review.token)

        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(self.cart.items.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)

    def test_invalid_quantity_is_converted(self):
        with patch(
            "user_panel.orders.services._validate_quantity",
            side_effect=CartServiceError("bad quantity"),
        ):
            with self.assertRaises(InvalidQuantityError):
                review_checkout(self.user, self.address.pk)

    def test_duplicate_variant_aggregation(self):
        aggregated = _aggregate_state(
            [
                (None, self.variant, 1),
                (None, self.variant, 2),
            ]
        )

        self.assertEqual(len(aggregated), 1)
        variant, quantity = aggregated[0]
        self.assertEqual(variant.pk, self.variant.pk)
        self.assertEqual(quantity, 3)


class AddressValidationTests(OrderServiceTestBase):
    def test_missing_address_rejected(self):
        with self.assertRaises(InvalidAddressError):
            review_checkout(self.user, 999999)

    def test_soft_deleted_address_rejected(self):
        self.address.is_deleted = True
        self.address.save()

        with self.assertRaises(InvalidAddressError):
            review_checkout(self.user, self.address.pk)

    def test_other_users_address_rejected(self):
        other = make_user("bob")
        other_address = make_address(other)

        with self.assertRaises(InvalidAddressError):
            review_checkout(self.user, other_address.pk)

    def test_address_modified_between_review_and_placement(self):
        review = review_checkout(self.user, self.address.pk)

        self.address.city = "Pune"
        self.address.save()

        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_ADDRESS_CHANGED)
        self.assertIsNone(result.order)
        self.assertIsNotNone(result.review)
        self.assertEqual(result.review.address_id, self.address.pk)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(self.variant.stock, 5)
        self.assertEqual(Order.objects.count(), 0)

    def test_different_address_requires_new_review(self):
        other_address = make_address(
            self.user,
            address_line1="99 Other Street",
        )
        review = review_checkout(self.user, self.address.pk)

        result = place_order(self.user, other_address.pk, review.token)

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_ADDRESS_CHANGED)
        self.assertEqual(result.review.address_id, other_address.pk)
        self.assertEqual(Order.objects.count(), 0)


class ReviewReconfirmationTests(OrderServiceTestBase):
    def test_price_change_requires_review(self):
        review = review_checkout(self.user, self.address.pk)

        self.variant.price = Decimal("1300.00")
        self.variant.save()

        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_PRICE_CHANGED)
        self.assertIsNone(result.order)
        self.assertEqual(result.review.total, Decimal("2600.00"))
        self.assertNotEqual(
            result.review.submission_id,
            review.submission_id,
        )
        self.assertEqual(Order.objects.count(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_quantity_change_requires_review(self):
        review = review_checkout(self.user, self.address.pk)

        self.item.quantity = 3
        self.item.save()

        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_CART_CHANGED)
        self.assertEqual(Order.objects.count(), 0)

    def test_item_added_requires_review(self):
        review = review_checkout(self.user, self.address.pk)
        _category, _brand, _product, second_variant = make_catalogue(
            sku="SKU-2",
            name="Bleu",
        )
        CartItem.objects.create(
            cart=self.cart,
            variant=second_variant,
            quantity=1,
        )

        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_CART_CHANGED)
        self.assertEqual(Order.objects.count(), 0)

    def test_item_removed_requires_review(self):
        _category, _brand, _product, second_variant = make_catalogue(
            sku="SKU-3",
            name="Bleu",
        )
        second_item = CartItem.objects.create(
            cart=self.cart,
            variant=second_variant,
            quantity=1,
        )
        review = review_checkout(self.user, self.address.pk)

        second_item.delete()

        result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_REVIEW_REQUIRED)
        self.assertEqual(result.reason, REASON_CART_CHANGED)
        self.assertEqual(Order.objects.count(), 0)


class ReviewTokenTests(OrderServiceTestBase):
    def test_invalid_signature_rejected(self):
        with self.assertRaises(InvalidReviewTokenError):
            place_order(self.user, self.address.pk, "not-a-valid-token")

    def test_tampered_token_rejected(self):
        review = review_checkout(self.user, self.address.pk)
        characters = list(review.token)
        index = len(characters) // 2
        characters[index] = "A" if characters[index] != "A" else "B"

        with self.assertRaises(InvalidReviewTokenError):
            place_order(
                self.user,
                self.address.pk,
                "".join(characters),
            )

    def test_expired_token_without_order_rejected(self):
        review = review_checkout(self.user, self.address.pk)
        payload = signing.loads(review.token, salt=REVIEW_TOKEN_SALT)
        expired_token = make_expired_token(payload)

        with self.assertRaises(ExpiredReviewTokenError):
            place_order(self.user, self.address.pk, expired_token)

        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())

    def test_expired_token_with_existing_order_recovers_duplicate(self):
        review = review_checkout(self.user, self.address.pk)
        first = place_order(self.user, self.address.pk, review.token)

        payload = signing.loads(review.token, salt=REVIEW_TOKEN_SALT)
        expired_token = make_expired_token(payload)

        result = place_order(self.user, self.address.pk, expired_token)

        self.assertEqual(result.status, STATUS_DUPLICATE)
        self.assertTrue(result.recovered)
        self.assertEqual(result.order.pk, first.order.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_repeated_valid_token_returns_duplicate(self):
        review = review_checkout(self.user, self.address.pk)
        first = place_order(self.user, self.address.pk, review.token)
        second = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(first.status, STATUS_CREATED)
        self.assertEqual(second.status, STATUS_DUPLICATE)
        self.assertFalse(second.recovered)
        self.assertEqual(second.order.pk, first.order.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)

    def test_other_user_cannot_use_token(self):
        review = review_checkout(self.user, self.address.pk)
        other = make_user("bob")
        other_address = make_address(other)

        with self.assertRaises(InvalidReviewTokenError):
            place_order(other, other_address.pk, review.token)

    def test_forged_token_cannot_access_another_order(self):
        review = review_checkout(self.user, self.address.pk)
        place_order(self.user, self.address.pk, review.token)

        other = make_user("bob")
        other_address = make_address(other)
        forged_payload = {
            "sid": review.submission_id,
            "uid": other.pk,
            "cid": 1,
            "aid": other_address.pk,
        }
        forged_token = signing.dumps(
            forged_payload,
            salt=REVIEW_TOKEN_SALT,
            compress=True,
        )

        with self.assertRaises(InvalidReviewTokenError):
            place_order(other, other_address.pk, forged_token)


class TransactionIntegrityTests(OrderServiceTestBase):
    def test_failure_rolls_back_order_items_stock_and_cart(self):
        review = review_checkout(self.user, self.address.pk)

        with patch(
            "user_panel.orders.services._deduct_stock",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(UnexpectedOrderFailure):
                place_order(self.user, self.address.pk, review.token)

        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)


class OrderNumberTests(OrderServiceTestBase):
    def test_order_number_collision_retries_with_savepoint(self):
        make_existing_order("AURA-20260101-DEADBEEF")
        review = review_checkout(self.user, self.address.pk)
        numbers = iter(
            [
                "AURA-20260101-DEADBEEF",
                "AURA-20260101-CAFEBABE",
            ]
        )

        with patch.object(
            Order,
            "_generate_order_number",
            new=staticmethod(lambda: next(numbers)),
        ):
            result = place_order(self.user, self.address.pk, review.token)

        self.assertEqual(result.status, STATUS_CREATED)
        self.assertEqual(
            result.order.order_number,
            "AURA-20260101-CAFEBABE",
        )
        self.assertEqual(Order.objects.count(), 2)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentOrderTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = make_user("alice")
        (
            self.category,
            self.brand,
            self.product,
            self.variant,
        ) = make_catalogue(sku="SKU-C1", stock=5)
        self.address = make_address(self.user)
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )

    def test_concurrent_duplicate_submission_creates_one_order(self):
        review = review_checkout(self.user, self.address.pk)
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                results.append(
                    place_order(self.user, self.address.pk, review.token)
                )
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sorted(result.status for result in results),
            [STATUS_CREATED, STATUS_DUPLICATE],
        )
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_concurrent_last_unit_only_one_order(self):
        self.variant.stock = 1
        self.variant.save(update_fields=["stock"])

        first_item = self.cart.items.get()
        first_item.quantity = 1
        first_item.save(update_fields=["quantity"])

        other_user = make_user("bob")
        other_address = make_address(
            other_user,
            full_name="Bob Doe",
            phone_number="9876543211",
        )
        other_cart = Cart.objects.create(user=other_user)
        CartItem.objects.create(
            cart=other_cart,
            variant=self.variant,
            quantity=1,
        )

        first_review = review_checkout(self.user, self.address.pk)
        second_review = review_checkout(other_user, other_address.pk)

        created = []
        failed = []
        barrier = threading.Barrier(2)

        def worker(user, address, token):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                created.append(place_order(user, address.pk, token))
            except Exception as exc:  # noqa: BLE001 - reported below
                failed.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(
                target=worker,
                args=(self.user, self.address, first_review.token),
            ),
            threading.Thread(
                target=worker,
                args=(other_user, other_address, second_review.token),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(created), 1)
        self.assertEqual(len(failed), 1)
        self.assertIsInstance(failed[0], InsufficientStockError)
        self.assertEqual(Order.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
