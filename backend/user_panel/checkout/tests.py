"""Tests for the customer-facing checkout review page and address modal integration."""

import time
from decimal import Decimal
from http import HTTPStatus
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, TestCase
from django.urls import reverse

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant, VariantImage
from user_panel.address.models import Address
from user_panel.cart.models import Cart, CartItem
from user_panel.orders.models import Order
from user_panel.orders.services import (
    REVIEW_TOKEN_MAX_AGE,
    REVIEW_TOKEN_SALT,
    review_checkout,
)

from .views import (
    REVIEW_ADDRESS_SESSION_KEY,
    REVIEW_SESSION_KEY,
    REVIEW_SUBMISSION_SESSION_KEY,
)


User = get_user_model()

CHECKOUT_URL = reverse("checkout:checkout_review")
PLACE_ORDER_URL = reverse("checkout:place_order")


def make_expired_token(payload):
    old_time = time.time() - REVIEW_TOKEN_MAX_AGE - 60
    with patch("django.core.signing.time.time", return_value=old_time):
        return signing.dumps(
            payload,
            salt=REVIEW_TOKEN_SALT,
            compress=True,
        )


def confirmation_url(order):
    return reverse(
        "orders:order_confirmation",
        args=[order.order_number],
    )


def make_user(username="alice"):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pass1234",
    )


def make_variant(*, sku="SKU-1", price="1200.00", stock=5, name="Noir"):
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
    return ProductVariant.objects.create(
        product=product,
        size="50ml",
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )


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


class CheckoutReviewViewTestBase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.variant = make_variant()
        self.address = make_address(self.user, is_default=True)
        self.cart = Cart.objects.create(user=self.user)
        self.item = CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
        )
        self.client.force_login(self.user)


class CheckoutReviewAccessTests(CheckoutReviewViewTestBase):
    def test_anonymous_user_is_redirected_to_login(self):
        client = Client()
        response = client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("user_auth:login"), response.url)

    def test_unsupported_method_returns_405(self):
        response = self.client.put(CHECKOUT_URL)

        self.assertEqual(response.status_code, 405)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(CHECKOUT_URL, {"address_id": self.address.pk})

        self.assertEqual(response.status_code, 403)

    def test_response_is_not_cached(self):
        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response["Cache-Control"])


