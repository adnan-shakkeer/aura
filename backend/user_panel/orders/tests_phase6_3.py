"""Phase 6.3 customer order details, cancellation, and return tests."""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant
from user_panel.orders.cancellation import cancel_order, cancel_order_item
from user_panel.orders.exceptions import (
    ItemAlreadyCancelledError,
    ItemNotCancellableError,
    OrderPermissionError,
    OrderServiceError,
    ReturnNotAllowedError,
    ReturnReasonRequiredError,
)
from user_panel.orders.financials import get_order_financials
from user_panel.orders.models import (
    Order,
    OrderItem,
    OrderStatusEvent,
    ReturnItem,
    ReturnRequest,
)
from user_panel.orders.returns import approve_return, reject_return, request_return, request_return_order

User = get_user_model()


def make_user(username="customer_user", **overrides):
    values = {
        "username": username,
        "email": f"{username}@example.com",
        "password": "pass1234Password!",
    }
    values.update(overrides)
    return User.objects.create_user(**values)


def make_catalogue(*, sku="SKU-63-1", price="1500.00", stock=10, name="Aura Elixir"):
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
        size="100ml",
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )
    return category, brand, product, variant


def make_variant(product, *, sku, price="1500.00", stock=10, size="50ml"):
    return ProductVariant.objects.create(
        product=product,
        size=size,
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )


def make_order(user, order_number="AURA-20261103-00000001", *, total="1500.00", status=None, payment_status=None):
    return Order.objects.create(
        user=user,
        order_number=order_number,
        status=status or Order.OrderStatus.PENDING,
        payment_method=Order.PaymentMethod.COD,
        payment_status=payment_status or Order.PaymentStatus.UNPAID,
        subtotal=Decimal(total),
        total=Decimal(total),
        delivery_full_name="Jane Doe",
        delivery_phone_number="9876543210",
        delivery_address_line1="42 Amber Boulevard",
        delivery_address_line2="Apt 4B",
        delivery_landmark="Near Clocktower",
        delivery_city="Bengaluru",
        delivery_state="Karnataka",
        delivery_pincode="560001",
        delivery_address_type=Order.DeliveryAddressType.HOME,
    )


def add_item(order, variant, *, quantity=1, product_name=None, brand_name=None, unit_price=None):
    price = Decimal(unit_price) if unit_price is not None else variant.price
    return OrderItem.objects.create(
        order=order,
        variant=variant,
        product_name=product_name or variant.product.name,
        brand_name=brand_name or variant.product.brand.name,
        variant_size=variant.size,
        sku=variant.sku,
        unit_price=price,
        quantity=quantity,
        line_total=price * quantity,
    )


class OrderDetailsAccessAndSecurityTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.other_user = make_user("bob")
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(self.user, "AURA-20261103-ACC00001")
        self.item = add_item(self.order, self.variant, quantity=1)

    def test_unauthenticated_user_redirected_to_login(self):
        self.client.logout()
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("user_auth:login"), response.url)

    def test_customer_can_view_own_order_details(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.order_number)
        self.assertContains(response, self.item.product_name)
        self.assertContains(response, "42 Amber Boulevard")

    def test_customer_cannot_view_other_customers_order_idor_prevention(self):
        self.client.force_login(self.other_user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_customer_cannot_cancel_other_customers_item(self):
        self.client.force_login(self.other_user)
        url = reverse("orders:cancel_item", args=[self.order.order_number, self.item.pk])
        response = self.client.post(url, {"reason": "Malicious attempt"})
        self.assertEqual(response.status_code, 404)
        self.item.refresh_from_db()
        self.assertFalse(self.item.is_cancelled)

    def test_customer_cannot_return_other_customers_item(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status"])
        self.client.force_login(self.other_user)
        url = reverse("orders:return_item", args=[self.order.order_number, self.item.pk])
        response = self.client.post(url, {"reason": "Cross-customer return"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ReturnItem.objects.count(), 0)

    def test_staff_user_is_redirected_by_panel_access_middleware(self):
        staff = make_user("staff_admin", is_staff=True)
        self.client.force_login(staff)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard:index"))

    def test_cross_order_item_mismatch_returns_404(self):
        other_order = make_order(self.user, "AURA-20261103-ACC00002")
        self.client.force_login(self.user)
        # Attempt to cancel self.item using other_order's URL
        url = reverse("orders:cancel_item", args=[other_order.order_number, self.item.pk])
        response = self.client.post(url, {"reason": "Mismatch"})
        self.assertEqual(response.status_code, 404)

    def test_post_only_enforced_on_cancel_and_return_endpoints(self):
        self.client.force_login(self.user)
        cancel_url = reverse("orders:cancel_item", args=[self.order.order_number, self.item.pk])
        return_url = reverse("orders:return_item", args=[self.order.order_number, self.item.pk])
        self.assertEqual(self.client.get(cancel_url).status_code, 405)
        self.assertEqual(self.client.get(return_url).status_code, 405)


class OrderDetailsRenderingAndSnapshotsTests(TestCase):
    def setUp(self):
        self.user = make_user("charlie")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            sku="SKU-SNAP-1",
            price="2450.00",
            name="Velvet Oud",
        )
        self.order = make_order(self.user, "AURA-20261103-SNAP0001", total="4900.00")
        self.item = add_item(
            self.order,
            self.variant,
            quantity=2,
            product_name="Velvet Oud Snapshot",
            brand_name="Aura Artisanal",
            unit_price="2450.00",
        )

    def test_persisted_item_snapshots_rendered_accurately(self):
        # Alter the live catalog to ensure historical snapshots are preserved
        self.product.name = "Mutated Product Name"
        self.product.save(update_fields=["name"])
        self.variant.price = Decimal("9999.00")
        self.variant.size = "200ml"
        self.variant.save(update_fields=["price", "size"])

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Velvet Oud Snapshot")
        self.assertContains(response, "Aura Artisanal")
        self.assertContains(response, "100ml")
        self.assertContains(response, "SKU-SNAP-1")
        self.assertContains(response, "2450.00")
        self.assertContains(response, "4900.00")
        self.assertNotContains(response, "Mutated Product Name")

    def test_delivery_address_snapshot_rendered(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)
        self.assertContains(response, "Jane Doe")
        self.assertContains(response, "42 Amber Boulevard")
        self.assertContains(response, "Apt 4B")
        self.assertContains(response, "Near Clocktower")
        self.assertContains(response, "Bengaluru, Karnataka - 560001")
        self.assertContains(response, "9876543210")

    def test_my_orders_details_button_links_to_order_detail_page(self):
        self.client.force_login(self.user)
        listing_url = reverse("orders:my_orders")
        response = self.client.get(listing_url)
        expected_detail_url = reverse("orders:order_detail", args=[self.order.order_number])
        self.assertContains(response, f'href="{expected_detail_url}"')
        self.assertNotContains(response, "Order details will be available soon")


class FulfilmentTimelineTests(TestCase):
    def setUp(self):
        self.user = make_user("timeline_user")
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(self.user, "AURA-20261103-TIME0001")
        self.item = add_item(self.order, self.variant)

    def test_timeline_uses_order_created_at_for_ordered_milestone(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        timeline_steps = response.context["timeline_steps"]
        ordered_step = timeline_steps[0]
        self.assertEqual(ordered_step["label"], "Ordered")
        self.assertTrue(ordered_step["is_completed"])
        self.assertEqual(ordered_step["timestamp"], self.order.created_at)

    def test_timeline_never_infers_unrecorded_events(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status"])

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        timeline_steps = response.context["timeline_steps"]
        steps_by_label = {s["label"]: s for s in timeline_steps}

        self.assertTrue(steps_by_label["Ordered"]["is_completed"])
        self.assertFalse(steps_by_label["Shipped"]["is_completed"])
        self.assertFalse(steps_by_label["Out for Delivery"]["is_completed"])
        self.assertFalse(steps_by_label["Delivered"]["is_completed"])

    def test_timeline_reflects_actual_order_status_events(self):
        staff = make_user("timeline_staff", is_staff=True)
        event1 = OrderStatusEvent.objects.create(
            order=self.order,
            status=Order.OrderStatus.SHIPPED,
            previous_status=Order.OrderStatus.PENDING,
            changed_by=staff,
            note="Dispatched via express courier.",
        )
        event2 = OrderStatusEvent.objects.create(
            order=self.order,
            status=Order.OrderStatus.OUT_FOR_DELIVERY,
            previous_status=Order.OrderStatus.SHIPPED,
            changed_by=staff,
            note="With delivery partner.",
        )
        self.order.status = Order.OrderStatus.OUT_FOR_DELIVERY
        self.order.save(update_fields=["status"])

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        steps_by_label = {s["label"]: s for s in response.context["timeline_steps"]}
        self.assertTrue(steps_by_label["Ordered"]["is_completed"])
        self.assertTrue(steps_by_label["Shipped"]["is_completed"])
        self.assertEqual(steps_by_label["Shipped"]["timestamp"], event1.created_at)
        self.assertEqual(steps_by_label["Shipped"]["note"], "Dispatched via express courier.")
        self.assertTrue(steps_by_label["Out for Delivery"]["is_completed"])
        self.assertEqual(steps_by_label["Out for Delivery"]["timestamp"], event2.created_at)
        self.assertFalse(steps_by_label["Delivered"]["is_completed"])

    def test_cancelled_order_timeline_shows_ordered_and_cancelled(self):
        cancel_order_item(self.user, self.item.pk, reason="No longer required")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.CANCELLED)

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        labels = [s["label"] for s in response.context["timeline_steps"]]
        self.assertEqual(labels, ["Ordered", "Cancelled"])
        self.assertTrue(response.context["timeline_steps"][1]["is_completed"])


class ProductLevelStatusOutcomeTests(TestCase):
    def setUp(self):
        self.user = make_user("outcome_user")
        self.admin = make_user("admin_user", is_staff=True)
        self.category, self.brand, self.product, self.variant1 = make_catalogue(stock=20)
        self.variant2 = make_variant(self.product, sku="SKU-OUT-VAR2", stock=15)
        self.order = make_order(self.user, "AURA-20261103-OUT0001", total="6000.00")
        self.item1 = add_item(self.order, self.variant1, quantity=1, product_name="Item Normal")
        self.item2 = add_item(self.order, self.variant2, quantity=1, product_name="Item Cancelled")

    def test_normal_item_displays_order_fulfilment_status(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)
        self.assertContains(response, "Item Normal")
        item_rows = response.context["item_rows"]
        row1 = next(r for r in item_rows if r["item"].pk == self.item1.pk)
        self.assertEqual(row1["outcome_status"], "Pending")

    def test_cancelled_item_displays_cancelled_outcome(self):
        cancel_order_item(self.user, self.item2.pk, reason="Changed mind")
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        item_rows = response.context["item_rows"]
        row2 = next(r for r in item_rows if r["item"].pk == self.item2.pk)
        self.assertEqual(row2["outcome_status"], "Cancelled")
        self.assertContains(response, "Changed mind")

    def test_return_requested_outcome_display(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status"])

        request_return(self.user, self.item1.pk, reason="Defective spray nozzle")
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        item_rows = response.context["item_rows"]
        row1 = next(r for r in item_rows if r["item"].pk == self.item1.pk)
        self.assertEqual(row1["outcome_status"], "Return Requested")
        self.assertContains(response, "Return Requested")

    def test_return_approved_outcome_display(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status"])

        ret_req = request_return(self.user, self.item1.pk, reason="Wrong size received")
        ret_item = ret_req.items.get()
        ret_item.status = ReturnItem.Status.APPROVED
        ret_item.decided_at = timezone.now()
        ret_item.decided_by = self.admin
        ret_item.stock_restored_at = timezone.now()
        ret_item.save()

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        item_rows = response.context["item_rows"]
        row1 = next(r for r in item_rows if r["item"].pk == self.item1.pk)
        self.assertEqual(row1["outcome_status"], "Return Approved")
        self.assertContains(response, "Return Approved")

    def test_return_rejected_outcome_with_rejection_reason_display(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status"])

        ret_req = request_return(self.user, self.item1.pk, reason="Used bottle returned")
        ret_item = ret_req.items.get()
        reject_return(ret_item.pk, admin=self.admin, note="Item seal was opened and consumed.")

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        item_rows = response.context["item_rows"]
        row1 = next(r for r in item_rows if r["item"].pk == self.item1.pk)
        self.assertEqual(row1["outcome_status"], "Return Rejected")
        self.assertEqual(row1["rejection_note"], "Item seal was opened and consumed.")
        self.assertContains(response, "Return Rejected")
        self.assertContains(response, "Item seal was opened and consumed.")


class CustomerCancellationFlowTests(TestCase):
    def setUp(self):
        self.user = make_user("canceller")
        self.category, self.brand, self.product, self.variant1 = make_catalogue(stock=5)
        self.variant2 = make_variant(self.product, sku="SKU-CNC-VAR2", stock=5)
        self.order = make_order(self.user, "AURA-20261103-CNC0001", total="3000.00")
        self.item1 = add_item(self.order, self.variant1, quantity=1, unit_price="1500.00")
        self.item2 = add_item(self.order, self.variant2, quantity=1, unit_price="1500.00")

    def test_customer_can_cancel_item_in_pending_order_with_optional_reason(self):
        self.client.force_login(self.user)
        url = reverse("orders:cancel_item", args=[self.order.order_number, self.item1.pk])
        response = self.client.post(url, {"reason": "Ordered wrong fragrance"})

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        self.item1.refresh_from_db()
        self.assertTrue(self.item1.is_cancelled)
        self.assertEqual(self.item1.cancellation_reason, "Ordered wrong fragrance")
        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 6)  # 5 + 1 restored

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.PENDING)

    def test_cancelling_without_reason_is_permitted(self):
        self.client.force_login(self.user)
        url = reverse("orders:cancel_item", args=[self.order.order_number, self.item1.pk])
        response = self.client.post(url, {"reason": ""})

        self.assertEqual(response.status_code, 302)
        self.item1.refresh_from_db()
        self.assertTrue(self.item1.is_cancelled)
        self.assertEqual(self.item1.cancellation_reason, "")

    def test_cancelling_last_active_item_transitions_order_to_cancelled(self):
        self.client.force_login(self.user)
        cancel_url1 = reverse("orders:cancel_item", args=[self.order.order_number, self.item1.pk])
        cancel_url2 = reverse("orders:cancel_item", args=[self.order.order_number, self.item2.pk])

        self.client.post(cancel_url1, {"reason": ""})
        self.client.post(cancel_url2, {"reason": "All done"})

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.CANCELLED)

        event = self.order.status_events.filter(status=Order.OrderStatus.CANCELLED).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.changed_by, self.user)

    def test_cannot_cancel_item_if_order_is_shipped_or_delivered(self):
        self.order.status = Order.OrderStatus.SHIPPED
        self.order.save(update_fields=["status"])

        self.client.force_login(self.user)
        url = reverse("orders:cancel_item", args=[self.order.order_number, self.item1.pk])
        response = self.client.post(url, {"reason": "Too late"})

        self.assertEqual(response.status_code, 302)
        self.item1.refresh_from_db()
        self.assertFalse(self.item1.is_cancelled)
        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 5)

    def test_cannot_cancel_already_cancelled_item(self):
        cancel_order_item(self.user, self.item1.pk)
        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 6)

        self.client.force_login(self.user)
        url = reverse("orders:cancel_item", args=[self.order.order_number, self.item1.pk])
        response = self.client.post(url, {"reason": "Duplicate cancel"})

        self.assertEqual(response.status_code, 302)
        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 6)  # Exactly once stock restoration

    def test_cancel_item_restores_stock_to_soft_deleted_variant(self):
        self.variant1.is_deleted = True
        self.variant1.save(update_fields=["is_deleted"])

        self.client.force_login(self.user)
        url = reverse("orders:cancel_item", args=[self.order.order_number, self.item1.pk])
        self.client.post(url, {"reason": "Soft deleted test"})

        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 6)


