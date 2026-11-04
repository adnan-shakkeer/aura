"""Tests for the Phase 6.2 customer My Orders listing."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant, VariantImage

from .models import Order, OrderItem

User = get_user_model()

MY_ORDERS_URL = reverse("orders:my_orders")


def make_user(username="alice", **overrides):
    values = {
        "username": username,
        "email": f"{username}@example.com",
        "password": "pass1234",
    }
    values.update(overrides)
    return User.objects.create_user(**values)


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


def make_order(user, order_number, *, total="1200.00", status=None, payment_status=None):
    return Order.objects.create(
        user=user,
        order_number=order_number,
        status=status or Order.OrderStatus.PENDING,
        payment_status=payment_status or Order.PaymentStatus.UNPAID,
        subtotal=Decimal(total),
        total=Decimal(total),
        delivery_full_name="Alice Doe",
        delivery_phone_number="9876543210",
        delivery_address_line1="12 Test Street",
        delivery_city="Mumbai",
        delivery_state="Maharashtra",
        delivery_pincode="400001",
    )


def add_item(order, variant, *, quantity=1, product_name=None, brand_name=None):
    unit_price = variant.price
    return OrderItem.objects.create(
        order=order,
        variant=variant,
        product_name=product_name or variant.product.name,
        brand_name=brand_name or variant.product.brand.name,
        variant_size=variant.size,
        sku=variant.sku,
        unit_price=unit_price,
        quantity=quantity,
        line_total=unit_price * quantity,
    )


class MyOrdersTestBase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(
            self.user,
            "AURA-20261102-00000001",
            total="2400.00",
        )
        self.item = add_item(self.order, self.variant, quantity=2)
        self.client.force_login(self.user)


class MyOrdersAccessTests(MyOrdersTestBase):
    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()

        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("user_auth:login"), response.url)

    def test_unsupported_method_returns_405(self):
        response = self.client.post(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 405)

    def test_response_is_not_cached(self):
        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response["Cache-Control"])

    def test_staff_user_is_redirected_to_admin_dashboard(self):
        admin = make_user("admin", is_staff=True)
        self.client.force_login(admin)

        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard:index"))

    def test_staff_user_is_redirected_from_order_confirmation(self):
        admin = make_user("admin", is_staff=True)
        self.client.force_login(admin)

        response = self.client.get(
            reverse("orders:order_confirmation", args=[self.order.order_number])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard:index"))

    def test_customer_can_still_open_order_confirmation(self):
        response = self.client.get(
            reverse("orders:order_confirmation", args=[self.order.order_number])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Order Confirmed")


class MyOrdersListTests(MyOrdersTestBase):
    def test_only_the_customers_orders_are_listed(self):
        other = make_user("mallory")
        other_order = make_order(other, "AURA-20261102-00009999")
        add_item(other_order, self.variant)

        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.order_number)
        self.assertNotContains(response, other_order.order_number)

    def test_one_card_is_rendered_per_order(self):
        second_variant = make_catalogue(sku="SKU-2", name="Amber")[3]
        add_item(self.order, second_variant)

        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.content.decode().count("data-order-card"), 1)
        self.assertContains(response, "+1 more item")

    def test_order_with_multiple_items_shows_total_quantity(self):
        second_variant = make_catalogue(sku="SKU-2", name="Amber")[3]
        add_item(self.order, second_variant, quantity=3)

        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "Qty 5")
        self.assertContains(response, "+1 more item")

    def test_order_snapshots_survive_catalogue_changes(self):
        self.product.name = "Changed Product"
        self.product.save(update_fields=["name"])
        self.brand.name = "Changed Brand"
        self.brand.save(update_fields=["name"])
        self.variant.size = "100ml"
        self.variant.price = Decimal("9999.00")
        self.variant.save(update_fields=["size", "price"])

        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "Noir")
        self.assertContains(response, "50ml")
        self.assertContains(response, "₹2400.00")
        self.assertNotContains(response, "Changed Product")
        self.assertNotContains(response, "Changed Brand")
        self.assertNotContains(response, "₹9999.00")

    def test_status_and_payment_labels_are_rendered(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.payment_status = Order.PaymentStatus.PAID
        self.order.save(update_fields=["status", "payment_status"])

        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "Delivered")
        self.assertContains(response, "Paid")
        self.assertContains(response, "Cash on Delivery")

    def test_cancelled_status_is_rendered(self):
        self.order.status = Order.OrderStatus.CANCELLED
        self.order.save(update_fields=["status"])

        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "Cancelled")
        self.assertContains(response, "order-card--cancelled")

    def test_details_control_links_to_order_detail(self):
        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "DETAILS")
        self.assertContains(
            response,
            f'href="{reverse("orders:order_detail", args=[self.order.order_number])}"',
        )
        self.assertNotContains(
            response,
            reverse("orders:order_confirmation", args=[self.order.order_number]),
        )

    def test_missing_image_uses_aura_placeholder(self):
        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "AURA")

    def test_variant_image_is_rendered(self):
        VariantImage.objects.create(
            variant=self.variant,
            image="products/variants/noir.jpg",
            alt_text="Noir bottle",
        )

        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "/media/products/variants/noir.jpg")
        self.assertContains(response, "Noir bottle")

    def test_order_without_items_shows_fallback(self):
        empty_order = make_order(self.user, "AURA-20261102-00000002")
        empty_order.items.all().delete()

        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, "Item details unavailable for this order.")

    def test_query_count_does_not_grow_with_order_items(self):
        with CaptureQueriesContext(connection) as single_order_queries:
            response = self.client.get(MY_ORDERS_URL)
        self.assertEqual(response.status_code, 200)

        for index in range(2, 6):
            variant = make_catalogue(sku=f"SKU-{index}", name=f"Fragrance {index}")[3]
            order = make_order(self.user, f"AURA-20261102-0000000{index}")
            add_item(order, variant, quantity=index)
            VariantImage.objects.create(
                variant=variant,
                image=f"products/variants/{index}.jpg",
            )

        with CaptureQueriesContext(connection) as multiple_order_queries:
            response = self.client.get(MY_ORDERS_URL)
        self.assertEqual(response.status_code, 200)

        self.assertEqual(len(single_order_queries), len(multiple_order_queries))


class MyOrdersSearchTests(MyOrdersTestBase):
    def test_search_matches_partial_order_number(self):
        response = self.client.get(MY_ORDERS_URL, {"q": "00000001"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.order_number)
        self.assertEqual(response.context["search_query"], "00000001")

    def test_search_is_scoped_to_the_customer(self):
        other = make_user("mallory")
        other_order = make_order(other, "AURA-20261102-00009999")
        add_item(other_order, self.variant)

        response = self.client.get(MY_ORDERS_URL, {"q": "00009999"})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, other_order.order_number)
        self.assertContains(response, "No orders match your search")

    def test_blank_search_shows_all_orders(self):
        response = self.client.get(MY_ORDERS_URL, {"q": "   "})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.order_number)
        self.assertEqual(response.context["search_query"], "")

    def test_no_results_shows_search_empty_state(self):
        response = self.client.get(MY_ORDERS_URL, {"q": "does-not-exist"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No orders match your search")
        self.assertContains(response, "Clear Search")

    def test_no_orders_shows_empty_state(self):
        self.order.delete()

        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No orders yet")
        self.assertContains(response, "Continue Shopping")

    def test_search_placeholder_is_order_number_only(self):
        response = self.client.get(MY_ORDERS_URL)

        self.assertContains(response, 'placeholder="Search by order number..."')


class MyOrdersPaginationTests(MyOrdersTestBase):
    def setUp(self):
        super().setUp()
        for index in range(2, 10):
            order = make_order(self.user, f"AURA-20261102-0000000{index}")
            add_item(order, self.variant, quantity=index)

    def test_first_page_contains_eight_orders(self):
        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["order_cards"]), 8)
        self.assertEqual(response.context["page_obj"].paginator.per_page, 8)

    def test_second_page_contains_remaining_order(self):
        response = self.client.get(MY_ORDERS_URL, {"page": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["order_cards"]), 1)
        self.assertEqual(response.context["page_obj"].number, 2)

    def test_invalid_page_values_are_clamped(self):
        numeric = self.client.get(MY_ORDERS_URL, {"page": 999})
        non_numeric = self.client.get(MY_ORDERS_URL, {"page": "not-a-page"})

        self.assertEqual(numeric.status_code, 200)
        self.assertEqual(numeric.context["page_obj"].number, 2)
        self.assertEqual(non_numeric.status_code, 200)
        self.assertEqual(non_numeric.context["page_obj"].number, 1)

    def test_search_query_is_preserved_in_pagination(self):
        response = self.client.get(MY_ORDERS_URL, {"q": "AURA-2026", "page": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["base_query"], "q=AURA-2026")
        self.assertContains(response, "q=AURA-2026")


class MyOrdersNavigationTests(MyOrdersTestBase):
    def test_account_navigation_links_to_my_orders(self):
        response = self.client.get(reverse("address_book"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, MY_ORDERS_URL)

    def test_confirmation_view_orders_button_links_to_my_orders(self):
        response = self.client.get(
            reverse("orders:order_confirmation", args=[self.order.order_number])
        )

        self.assertContains(response, MY_ORDERS_URL)