class CheckoutReviewStateTests(CheckoutReviewViewTestBase):
    def test_empty_cart_shows_empty_state(self):
        self.item.delete()

        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["empty_cart"])
        self.assertContains(response, "Nothing to check out yet")
        self.assertNotIn(REVIEW_SESSION_KEY, self.client.session)

    def test_no_saved_addresses_prompts_for_address(self):
        Address.objects.filter(user=self.user).delete()

        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["no_addresses"])
        self.assertContains(response, "Add a delivery address")

    def test_default_address_is_selected(self):
        second = make_address(self.user, full_name="Bob Second", is_default=False)

        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_address"].pk, self.address.pk)
        self.assertNotEqual(response.context["selected_address"].pk, second.pk)
        self.assertEqual(response.context["review"].address_id, self.address.pk)

    def test_select_address_via_get(self):
        second = make_address(self.user, full_name="Bob Second", is_default=False)

        response = self.client.get(CHECKOUT_URL, {"address": second.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_address"].pk, second.pk)
        self.assertEqual(response.context["review"].address_id, second.pk)
        self.assertEqual(
            self.client.session[REVIEW_ADDRESS_SESSION_KEY],
            second.pk,
        )

    def test_select_address_via_post_redirects_then_reviews(self):
        second = make_address(self.user, full_name="Bob Second", is_default=False)

        response = self.client.post(CHECKOUT_URL, {"address_id": second.pk})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{CHECKOUT_URL}?address={second.pk}")

        follow = self.client.get(response.url)
        self.assertEqual(follow.context["selected_address"].pk, second.pk)

    def test_post_invalid_address_id_shows_issue_without_redirect(self):
        other_user = make_user("mallory")
        foreign = make_address(other_user, full_name="Mallory Foreign")

        response = self.client.post(CHECKOUT_URL, {"address_id": foreign.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please choose a valid delivery address.")
        self.assertEqual(response.context["selected_address"].pk, self.address.pk)

    def test_foreign_address_in_query_is_rejected(self):
        other_user = make_user("mallory")
        foreign = make_address(other_user, full_name="Mallory Foreign")

        response = self.client.get(CHECKOUT_URL, {"address": foreign.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer available")
        self.assertEqual(response.context["selected_address"].pk, self.address.pk)
        self.assertNotEqual(response.context["selected_address"].pk, foreign.pk)

    def test_deleted_address_is_rejected(self):
        deleted = make_address(
            self.user,
            full_name="Deleted Address",
            is_deleted=True,
        )

        response = self.client.get(CHECKOUT_URL, {"address": deleted.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_address"].pk, self.address.pk)
        self.assertNotEqual(response.context["selected_address"].pk, deleted.pk)

    def test_malformed_address_id_falls_back_to_default(self):
        response = self.client.get(CHECKOUT_URL, {"address": "not-a-number"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_address"].pk, self.address.pk)


class CheckoutReviewContentTests(CheckoutReviewViewTestBase):
    def test_review_calls_service_and_renders_totals(self):
        with patch(
            "user_panel.checkout.views.review_checkout",
            wraps=review_checkout,
        ) as mocked:
            response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        mocked.assert_called_once_with(self.user, self.address.pk)
        self.assertEqual(response.context["review"].subtotal, Decimal("2400.00"))
        self.assertEqual(response.context["review"].total, Decimal("2400.00"))
        self.assertContains(response, "Noir")
        self.assertContains(response, "50ml")
        self.assertContains(response, "₹2400.00")
        # Visible button label must be exactly PLACE ORDER (not with "(Cash on Delivery)")
        self.assertContains(response, "PLACE ORDER")
        self.assertNotContains(response, "Place Order (Cash on Delivery)")

    def test_review_token_is_in_session_but_not_in_page(self):
        response = self.client.get(CHECKOUT_URL)
        token = self.client.session[REVIEW_SESSION_KEY]

        self.assertTrue(token)
        self.assertEqual(
            self.client.session[REVIEW_SUBMISSION_SESSION_KEY],
            response.context["review"].submission_id,
        )
        self.assertNotContains(response, token)

    def test_changing_address_regenerates_review_token(self):
        first = self.client.get(CHECKOUT_URL)
        first_token = self.client.session[REVIEW_SESSION_KEY]
        second_address = make_address(self.user, full_name="Bob Second")

        self.client.get(CHECKOUT_URL, {"address": second_address.pk})

        self.assertNotEqual(self.client.session[REVIEW_SESSION_KEY], first_token)
        self.assertEqual(
            self.client.session[REVIEW_ADDRESS_SESSION_KEY],
            second_address.pk,
        )
        self.assertEqual(first.context["review"].address_id, self.address.pk)

    def test_line_image_is_rendered_from_catalogue(self):
        VariantImage.objects.create(
            variant=self.variant,
            image="products/variants/noir.jpg",
        )

        response = self.client.get(CHECKOUT_URL)

        self.assertContains(response, "/media/products/variants/noir.jpg")

    def test_checkout_links_to_address_book_with_return_target(self):
        response = self.client.get(CHECKOUT_URL)

        self.assertContains(response, "Manage Addresses")
        self.assertContains(response, "?next=")

    def test_unavailable_variant_shows_customer_message(self):
        self.variant.is_listed = False
        self.variant.save(update_fields=["is_listed"])

        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unavailable")
        self.assertNotIn(REVIEW_SESSION_KEY, self.client.session)

    def test_insufficient_stock_shows_customer_message(self):
        self.variant.stock = 1
        self.variant.save(update_fields=["stock"])

        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "currently available")
        self.assertNotIn(REVIEW_SESSION_KEY, self.client.session)


class CheckoutRedesignLayoutTests(CheckoutReviewViewTestBase):
    def test_two_column_layout_and_sections_rendered(self):
        response = self.client.get(CHECKOUT_URL)

        self.assertEqual(response.status_code, 200)
        # Check section number badges and headings
        self.assertContains(response, "01")
        self.assertContains(response, "Shipping Address")
        self.assertContains(response, "02")
        self.assertContains(response, "Payment Method")
        self.assertContains(response, "03")
        self.assertContains(response, "Order Review")
        self.assertContains(response, "Order Summary")

    def test_payment_method_is_cod_only(self):
        response = self.client.get(CHECKOUT_URL)

        self.assertContains(response, "Cash on Delivery")
        self.assertContains(response, "Pay when your order is delivered.")
        # Ensure unimplemented payment methods are absent
        self.assertNotContains(response, "Razorpay")
        self.assertNotContains(response, "PayPal")
        self.assertNotContains(response, "Wallet")

    def test_order_review_contains_fragrance_concentration_and_size(self):
        response = self.client.get(CHECKOUT_URL)

        # Concentration from Product model
        self.assertContains(response, "Eau de Parfum (EDP)")
        self.assertContains(response, "50ml")
        self.assertContains(response, "Qty: 2")

    def test_order_review_has_no_cart_edit_controls(self):
        response = self.client.get(CHECKOUT_URL)

        # Must not contain quantity adjusters, remove item, or wishlist buttons
        self.assertNotContains(response, 'name="qty"')
        self.assertNotContains(response, "remove-item")
        self.assertNotContains(response, "update_cart_item")
        self.assertNotContains(response, "remove_cart_item")

    def test_order_summary_has_no_coupon_or_tax_inputs(self):
        response = self.client.get(CHECKOUT_URL)

        # Must not display coupon, discount row, or tax input
        self.assertNotContains(response, "Apply Coupon")
        self.assertNotContains(response, "coupon_code")
        self.assertNotContains(response, "Referral")
        self.assertNotContains(response, "Estimated Tax")
        self.assertContains(response, "Subtotal (2 items)")
        self.assertContains(response, "FREE")

    def test_place_order_button_submits_to_place_order_endpoint(self):
        response = self.client.get(CHECKOUT_URL)

        self.assertContains(response, "PLACE ORDER")
        self.assertContains(response, f'action="{PLACE_ORDER_URL}"')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertNotContains(response, 'aria-disabled="true"')

    def test_address_card_renders_edit_button_and_add_address_button(self):
        response = self.client.get(CHECKOUT_URL)

        self.assertContains(response, "edit-address-btn")
        self.assertContains(response, 'data-address-id="')
        self.assertContains(response, "open-add-address-btn")
        self.assertContains(response, "Add a New Address")
        # Modals exist in DOM
        self.assertContains(response, "add-address-modal")
        self.assertContains(response, "edit-address-modal")


class CheckoutAddressModalIntegrationTests(CheckoutReviewViewTestBase):
    def test_add_address_via_ajax_endpoint_and_select_in_checkout(self):
        payload = {
            "full_name": "Bob Builder",
            "phone_number": "9876543211",
            "address_line1": "45 Builder Avenue",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560001",
            "address_type": "WORK",
            "is_default": "false",
        }

        # Submitting to existing address book add endpoint
        response = self.client.post(
            reverse("add_address"),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, HTTPStatus.CREATED)
        data = response.json()
        self.assertTrue(data.get("success"))
        new_id = data.get("id")
        self.assertIsNotNone(new_id)

        # Verify address exists and belongs to user
        new_addr = Address.objects.get(pk=new_id)
        self.assertEqual(new_addr.user, self.user)
        self.assertEqual(new_addr.full_name, "Bob Builder")

        # Checkout refresh with the new address ID automatically selects it
        checkout_res = self.client.get(CHECKOUT_URL, {"address": new_id})
        self.assertEqual(checkout_res.status_code, 200)
        self.assertEqual(checkout_res.context["selected_address"].pk, new_id)
        self.assertEqual(
            self.client.session[REVIEW_ADDRESS_SESSION_KEY],
            new_id,
        )

    def test_edit_address_via_ajax_endpoint_updates_and_preserves_selection(self):
        edit_url = reverse("edit_address", args=[self.address.pk])
        payload = {
            "full_name": "Alice Modified",
            "phone_number": "9876543299",
            "address_line1": "99 Updated Boulevard",
            "city": "Mumbai",
            "state": "Maharashtra",
            "pincode": "400001",
            "address_type": "HOME",
            "is_default": "true",
        }

        response = self.client.post(
            edit_url,
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertTrue(response.json().get("success"))

        self.address.refresh_from_db()
        self.assertEqual(self.address.full_name, "Alice Modified")
        self.assertEqual(self.address.phone_number, "9876543299")

        # Checkout refresh preserves selection and renders updated details
        checkout_res = self.client.get(CHECKOUT_URL, {"address": self.address.pk})
        self.assertEqual(checkout_res.status_code, 200)
        self.assertContains(checkout_res, "Alice Modified")
        self.assertContains(checkout_res, "9876543299")
        self.assertContains(checkout_res, "99 Updated Boulevard")

    def test_edit_other_user_address_is_forbidden(self):
        other_user = make_user("mallory")
        foreign_addr = make_address(other_user, full_name="Mallory Secret")

        edit_url = reverse("edit_address", args=[foreign_addr.pk])
        response = self.client.post(
            edit_url,
            {"full_name": "Tampered Name"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        foreign_addr.refresh_from_db()
        self.assertEqual(foreign_addr.full_name, "Mallory Secret")

    def test_deleted_address_cannot_be_edited(self):
        deleted = make_address(self.user, full_name="Old Addr", is_deleted=True)

        edit_url = reverse("edit_address", args=[deleted.pk])
        response = self.client.post(
            edit_url,
            {"full_name": "Trying to revive"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

    def test_invalid_address_form_returns_validation_errors(self):
        # Missing required phone number and invalid pincode
        payload = {
            "full_name": "A",  # too short
            "phone_number": "",
            "address_line1": "X",
            "city": "",
            "state": "",
            "pincode": "abc",
        }

        response = self.client.post(
            reverse("add_address"),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        data = response.json()
        self.assertFalse(data.get("success"))
        self.assertIn("errors", data)
        self.assertIn("full_name", data["errors"])
        self.assertIn("phone_number", data["errors"])

    def test_editing_address_updates_review_token(self):
        # 1. First review
        self.client.get(CHECKOUT_URL)
        token_before = self.client.session[REVIEW_SESSION_KEY]

        # 2. Modify address line 1
        edit_url = reverse("edit_address", args=[self.address.pk])
        self.client.post(
            edit_url,
            {
                "full_name": self.address.full_name,
                "phone_number": self.address.phone_number,
                "address_line1": "Brand New Street 123",
                "city": self.address.city,
                "state": self.address.state,
                "pincode": self.address.pincode,
                "address_type": self.address.address_type,
                "is_default": "true",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # 3. Re-request checkout review with the updated address
        self.client.get(CHECKOUT_URL, {"address": self.address.pk})
        token_after = self.client.session[REVIEW_SESSION_KEY]

        # Because address fingerprint changed, review token must be refreshed
        self.assertNotEqual(token_before, token_after)


class PlaceOrderViewTests(CheckoutReviewViewTestBase):
    def _prime_checkout(self):
        response = self.client.get(CHECKOUT_URL)
        self.assertEqual(response.status_code, 200)
        return response

    def _session_review_state(self):
        return {
            "token": self.client.session[REVIEW_SESSION_KEY],
            "address_id": self.client.session[REVIEW_ADDRESS_SESSION_KEY],
            "submission_id": self.client.session[
                REVIEW_SUBMISSION_SESSION_KEY
            ],
        }

    def test_successful_cod_order_placement(self):
        self._prime_checkout()
        state_before = self._session_review_state()

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(response.url, confirmation_url(order))
        self.assertEqual(order.user_id, self.user.pk)
        self.assertEqual(order.status, Order.OrderStatus.PENDING)
        self.assertEqual(order.payment_method, Order.PaymentMethod.COD)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(order.subtotal, Decimal("2400.00"))
        self.assertEqual(order.total, Decimal("2400.00"))

        item = order.items.get()
        self.assertEqual(item.variant_id, self.variant.pk)
        self.assertEqual(item.product_name, "Noir")
        self.assertEqual(item.variant_size, "50ml")
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.unit_price, Decimal("1200.00"))
        self.assertEqual(item.line_total, Decimal("2400.00"))

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        self.assertEqual(self.cart.items.count(), 0)

        # Review state is intentionally preserved for idempotent recovery.
        self.assertEqual(self._session_review_state(), state_before)

    def test_unauthenticated_user_is_redirected_to_login(self):
        client = Client()
        response = client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("user_auth:login"), response.url)
        self.assertEqual(Order.objects.count(), 0)

    def test_get_request_returns_405(self):
        self._prime_checkout()

        response = self.client.get(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(Order.objects.count(), 0)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.count(), 0)

    def test_missing_review_session_redirects_to_checkout(self):
        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, CHECKOUT_URL)
        self.assertEqual(Order.objects.count(), 0)

        follow = self.client.get(response.url)
        self.assertContains(follow, "Please review your order before placing it.")

    def test_missing_address_session_value_redirects_to_checkout(self):
        self._prime_checkout()
        session = self.client.session
        session.pop(REVIEW_ADDRESS_SESSION_KEY, None)
        session.save()

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, CHECKOUT_URL)
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_review_token_is_rejected_safely(self):
        self._prime_checkout()
        session = self.client.session
        session[REVIEW_SESSION_KEY] = "not-a-valid-token"
        session.save()

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, CHECKOUT_URL)
        self.assertEqual(Order.objects.count(), 0)
        self.assertNotIn(REVIEW_SESSION_KEY, self.client.session)

        follow = self.client.get(response.url)
        self.assertContains(follow, "checkout review is no longer valid")

    def test_price_change_returns_customer_to_review(self):
        self._prime_checkout()
        token_before = self.client.session[REVIEW_SESSION_KEY]

        self.variant.price = Decimal("1300.00")
        self.variant.save(update_fields=["price"])

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(CHECKOUT_URL))
        self.assertIn(f"address={self.address.pk}", response.url)
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())
        self.assertNotEqual(
            self.client.session[REVIEW_SESSION_KEY],
            token_before,
        )

        follow = self.client.get(response.url)
        self.assertContains(follow, "Prices have been updated.")

    def test_address_change_returns_customer_to_review(self):
        self._prime_checkout()
        token_before = self.client.session[REVIEW_SESSION_KEY]

        self.address.city = "Pune"
        self.address.save(update_fields=["city"])

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(CHECKOUT_URL))
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())
        self.assertNotEqual(
            self.client.session[REVIEW_SESSION_KEY],
            token_before,
        )

        follow = self.client.get(response.url)
        self.assertContains(follow, "delivery address has changed")

    def test_insufficient_stock_leaves_cart_and_stock_intact(self):
        self._prime_checkout()

        self.variant.stock = 1
        self.variant.save(update_fields=["stock"])

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, CHECKOUT_URL)
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)

        follow = self.client.get(response.url)
        self.assertContains(follow, "currently available")

    def test_invalid_deleted_address_is_rejected(self):
        self._prime_checkout()

        self.address.is_deleted = True
        self.address.save(update_fields=["is_deleted"])

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, CHECKOUT_URL)
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())

    def test_retry_with_same_session_token_recovers_same_order(self):
        self._prime_checkout()
        token_before = self.client.session[REVIEW_SESSION_KEY]

        first = self.client.post(PLACE_ORDER_URL)
        order = Order.objects.get()

        second = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first.url, confirmation_url(order))
        self.assertEqual(second.url, confirmation_url(order))
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(
            self.client.session[REVIEW_SESSION_KEY],
            token_before,
        )

        follow = self.client.get(second.url)
        self.assertContains(follow, "Order Confirmed")
        self.assertContains(follow, "already been placed")

    def test_expired_token_without_order_requires_new_review(self):
        self._prime_checkout()
        payload = signing.loads(
            self.client.session[REVIEW_SESSION_KEY],
            salt=REVIEW_TOKEN_SALT,
        )
        session = self.client.session
        session[REVIEW_SESSION_KEY] = make_expired_token(payload)
        session.save()

        response = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, CHECKOUT_URL)
        self.assertEqual(Order.objects.count(), 0)
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())

        follow = self.client.get(response.url)
        self.assertContains(follow, "session expired")

    def test_expired_token_with_existing_order_recovers_confirmation(self):
        self._prime_checkout()
        payload = signing.loads(
            self.client.session[REVIEW_SESSION_KEY],
            salt=REVIEW_TOKEN_SALT,
        )

        first = self.client.post(PLACE_ORDER_URL)
        order = Order.objects.get()

        session = self.client.session
        session[REVIEW_SESSION_KEY] = make_expired_token(payload)
        session.save()
        recovered = self.client.post(PLACE_ORDER_URL)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(recovered.status_code, 302)
        self.assertEqual(recovered.url, confirmation_url(order))
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(order.items.count(), 1)

        follow = self.client.get(recovered.url)
        self.assertContains(follow, "Showing your existing confirmation")


class AddressBookNextTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.address = make_address(self.user)
        self.client.force_login(self.user)

    def test_relative_next_is_preserved(self):
        target = f"{CHECKOUT_URL}?address={self.address.pk}"

        response = self.client.get(reverse("address_book"), {"next": target})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["next_url"], target)
        self.assertContains(response, "Return to Checkout")

    def test_external_next_is_rejected(self):
        response = self.client.get(
            reverse("address_book"),
            {"next": "https://evil.example.com/phish"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["next_url"], "")
        self.assertNotContains(response, "Return to Checkout")

    def test_protocol_relative_next_is_rejected(self):
        response = self.client.get(
            reverse("address_book"),
            {"next": "//evil.example.com/phish"},
        )

        self.assertEqual(response.context["next_url"], "")