class CustomerReturnFlowTests(TestCase):
    def setUp(self):
        self.user = make_user("returner")
        self.admin = make_user("return_admin", is_staff=True)
        self.category, self.brand, self.product, self.variant = make_catalogue(stock=8)
        self.order = make_order(
            self.user,
            "AURA-20261103-RET0001",
            total="3000.00",
            status=Order.OrderStatus.DELIVERED,
            payment_status=Order.PaymentStatus.PAID,
        )
        self.item = add_item(self.order, self.variant, quantity=2, unit_price="1500.00")

    def test_customer_can_request_return_on_delivered_order(self):
        self.client.force_login(self.user)
        url = reverse("orders:return_item", args=[self.order.order_number, self.item.pk])
        response = self.client.post(url, {"reason": "Product arrived damaged in transit"})

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        ret_item = ReturnItem.objects.get(order_item=self.item)
        self.assertEqual(ret_item.quantity, 2)
        self.assertEqual(ret_item.reason, "Product arrived damaged in transit")
        self.assertEqual(ret_item.status, ReturnItem.Status.REQUESTED)

        # Stock is NOT restored upon request submission
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 8)
        self.assertIsNone(ret_item.stock_restored_at)

        # Order status remains DELIVERED
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.DELIVERED)

    def test_return_reason_is_mandatory(self):
        self.client.force_login(self.user)
        url = reverse("orders:return_item", args=[self.order.order_number, self.item.pk])

        # Blank reason
        response = self.client.post(url, {"reason": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReturnItem.objects.count(), 0)

        # Whitespace-only reason
        response = self.client.post(url, {"reason": "   \n\t  "})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReturnItem.objects.count(), 0)

    def test_cannot_request_return_if_order_not_delivered(self):
        self.order.status = Order.OrderStatus.SHIPPED
        self.order.save(update_fields=["status"])

        self.client.force_login(self.user)
        url = reverse("orders:return_item", args=[self.order.order_number, self.item.pk])
        response = self.client.post(url, {"reason": "Too early"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReturnItem.objects.count(), 0)

    def test_cannot_return_cancelled_item(self):
        self.item.cancelled_at = timezone.now()
        self.item.save(update_fields=["cancelled_at"])

        self.client.force_login(self.user)
        url = reverse("orders:return_item", args=[self.order.order_number, self.item.pk])
        response = self.client.post(url, {"reason": "Returning cancelled line"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReturnItem.objects.count(), 0)

    def test_cannot_resubmit_return_after_rejection(self):
        ret_req = request_return(self.user, self.item.pk, reason="Initial request")
        ret_item = ret_req.items.get()
        reject_return(ret_item.pk, admin=self.admin, note="Not eligible under return policy.")

        self.client.force_login(self.user)
        url = reverse("orders:return_item", args=[self.order.order_number, self.item.pk])
        response = self.client.post(url, {"reason": "Resubmission attempt"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReturnRequest.objects.count(), 1)
        self.assertEqual(ReturnItem.objects.count(), 1)


class ReturnModalWordingAndMarkupTests(TestCase):
    def setUp(self):
        self.user = make_user("modal_user")
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(
            self.user,
            "AURA-20261103-MOD0001",
            status=Order.OrderStatus.DELIVERED,
        )
        self.item = add_item(self.order, self.variant, product_name="Signature Scent")

    def test_modal_has_exact_required_wording_and_no_predefined_options(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)

        # Exact heading
        self.assertContains(response, "Return Item?")

        # Description template pattern
        self.assertContains(response, "Please provide a reason to return")

        # Exact uppercase label
        self.assertContains(response, "REASON FOR RETURN *")

        # Textarea placeholder
        self.assertContains(response, 'placeholder="Enter return reason..."')

        # Action buttons
        self.assertContains(response, "SUBMIT RETURN")
        self.assertContains(response, "CANCEL")

        # Ensure no dropdowns, select tags, or radio buttons exist in the modal
        content = response.content.decode("utf-8")
        modal_content = content[content.find('id="aura-return-modal"'):content.find('id="aura-cancel-modal"')]
        self.assertNotIn("<select", modal_content)
        self.assertNotIn('type="radio"', modal_content)


class FinancialSummaryDisplayTests(TestCase):
    def setUp(self):
        self.user = make_user("finance_user")
        self.admin = make_user("finance_admin", is_staff=True)
        self.category, self.brand, self.product, self.variant1 = make_catalogue(stock=20)
        self.variant2 = make_variant(self.product, sku="SKU-FIN-VAR2", stock=20)
        self.variant3 = make_variant(self.product, sku="SKU-FIN-VAR3", stock=20)
        self.order = make_order(
            self.user,
            "AURA-20261103-FIN0001",
            total="4500.00",
            status=Order.OrderStatus.DELIVERED,
            payment_status=Order.PaymentStatus.PAID,
        )
        self.item1 = add_item(self.order, self.variant1, quantity=1, unit_price="1500.00")
        self.item2 = add_item(self.order, self.variant2, quantity=1, unit_price="1500.00")
        self.item3 = add_item(self.order, self.variant3, quantity=1, unit_price="1500.00")

    def test_financial_summary_displays_correct_adjusted_figures_without_persistence(self):
        # item1: Cancelled
        self.item1.cancelled_at = timezone.now()
        self.item1.save(update_fields=["cancelled_at"])

        # item2: Approved return
        ret_req = request_return(self.user, self.item2.pk, reason="Valid return")
        ret_item = ret_req.items.get()
        ret_item.status = ReturnItem.Status.APPROVED
        ret_item.decided_at = timezone.now()
        ret_item.decided_by = self.admin
        ret_item.stock_restored_at = timezone.now()
        ret_item.save()

        # item3: Requested return (not approved yet, so does NOT reduce total)
        request_return(self.user, self.item3.pk, reason="Pending review")

        financials = get_order_financials(self.order)
        self.assertEqual(financials.original_total, Decimal("4500.00"))
        self.assertEqual(financials.cancelled_total, Decimal("1500.00"))
        self.assertEqual(financials.approved_return_total, Decimal("1500.00"))
        self.assertEqual(financials.adjusted_total, Decimal("1500.00"))

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        self.assertContains(response, "4500.00")  # Original total
        self.assertContains(response, "-₹1500.00")  # Cancelled total
        self.assertContains(response, "-₹1500.00")  # Approved return total
        self.assertContains(response, "1500.00")  # Current adjusted total

        # Ensure order total on the database record was never modified
        self.order.refresh_from_db()
        self.assertEqual(self.order.total, Decimal("4500.00"))


class OrderLevelActionsTests(TestCase):
    """Tests for order-level CANCEL ORDER, RETURN ORDER, and DOWNLOAD INVOICE actions."""

    def setUp(self):
        self.user = make_user("order_level_user")
        self.other_user = make_user("other_order_user")
        self.category, self.brand, self.product, self.variant1 = make_catalogue(
            sku="SKU-OLA-1", stock=15
        )
        self.variant2 = make_variant(self.product, sku="SKU-OLA-2", stock=20)
        self.order = make_order(
            self.user,
            "AURA-20261103-OLA0001",
            status=Order.OrderStatus.PENDING,
            total="3000.00",
        )
        self.item1 = add_item(self.order, self.variant1, quantity=2, unit_price="1000.00")
        self.item2 = add_item(self.order, self.variant2, quantity=1, unit_price="1000.00")

    def test_cancel_order_button_visible_only_when_pending_and_active_items_remain(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])

        # 1. PENDING with active items -> CANCEL ORDER visible
        response = self.client.get(url)
        self.assertContains(response, "CANCEL ORDER")
        self.assertContains(response, 'onclick="openCancelOrderModal()"')

        # 2. When all items are cancelled in a PENDING order -> CANCEL ORDER hidden
        self.item1.cancelled_at = timezone.now()
        self.item1.save()
        self.item2.cancelled_at = timezone.now()
        self.item2.save()
        response = self.client.get(url)
        self.assertNotContains(response, 'onclick="openCancelOrderModal()"')
        self.assertNotContains(response, "CANCEL ORDER")

        # 3. SHIPPED -> CANCEL ORDER hidden
        self.order.status = Order.OrderStatus.SHIPPED
        self.order.save()
        response = self.client.get(url)
        self.assertNotContains(response, 'onclick="openCancelOrderModal()"')
        self.assertNotContains(response, "CANCEL ORDER")

        # 4. DELIVERED -> CANCEL ORDER hidden
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()
        response = self.client.get(url)
        self.assertNotContains(response, 'onclick="openCancelOrderModal()"')
        self.assertNotContains(response, "CANCEL ORDER")

        # 5. CANCELLED -> CANCEL ORDER hidden
        self.order.status = Order.OrderStatus.CANCELLED
        self.order.save()
        response = self.client.get(url)
        self.assertNotContains(response, 'onclick="openCancelOrderModal()"')
        self.assertNotContains(response, "CANCEL ORDER")

    def test_cancel_order_successfully_cancels_all_active_items_and_restores_stock(self):
        self.client.force_login(self.user)
        cancel_url = reverse("orders:cancel_order", args=[self.order.order_number])

        response = self.client.post(cancel_url, {"reason": "Customer changed mind"})
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.CANCELLED)

        self.item1.refresh_from_db()
        self.item2.refresh_from_db()
        self.assertIsNotNone(self.item1.cancelled_at)
        self.assertIsNotNone(self.item2.cancelled_at)
        self.assertEqual(self.item1.cancellation_reason, "Customer changed mind")
        self.assertEqual(self.item2.cancellation_reason, "Customer changed mind")

        # Stocks restored exactly once
        self.variant1.refresh_from_db()
        self.variant2.refresh_from_db()
        self.assertEqual(self.variant1.stock, 17)  # 15 + 2
        self.assertEqual(self.variant2.stock, 21)  # 20 + 1

        # Event logged
        event = OrderStatusEvent.objects.filter(order=self.order, status=Order.OrderStatus.CANCELLED).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.changed_by, self.user)
        self.assertIn("Customer changed mind", event.note)

    def test_cancel_order_duplicate_call_does_not_double_restore_stock(self):
        # First cancellation
        cancel_order(self.user, self.order.pk, reason="First cancel")

        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 17)

        # Second cancellation must fail and not double stock
        with self.assertRaises(ItemNotCancellableError):
            cancel_order(self.user, self.order.pk, reason="Second cancel")

        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 17)

    def test_cancel_order_atomic_rollback_on_failure(self):
        # Simulate variant restoration failure mid-operation
        with patch("user_panel.orders.cancellation._restore_stock", side_effect=OrderServiceError("DB Error")):
            with self.assertRaises(OrderServiceError):
                cancel_order(self.user, self.order.pk, reason="Failed cancel")

        # Verify rollback: order still PENDING, items still uncancelled, stock untouched
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.PENDING)
        self.item1.refresh_from_db()
        self.assertIsNone(self.item1.cancelled_at)
        self.variant1.refresh_from_db()
        self.assertEqual(self.variant1.stock, 15)

    def test_cancel_order_modal_markup_and_buttons(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        self.assertContains(response, "Cancel Order?")
        self.assertContains(response, "CONFIRM CANCELLATION")
        self.assertContains(response, "KEEP ORDER")
        self.assertContains(response, "All remaining active items in this order will be cancelled")

    def test_return_order_button_visible_only_when_delivered_and_eligible_items_remain(self):
        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])

        # 1. PENDING -> RETURN ORDER hidden
        response = self.client.get(url)
        self.assertNotContains(response, "RETURN ORDER")

        # 2. DELIVERED with unreturned items -> RETURN ORDER visible
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()
        response = self.client.get(url)
        self.assertContains(response, "RETURN ORDER")
        self.assertContains(response, "openReturnOrderModal()")

        # 3. When all items have return requests -> RETURN ORDER hidden
        request_return(self.user, self.item1.pk, reason="Reason 1")
        request_return(self.user, self.item2.pk, reason="Reason 2")
        response = self.client.get(url)
        self.assertNotContains(response, "RETURN ORDER")

    def test_download_invoice_link_is_available_for_delivered_orders(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        invoice_url = reverse(
            "orders:download_invoice",
            args=[self.order.order_number],
        )
        self.assertContains(response, "DOWNLOAD INVOICE")
        self.assertContains(response, f'href="{invoice_url}"')
        self.assertNotContains(response, 'aria-disabled="true"')

    def test_cross_customer_cannot_cancel_order(self):
        self.client.force_login(self.other_user)
        cancel_url = reverse("orders:cancel_order", args=[self.order.order_number])
        response = self.client.post(cancel_url, {"reason": "IDOR attempt"})
        self.assertEqual(response.status_code, 404)

        with self.assertRaises(OrderPermissionError):
            cancel_order(self.other_user, self.order.pk)

    def test_return_order_modal_markup_and_elements(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        self.client.force_login(self.user)
        url = reverse("orders:order_detail", args=[self.order.order_number])
        response = self.client.get(url)

        self.assertContains(response, 'id="aura-return-order-modal"')
        self.assertContains(response, "Return Order?")
        self.assertContains(response, "This action will submit return requests for all eligible products")
        self.assertContains(response, "REASON FOR RETURN *")
        self.assertContains(response, "KEEP ORDER")
        self.assertContains(response, "SUBMIT RETURN")
        self.assertContains(response, reverse("orders:return_order", args=[self.order.order_number]))

    def test_return_order_all_eligible_items(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        self.client.force_login(self.user)
        return_url = reverse("orders:return_order", args=[self.order.order_number])
        response = self.client.post(return_url, {"reason": "Items defective on arrival"})
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        return_requests = ReturnRequest.objects.filter(order=self.order)
        self.assertEqual(return_requests.count(), 1)
        ret_req = return_requests.first()
        self.assertEqual(ret_req.requested_by, self.user)

        # Both items must have ReturnItems created
        return_items = ReturnItem.objects.filter(return_request=ret_req).order_by("order_item_id")
        self.assertEqual(return_items.count(), 2)

        for ri in return_items:
            self.assertEqual(ri.status, ReturnItem.Status.REQUESTED)
            self.assertEqual(ri.reason, "Items defective on arrival")

        # Order fulfilment status remains DELIVERED
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.DELIVERED)

        # Order detail page reflects Return Requested for both items and hides RETURN ORDER button
        detail_response = self.client.get(reverse("orders:order_detail", args=[self.order.order_number]))
        self.assertContains(detail_response, "Return Requested", count=2)
        self.assertNotContains(detail_response, "RETURN ORDER")

    def test_return_order_mandatory_reason(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        self.client.force_login(self.user)
        return_url = reverse("orders:return_order", args=[self.order.order_number])

        # Blank reason fails
        response = self.client.post(return_url, {"reason": "   "})
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        self.assertEqual(ReturnRequest.objects.filter(order=self.order).count(), 0)
        self.assertEqual(ReturnItem.objects.count(), 0)

        # Service level directly raises ReturnReasonRequiredError
        with self.assertRaises(ReturnReasonRequiredError):
            request_return_order(self.user, self.order.pk, reason="")

    def test_return_order_partial_eligibility_with_existing_cancelled_and_returned(self):
        # Add a 3rd item to the order with distinct variant
        _, _, _, variant3 = make_catalogue(sku="SKU-63-3", name="Third Item", price="2000.00")
        item3 = add_item(self.order, variant3, quantity=1, unit_price="2000.00")
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        # Item 1 was cancelled earlier
        self.item1.cancelled_at = timezone.now()
        self.item1.cancellation_reason = "Customer cancelled before delivery"
        self.item1.save()

        # Item 2 already has an individual ReturnItem
        single_req = request_return(self.user, self.item2.pk, reason="Initial individual return")

        # Now invoke return_order
        self.client.force_login(self.user)
        return_url = reverse("orders:return_order", args=[self.order.order_number])
        response = self.client.post(return_url, {"reason": "Return remaining batch"})
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        # Item 1 remains cancelled, no ReturnItem
        self.item1.refresh_from_db()
        self.assertIsNotNone(self.item1.cancelled_at)
        self.assertFalse(hasattr(self.item1, "return_item"))

        # Item 2 has only its original ReturnItem
        self.item2.refresh_from_db()
        self.assertEqual(self.item2.return_item.reason, "Initial individual return")
        self.assertEqual(self.item2.return_item.return_request, single_req)

        # Item 3 received the new ReturnItem
        item3.refresh_from_db()
        self.assertTrue(hasattr(item3, "return_item"))
        self.assertEqual(item3.return_item.reason, "Return remaining batch")
        self.assertEqual(item3.return_item.status, ReturnItem.Status.REQUESTED)

    def test_return_order_preserves_previously_approved_or_rejected_items(self):
        # Create an admin user for decisions
        admin = User.objects.create_superuser(
            username="admin_super",
            email="admin_super@example.com",
            password="pass1234Password!",
        )

        # Add a 3rd item with distinct variant
        _, _, _, variant3 = make_catalogue(sku="SKU-63-3-APR", name="Third Item", price="1500.00")
        item3 = add_item(self.order, variant3, quantity=1, unit_price="1500.00")
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        # Item 1 requested and APPROVED
        request_return(self.user, self.item1.pk, reason="Reason 1")
        approve_return(self.item1.return_item.pk, admin=admin, note="Approved by admin")

        # Item 2 requested and REJECTED
        request_return(self.user, self.item2.pk, reason="Reason 2")
        reject_return(self.item2.return_item.pk, admin=admin, note="Defect not verifiable")

        # Now call return_order
        self.client.force_login(self.user)
        return_url = reverse("orders:return_order", args=[self.order.order_number])
        response = self.client.post(return_url, {"reason": "Return item 3"})
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        # Item 1 is still APPROVED
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.return_item.status, ReturnItem.Status.APPROVED)

        # Item 2 is still REJECTED with note
        self.item2.refresh_from_db()
        self.assertEqual(self.item2.return_item.status, ReturnItem.Status.REJECTED)
        self.assertEqual(self.item2.return_item.decision_note, "Defect not verifiable")

        # Item 3 received the new ReturnItem
        item3.refresh_from_db()
        self.assertTrue(hasattr(item3, "return_item"))
        self.assertEqual(item3.return_item.status, ReturnItem.Status.REQUESTED)

    def test_return_order_no_eligible_items_fails_gracefully(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        # All items already have ReturnItem
        request_return(self.user, self.item1.pk, reason="R1")
        request_return(self.user, self.item2.pk, reason="R2")

        self.client.force_login(self.user)
        return_url = reverse("orders:return_order", args=[self.order.order_number])
        response = self.client.post(return_url, {"reason": "Attempt return again"})
        self.assertRedirects(response, reverse("orders:order_detail", args=[self.order.order_number]))

        # Count of ReturnRequests is still 2 (from the 2 individual calls), no 3rd created
        self.assertEqual(ReturnRequest.objects.filter(order=self.order).count(), 2)

        # Service level directly raises ReturnNotAllowedError
        with self.assertRaises(ReturnNotAllowedError):
            request_return_order(self.user, self.order.pk, reason="Attempt again")

    def test_return_order_duplicate_submission_protection(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        # First service call succeeds
        ret_req = request_return_order(self.user, self.order.pk, reason="First batch submit")
        self.assertIsNotNone(ret_req)
        self.assertEqual(ReturnItem.objects.filter(return_request=ret_req).count(), 2)

        # Second service call fails because no eligible items remain
        with self.assertRaises(ReturnNotAllowedError):
            request_return_order(self.user, self.order.pk, reason="Duplicate submit")

        # Return items count remains exactly 2
        self.assertEqual(ReturnItem.objects.count(), 2)

    def test_return_order_atomic_rollback_on_failure(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        # Simulate unexpected failure during ReturnItem creation
        original_create = ReturnItem.objects.create
        call_count = [0]

        def failing_create(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Simulated database failure during batch return")
            return original_create(*args, **kwargs)

        with patch.object(ReturnItem.objects, "create", side_effect=failing_create):
            with self.assertRaises(RuntimeError):
                request_return_order(self.user, self.order.pk, reason="Atomic test")

        # Verify atomic rollback: 0 ReturnRequests and 0 ReturnItems exist
        self.assertEqual(ReturnRequest.objects.filter(order=self.order).count(), 0)
        self.assertEqual(ReturnItem.objects.count(), 0)

    def test_cross_customer_cannot_return_order(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        self.client.force_login(self.other_user)
        return_url = reverse("orders:return_order", args=[self.order.order_number])
        response = self.client.post(return_url, {"reason": "IDOR return attempt"})
        self.assertEqual(response.status_code, 404)

        with self.assertRaises(OrderPermissionError):
            request_return_order(self.other_user, self.order.pk, reason="IDOR attempt")

    def test_unauthenticated_user_redirected_to_login_on_return_order(self):
        return_url = reverse("orders:return_order", args=[self.order.order_number])
        response = self.client.post(return_url, {"reason": "Anon return attempt"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_post_only_enforced_on_return_order(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save()

        self.client.force_login(self.user)
        return_url = reverse("orders:return_order", args=[self.order.order_number])
        response = self.client.get(return_url)
        self.assertEqual(response.status_code, 405)

