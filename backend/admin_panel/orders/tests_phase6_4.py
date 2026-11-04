"""Phase 6.4: Admin Orders Management Tests.

Covers:
- Admin access control & security
- Admin Orders Listing (1 row = 1 order, search, filter, sort, pagination, KPIs, representative preview)
- Admin Order Details (snapshots, items, return/cancellation visibility, financials, payment, timeline)
- Fulfilment status transitions (PENDING -> SHIPPED -> OUT_FOR_DELIVERY -> DELIVERED)
- COD payment collection on delivery
- Timeline states across all 5 operational conditions
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant
from user_panel.orders.cancellation import cancel_order_item
from user_panel.orders.financials import get_order_financials
from user_panel.orders.models import (
    Order,
    OrderItem,
    OrderStatusEvent,
    ReturnItem,
    ReturnRequest,
)
from user_panel.orders.order_lifecycle import transition_order_status
from user_panel.orders.returns import approve_return, reject_return, request_return

User = get_user_model()


def make_user(username="customer_one", **overrides):
    values = {
        "username": username,
        "email": f"{username}@example.com",
        "first_name": "Test",
        "last_name": "Customer",
        "password": "pass1234Password!",
    }
    values.update(overrides)
    return User.objects.create_user(**values)


def make_admin(username="admin_one", **overrides):
    return make_user(username, is_staff=True, is_superuser=True, **overrides)


def make_catalogue(*, sku="SKU-64-1", price="2500.00", stock=20, name="Noir Extrait"):
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


def make_order(
    user,
    order_number="AURA-20261104-00000001",
    *,
    total="2500.00",
    status=None,
    payment_status=None,
    delivery_name="Jane Doe",
):
    return Order.objects.create(
        user=user,
        order_number=order_number,
        status=status or Order.OrderStatus.PENDING,
        payment_method=Order.PaymentMethod.COD,
        payment_status=payment_status or Order.PaymentStatus.UNPAID,
        subtotal=Decimal(total),
        total=Decimal(total),
        delivery_full_name=delivery_name,
        delivery_phone_number="9876543210",
        delivery_address_line1="100 Fragrance Avenue",
        delivery_address_line2="Suite 5A",
        delivery_landmark="Near Botanical Gardens",
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


class AdminOrdersAccessTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.customer = make_user()
        _, _, self.product, self.variant = make_catalogue()
        self.order = make_order(self.customer)
        add_item(self.order, self.variant)

    def test_unauthenticated_user_redirected_to_admin_login(self):
        url = reverse("admin_orders:list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin-panel/auth/", response.url)

    def test_customer_cannot_access_admin_orders(self):
        self.client.force_login(self.customer)
        url = reverse("admin_orders:list")
        response = self.client.get(url)
        # Blocked or redirected by PanelAccessMiddleware / admin_required
        self.assertIn(response.status_code, [302, 403])

    def test_admin_can_access_admin_orders_list_and_detail(self):
        self.client.force_login(self.admin)
        list_url = reverse("admin_orders:list")
        resp_list = self.client.get(list_url)
        self.assertEqual(resp_list.status_code, 200)
        self.assertContains(resp_list, "Order Management")

        detail_url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        resp_detail = self.client.get(detail_url)
        self.assertEqual(resp_detail.status_code, 200)
        self.assertContains(resp_detail, self.order.order_number)


class AdminOrdersListingTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.customer = make_user("sajad_cust", email="sajad@example.com")
        self.client.force_login(self.admin)

        _, _, self.product, self.variant1 = make_catalogue(sku="SKU-L1", price="1000.00", name="Perfume Alpha")
        self.variant2 = make_variant(self.product, sku="SKU-L2", price="2000.00")

        # Order 1: 2 items, PENDING
        self.order1 = make_order(self.customer, order_number="AURA-20261104-00000001", total="3000.00", status=Order.OrderStatus.PENDING)
        add_item(self.order1, self.variant1, quantity=1)
        add_item(self.order1, self.variant2, quantity=1)

        # Order 2: 1 item, DELIVERED + PAID
        self.order2 = make_order(self.customer, order_number="AURA-20261104-00000002", total="1000.00", status=Order.OrderStatus.DELIVERED, payment_status=Order.PaymentStatus.PAID)
        self.order2.payment_collected_amount = Decimal("1000.00")
        self.order2.payment_collected_at = timezone.now()
        self.order2.payment_collected_by = self.admin
        self.order2.save()
        add_item(self.order2, self.variant1, quantity=1)

        # Order 3: 1 item, CANCELLED
        self.order3 = make_order(self.customer, order_number="AURA-20261104-00000003", total="1000.00", status=Order.OrderStatus.CANCELLED)
        add_item(self.order3, self.variant1, quantity=1)

    def test_order_list_one_row_per_order(self):
        url = reverse("admin_orders:list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        rows = response.context["order_rows"]
        # Exactly 3 orders exist
        self.assertEqual(len(rows), 3)
        # Order 1 has 2 items but is exactly 1 row
        row1 = next(r for r in rows if r["order"].order_number == self.order1.order_number)
        self.assertEqual(row1["total_items_count"], 2)
        self.assertEqual(row1["more_items_count"], 1)

    def test_order_list_representative_product_preview(self):
        url = reverse("admin_orders:list")
        response = self.client.get(url)
        self.assertContains(response, "Perfume Alpha")
        self.assertContains(response, "+1 more")

    def test_order_list_kpi_calculations_accurate(self):
        url = reverse("admin_orders:list")
        response = self.client.get(url)
        ctx = response.context
        self.assertEqual(ctx["kpi_total_orders"], 3)
        self.assertEqual(ctx["kpi_pending_shipments"], 1)  # Only order1 is PENDING
        self.assertEqual(ctx["kpi_total_revenue"], Decimal("1000.00"))  # Only order2 is PAID
        self.assertEqual(ctx["kpi_returned_cancelled"], 1)  # Only order3 is CANCELLED

    def test_order_list_search_by_order_number(self):
        url = reverse("admin_orders:list") + "?search=00000002"
        response = self.client.get(url)
        rows = response.context["order_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order"].order_number, self.order2.order_number)

    def test_order_list_search_by_customer_email(self):
        url = reverse("admin_orders:list") + "?search=sajad@example.com"
        response = self.client.get(url)
        rows = response.context["order_rows"]
        self.assertEqual(len(rows), 3)

    def test_order_list_filter_by_status(self):
        url = reverse("admin_orders:list") + "?status=PENDING"
        response = self.client.get(url)
        rows = response.context["order_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order"].status, Order.OrderStatus.PENDING)

    def test_order_list_sorting(self):
        url = reverse("admin_orders:list") + "?sort=total_high"
        response = self.client.get(url)
        rows = response.context["order_rows"]
        # Order 1 has total 3000.00, should be first
        self.assertEqual(rows[0]["order"].order_number, self.order1.order_number)

    def test_sorting_by_amount_uses_adjusted_total_consistently(self):
        # Create an order with original 5000, but cancel 4000 so adjusted is 1000
        order_large = make_order(self.customer, order_number="AURA-SORT-LARGE", total="5000.00")
        item_lg1 = add_item(order_large, self.variant1, unit_price="1000.00", quantity=1)
        item_lg2 = add_item(order_large, self.variant2, unit_price="4000.00", quantity=1)
        cancel_order_item(self.customer, item_lg2.pk, reason="Cancelled big item")

        # Order 1 has adjusted 3000.00
        # order_large now has adjusted 1000.00 (though original total was 5000.00)
        url_high = reverse("admin_orders:list") + "?sort=total_high"
        resp_high = self.client.get(url_high)
        rows_high = resp_high.context["order_rows"]
        # Order 1 (3000.00) must appear BEFORE order_large (1000.00) when sorted by highest amount
        idx_order1 = next(i for i, r in enumerate(rows_high) if r["order"].order_number == self.order1.order_number)
        idx_large = next(i for i, r in enumerate(rows_high) if r["order"].order_number == order_large.order_number)
        self.assertLess(idx_order1, idx_large)

        url_low = reverse("admin_orders:list") + "?sort=total_low"
        resp_low = self.client.get(url_low)
        rows_low = resp_low.context["order_rows"]
        idx_order1_low = next(i for i, r in enumerate(rows_low) if r["order"].order_number == self.order1.order_number)
        idx_large_low = next(i for i, r in enumerate(rows_low) if r["order"].order_number == order_large.order_number)
        self.assertLess(idx_large_low, idx_order1_low)

    def test_pagination_preserves_search_status_sort_parameters(self):
        # Create 12 additional orders to trigger pagination
        for i in range(12):
            o = make_order(self.customer, order_number=f"AURA-PAGE-{i:03d}", total="1000.00")
            add_item(o, self.variant1)

        url = reverse("admin_orders:list") + "?page=1&search=AURA-PAGE&status=PENDING&sort=total_high"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Check next page link in HTML
        self.assertContains(response, "page=2")
        self.assertContains(response, "search=AURA-PAGE")
        self.assertContains(response, "status=PENDING")
        self.assertContains(response, "sort=total_high")

    def test_revenue_kpi_excludes_undelivered_orders(self):
        # Order in SHIPPED or OUT_FOR_DELIVERY (even if payment fields set) must be excluded from Revenue
        uncollected_order = make_order(self.customer, order_number="AURA-UNCOLLECTED", total="9999.00", status=Order.OrderStatus.SHIPPED)
        add_item(uncollected_order, self.variant1)

        url = reverse("admin_orders:list")
        response = self.client.get(url)
        # Revenue should strictly reflect the delivered PAID order (1000.00), not the 9999.00
        self.assertEqual(response.context["kpi_total_revenue"], Decimal("1000.00"))

    def test_order_list_empty_state_when_no_match(self):
        url = reverse("admin_orders:list") + "?search=NON_EXISTENT_ORDER_XYZ"
        response = self.client.get(url)
        self.assertContains(response, "No orders found")


class AdminOrderDetailTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.customer = make_user("adnan_cust", email="adnan@example.com")
        self.client.force_login(self.admin)

        _, _, self.product, self.variant1 = make_catalogue(sku="SKU-D1", price="2500.00", name="Dior Sauvage Extrait")
        self.variant2 = make_variant(self.product, sku="SKU-D2", price="1500.00", size="50ml")

        self.order = make_order(
            self.customer,
            order_number="AURA-20261104-DETAIL01",
            total="4000.00",
            status=Order.OrderStatus.PENDING,
            delivery_name="Adnan Shakkeer",
        )
        self.item1 = add_item(self.order, self.variant1, quantity=1)
        self.item2 = add_item(self.order, self.variant2, quantity=1)

    def test_order_detail_renders_snapshots_and_address(self):
        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Address snapshot
        self.assertContains(response, "Adnan Shakkeer")
        self.assertContains(response, "100 Fragrance Avenue")
        self.assertContains(response, "Bengaluru")

        # Item snapshots
        self.assertContains(response, "Dior Sauvage Extrait")
        self.assertContains(response, "SKU-D1")
        self.assertContains(response, "SKU-D2")

    def test_order_detail_displays_item_level_cancellation(self):
        # Cancel item 1
        cancel_order_item(self.customer, self.item1.pk, reason="Changed mind")
        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        self.assertContains(response, "Cancelled")
        self.assertContains(response, "Changed mind")

    def test_order_detail_displays_item_level_returns(self):
        # Simulate delivery first
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.payment_status = Order.PaymentStatus.PAID
        self.order.save()

        # Request return for item 2
        req = request_return(self.customer, self.item2.pk, reason="Damaged bottle nozzle")
        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        self.assertContains(response, "Return Requested")
        self.assertContains(response, "Damaged bottle nozzle")

        # Now approve return
        ret_item = self.item2.return_item
        approve_return(ret_item.pk, admin=self.admin, note="Defect verified")
        response_appr = self.client.get(url)
        self.assertContains(response_appr, "Return Approved")
        self.assertContains(response_appr, "Defect verified")

    def test_order_detail_financial_summary_card(self):
        cancel_order_item(self.customer, self.item1.pk, reason="Unneeded")
        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        # Original: 4000.00, Cancelled: 2500.00, Adjusted: 1500.00
        self.assertContains(response, "4000.00")
        self.assertContains(response, "-₹2500.00")
        self.assertContains(response, "1500.00")


class AdminOrderLifecycleTransitionsTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.customer = make_user()
        self.client.force_login(self.admin)

        _, _, self.product, self.variant = make_catalogue(sku="SKU-T1", price="3500.00")
        self.order = make_order(self.customer, order_number="AURA-20261104-TRANS01", total="3500.00", status=Order.OrderStatus.PENDING)
        add_item(self.order, self.variant, quantity=1)

    def test_transition_pending_to_shipped(self):
        url = reverse("admin_orders:update_status", kwargs={"order_number": self.order.order_number})
        response = self.client.post(url, {"new_status": Order.OrderStatus.SHIPPED, "note": "Dispatched via Bluedart"})
        self.assertRedirects(response, reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number}))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.SHIPPED)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.UNPAID)

        # Check event
        event = self.order.status_events.last()
        self.assertEqual(event.status, Order.OrderStatus.SHIPPED)
        self.assertEqual(event.changed_by, self.admin)
        self.assertEqual(event.note, "Dispatched via Bluedart")

    def test_transition_shipped_to_out_for_delivery(self):
        transition_order_status(self.order.id, Order.OrderStatus.SHIPPED, actor=self.admin)

        url = reverse("admin_orders:update_status", kwargs={"order_number": self.order.order_number})
        response = self.client.post(url, {"new_status": Order.OrderStatus.OUT_FOR_DELIVERY, "note": "Van #4"})
        self.assertEqual(response.status_code, 302)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.OUT_FOR_DELIVERY)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.UNPAID)

    def test_transition_out_for_delivery_to_delivered_triggers_cod_payment(self):
        transition_order_status(self.order.id, Order.OrderStatus.SHIPPED, actor=self.admin)
        transition_order_status(self.order.id, Order.OrderStatus.OUT_FOR_DELIVERY, actor=self.admin)

        url = reverse("admin_orders:update_status", kwargs={"order_number": self.order.order_number})
        response = self.client.post(url, {"new_status": Order.OrderStatus.DELIVERED, "note": "Delivered to customer"})
        self.assertEqual(response.status_code, 302)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.DELIVERED)
        # Payment automatically collected
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(self.order.payment_collected_amount, Decimal("3500.00"))
        self.assertIsNotNone(self.order.payment_collected_at)
        self.assertEqual(self.order.payment_collected_by, self.admin)

    def test_invalid_skipped_transition_rejected(self):
        # Trying to go PENDING -> DELIVERED directly
        url = reverse("admin_orders:update_status", kwargs={"order_number": self.order.order_number})
        response = self.client.post(url, {"new_status": Order.OrderStatus.DELIVERED})
        self.assertEqual(response.status_code, 302)

        self.order.refresh_from_db()
        # Must still be PENDING
        self.assertEqual(self.order.status, Order.OrderStatus.PENDING)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.UNPAID)

    def test_cannot_transition_cancelled_order(self):
        self.order.status = Order.OrderStatus.CANCELLED
        self.order.save()

        url = reverse("admin_orders:update_status", kwargs={"order_number": self.order.order_number})
        response = self.client.post(url, {"new_status": Order.OrderStatus.SHIPPED})
        self.assertEqual(response.status_code, 302)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.CANCELLED)

    def test_update_status_requires_post(self):
        url = reverse("admin_orders:update_status", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_update_status_enforces_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        url = reverse("admin_orders:update_status", kwargs={"order_number": self.order.order_number})
        # Attempt POST without CSRF token
        response = csrf_client.post(url, {"new_status": Order.OrderStatus.SHIPPED})
        self.assertEqual(response.status_code, 403)


class AdminOrderTimelineTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.customer = make_user()
        self.client.force_login(self.admin)

        _, _, self.product, self.variant = make_catalogue(sku="SKU-TIME-1", price="2000.00")
        self.order = make_order(self.customer, order_number="AURA-20261104-TIME01", total="2000.00", status=Order.OrderStatus.PENDING)
        add_item(self.order, self.variant)

    def test_timeline_pending_unpaid_cod(self):
        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        timeline = response.context["timeline"]

        self.assertEqual(timeline[0]["key"], "placed")
        self.assertEqual(timeline[0]["status"], "completed")

        self.assertEqual(timeline[1]["key"], "payment")
        self.assertEqual(timeline[1]["status"], "pending")
        self.assertIn("Awaiting COD collection", timeline[1]["subtitle"])

        self.assertEqual(timeline[2]["key"], "processing")
        self.assertEqual(timeline[2]["status"], "active")

        self.assertEqual(timeline[3]["key"], "shipped")
        self.assertEqual(timeline[3]["status"], "pending")

        self.assertEqual(timeline[4]["key"], "out_for_delivery")
        self.assertEqual(timeline[4]["status"], "pending")

        self.assertEqual(timeline[5]["key"], "delivered")
        self.assertEqual(timeline[5]["status"], "pending")

    def test_timeline_shipped_unpaid_cod(self):
        transition_order_status(self.order.id, Order.OrderStatus.SHIPPED, actor=self.admin)
        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        timeline = response.context["timeline"]

        self.assertEqual(timeline[0]["status"], "completed")  # Placed
        self.assertEqual(timeline[1]["status"], "pending")    # Payment awaiting COD
        self.assertEqual(timeline[2]["status"], "completed")  # Processing
        self.assertEqual(timeline[3]["status"], "completed")  # Shipped
        self.assertEqual(timeline[4]["status"], "pending")    # Out for delivery
        self.assertEqual(timeline[5]["status"], "pending")    # Delivered

    def test_timeline_out_for_delivery_unpaid_cod(self):
        transition_order_status(self.order.id, Order.OrderStatus.SHIPPED, actor=self.admin)
        transition_order_status(self.order.id, Order.OrderStatus.OUT_FOR_DELIVERY, actor=self.admin)
        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        timeline = response.context["timeline"]

        self.assertEqual(timeline[0]["status"], "completed")  # Placed
        self.assertEqual(timeline[1]["status"], "pending")    # Payment awaiting COD
        self.assertIn("Awaiting COD collection", timeline[1]["subtitle"])
        self.assertEqual(timeline[2]["status"], "completed")  # Processing
        self.assertEqual(timeline[3]["status"], "completed")  # Shipped
        self.assertEqual(timeline[4]["status"], "completed")  # Out for delivery
        self.assertIn("In transit with courier", timeline[4]["subtitle"])
        self.assertEqual(timeline[5]["status"], "pending")    # Delivered

    def test_timeline_delivered_paid_cod(self):
        transition_order_status(self.order.id, Order.OrderStatus.SHIPPED, actor=self.admin)
        transition_order_status(self.order.id, Order.OrderStatus.OUT_FOR_DELIVERY, actor=self.admin)
        transition_order_status(self.order.id, Order.OrderStatus.DELIVERED, actor=self.admin)

        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        timeline = response.context["timeline"]

        self.assertEqual(timeline[0]["status"], "completed")  # Placed
        self.assertEqual(timeline[1]["status"], "completed")  # Payment paid
        self.assertIn("Cash on Delivery collected", timeline[1]["subtitle"])
        self.assertEqual(timeline[2]["status"], "completed")  # Processing
        self.assertEqual(timeline[3]["status"], "completed")  # Shipped
        self.assertEqual(timeline[4]["status"], "completed")  # Out for delivery
        self.assertEqual(timeline[5]["status"], "completed")  # Delivered

    def test_timeline_cancelled_order(self):
        self.order.status = Order.OrderStatus.CANCELLED
        self.order.save()

        url = reverse("admin_orders:detail", kwargs={"order_number": self.order.order_number})
        response = self.client.get(url)
        timeline = response.context["timeline"]

        self.assertEqual(timeline[0]["status"], "completed")
        self.assertEqual(timeline[1]["status"], "cancelled")
        self.assertEqual(timeline[2]["status"], "cancelled")
        self.assertEqual(timeline[3]["status"], "cancelled")
        self.assertEqual(timeline[4]["status"], "cancelled")
        self.assertEqual(timeline[5]["status"], "cancelled")

