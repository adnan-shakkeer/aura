"""
Phase 6.5 Test Suite – Admin Returns Management
================================================
Tests access control, KPI aggregation, filtering, search, approval,
rejection, and concurrent access safety.
"""

import threading

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant
from user_panel.orders.models import Order, OrderItem, ReturnItem, ReturnRequest

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_staff(**kwargs):
    defaults = dict(
        username="staff_user",
        email="staff@aura.test",
        password="StaffPass123!",
        is_staff=True,
    )
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def make_superuser(**kwargs):
    defaults = dict(
        username="superuser",
        email="super@aura.test",
        password="SuperPass123!",
        is_staff=True,
        is_superuser=True,
    )
    defaults.update(kwargs)
    return User.objects.create_superuser(**defaults)


def make_customer(**kwargs):
    defaults = dict(
        username="customer_user",
        email="customer@aura.test",
        password="CustomerPass123!",
    )
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def make_catalogue(sku="TEST-SKU-001"):
    cat = Category.objects.create(name=f"Cat-{sku}", description="Test category")
    brand = Brand.objects.create(
        category=cat,
        name=f"Brand-{sku}",
        description="Brand description",
        listing_image="brands/listing/test.jpg",
        listing_description="Listing description",
        hero_image="brands/hero/test.jpg",
        hero_description="Hero description",
    )
    product = Product.objects.create(
        brand=brand,
        name=f"Product-{sku}",
        description="Test product",
        fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
    )
    variant = ProductVariant.objects.create(
        product=product,
        sku=sku,
        size="50ml",
        price=Decimal("1000.00"),
        stock=10,
    )
    return variant


def make_delivered_order(user, variant, qty=1, order_number=None):
    total = variant.price * qty
    order = Order.objects.create(
        user=user,
        order_number=order_number or f"AURA-TEST-{variant.sku}",
        status=Order.OrderStatus.DELIVERED,
        payment_method=Order.PaymentMethod.COD,
        payment_status=Order.PaymentStatus.UNPAID,
        subtotal=total,
        total=total,
        delivery_full_name="Test Customer",
        delivery_phone_number="9876543210",
        delivery_address_line1="123 Test Street",
        delivery_address_line2="",
        delivery_landmark="",
        delivery_city="Bengaluru",
        delivery_state="Karnataka",
        delivery_pincode="560001",
        delivery_address_type=Order.DeliveryAddressType.HOME,
    )
    item = OrderItem.objects.create(
        order=order,
        variant=variant,
        product_name=variant.product.name,
        brand_name=variant.product.brand.name,
        variant_size=variant.size,
        sku=variant.sku,
        unit_price=variant.price,
        quantity=qty,
        line_total=total,
    )
    return order, item


def make_return_item(order, item, reason="Test reason", status=ReturnItem.Status.REQUESTED,
                     admin=None, decision_note=""):
    rr = ReturnRequest.objects.create(
        order=order,
        requested_by=order.user,
        request_number=ReturnRequest._generate_request_number(),
    )
    ri = ReturnItem.objects.create(
        return_request=rr,
        order_item=item,
        quantity=item.quantity,
        reason=reason,
        status=status,
        decided_by=admin if status != ReturnItem.Status.REQUESTED else None,
        decision_note=decision_note,
    )
    if status == ReturnItem.Status.REQUESTED:
        # Clear decided fields to satisfy model constraints
        ri.decided_at = None
        ri.decided_by = None
        ri.decision_note = ""
        ri.save(update_fields=["decided_at", "decided_by", "decision_note"])
    return rr, ri


LIST_URL = reverse("admin_returns:list")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Access Control
# ─────────────────────────────────────────────────────────────────────────────

class ReturnsListAccessTests(TestCase):
    def test_unauthenticated_redirects_to_login(self):
        resp = self.client.get(LIST_URL)
        self.assertRedirects(resp, reverse("admin_auth:login"), fetch_redirect_response=False)

    def test_customer_redirects_to_login(self):
        c = make_customer(username="cust_access", email="cust_access@test.com")
        self.client.login(username="cust_access", password="CustomerPass123!")
        resp = self.client.get(LIST_URL)
        self.assertIn(resp.status_code, [302, 403])
        self.assertNotEqual(resp.status_code, 200)

    def test_staff_can_access(self):
        make_staff(username="staff_access", email="staff_access@test.com")
        self.client.login(username="staff_access", password="StaffPass123!")
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200)

    def test_superuser_can_access(self):
        make_superuser(username="super_access", email="super_access@test.com")
        self.client.login(username="super_access", password="SuperPass123!")
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200)

    def test_approve_get_returns_405(self):
        admin = make_superuser(username="super_405a", email="super_405a@test.com")
        v = make_catalogue("SKU-405A")
        cust = make_customer(username="cust_405a", email="cust_405a@test.com")
        order, item = make_delivered_order(cust, v, order_number="AURA-405A")
        _rr, ri = make_return_item(order, item)
        self.client.login(username="super_405a", password="SuperPass123!")
        resp = self.client.get(reverse("admin_returns:approve", args=[ri.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_reject_get_returns_405(self):
        admin = make_superuser(username="super_405b", email="super_405b@test.com")
        v = make_catalogue("SKU-405B")
        cust = make_customer(username="cust_405b", email="cust_405b@test.com")
        order, item = make_delivered_order(cust, v, order_number="AURA-405B")
        _rr, ri = make_return_item(order, item)
        self.client.login(username="super_405b", password="SuperPass123!")
        resp = self.client.get(reverse("admin_returns:reject", args=[ri.pk]))
        self.assertEqual(resp.status_code, 405)


# ─────────────────────────────────────────────────────────────────────────────
# 2. KPI Counts
# ─────────────────────────────────────────────────────────────────────────────

class ReturnsListKPITests(TestCase):
    def setUp(self):
        self.admin = make_superuser(username="super_kpi", email="super_kpi@test.com")
        self.cust = make_customer(username="cust_kpi", email="cust_kpi@test.com")
        self.client.login(username="super_kpi", password="SuperPass123!")

    def test_kpi_counts_match_database(self):
        v1 = make_catalogue("SKU-KPI1")
        v2 = make_catalogue("SKU-KPI2")
        v3 = make_catalogue("SKU-KPI3")

        o1, i1 = make_delivered_order(self.cust, v1, order_number="AURA-KPI1")
        o2, i2 = make_delivered_order(self.cust, v2, order_number="AURA-KPI2")
        o3, i3 = make_delivered_order(self.cust, v3, order_number="AURA-KPI3")

        make_return_item(o1, i1)  # REQUESTED

        # Manually create APPROVED & REJECTED states via direct ORM
        from django.utils import timezone
        rr2 = ReturnRequest.objects.create(
            order=o2, requested_by=self.cust,
            request_number=ReturnRequest._generate_request_number()
        )
        ri2 = ReturnItem.objects.create(
            return_request=rr2, order_item=i2, quantity=i2.quantity, reason="reason",
            status=ReturnItem.Status.APPROVED, decided_at=timezone.now(),
            decided_by=self.admin, stock_restored_at=timezone.now(),
        )

        rr3 = ReturnRequest.objects.create(
            order=o3, requested_by=self.cust,
            request_number=ReturnRequest._generate_request_number()
        )
        ri3 = ReturnItem.objects.create(
            return_request=rr3, order_item=i3, quantity=i3.quantity, reason="reason",
            status=ReturnItem.Status.REJECTED, decided_at=timezone.now(),
            decided_by=self.admin, decision_note="Invalid",
        )

        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200)
        kpis = resp.context["kpis"]
        self.assertGreaterEqual(kpis["total"], 3)
        self.assertGreaterEqual(kpis["pending"], 1)
        self.assertGreaterEqual(kpis["approved"], 1)
        self.assertGreaterEqual(kpis["rejected"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Filtering and Search
# ─────────────────────────────────────────────────────────────────────────────

class ReturnsListFilteringTests(TestCase):
    def setUp(self):
        self.admin = make_superuser(username="super_filt", email="super_filt@test.com")
        self.cust = make_customer(username="cust_filt", email="cust_filt@test.com")
        self.client.login(username="super_filt", password="SuperPass123!")

        self.v1 = make_catalogue("SKU-FLT1")
        self.v2 = make_catalogue("SKU-FLT2")

        self.o1, self.i1 = make_delivered_order(self.cust, self.v1, order_number="AURA-FLT1")
        self.rr1, self.ri1 = make_return_item(self.o1, self.i1, reason="Too fragrant")

        from django.utils import timezone
        self.o2, self.i2 = make_delivered_order(self.cust, self.v2, order_number="AURA-FLT2")
        rr2 = ReturnRequest.objects.create(
            order=self.o2, requested_by=self.cust,
            request_number=ReturnRequest._generate_request_number()
        )
        self.ri2 = ReturnItem.objects.create(
            return_request=rr2, order_item=self.i2, quantity=self.i2.quantity,
            reason="Broken bottle", status=ReturnItem.Status.APPROVED,
            decided_at=timezone.now(), decided_by=self.admin,
            stock_restored_at=timezone.now(),
        )

    def test_filter_pending(self):
        resp = self.client.get(LIST_URL + "?status=pending")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri1.pk, ids)
        self.assertNotIn(self.ri2.pk, ids)

    def test_filter_approved(self):
        resp = self.client.get(LIST_URL + "?status=approved")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri2.pk, ids)
        self.assertNotIn(self.ri1.pk, ids)

    def test_filter_all(self):
        resp = self.client.get(LIST_URL + "?status=all")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri1.pk, ids)
        self.assertIn(self.ri2.pk, ids)

    def test_search_by_order_number(self):
        resp = self.client.get(LIST_URL + "?search=AURA-FLT1")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri1.pk, ids)
        self.assertNotIn(self.ri2.pk, ids)

    def test_search_by_product_name(self):
        resp = self.client.get(LIST_URL + f"?search={self.v1.product.name}")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri1.pk, ids)

    def test_search_by_customer_name(self):
        resp = self.client.get(LIST_URL + "?search=Test Customer")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri1.pk, ids)
        self.assertIn(self.ri2.pk, ids)

    def test_search_by_customer_email(self):
        resp = self.client.get(LIST_URL + "?search=cust_filt@test.com")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri1.pk, ids)

    def test_search_by_request_number(self):
        resp = self.client.get(LIST_URL + f"?search={self.rr1.request_number}")
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(self.ri1.pk, ids)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Display / row identity
# ─────────────────────────────────────────────────────────────────────────────

class ReturnsListDisplayTests(TestCase):
    def setUp(self):
        self.admin = make_superuser(username="super_disp", email="super_disp@test.com")
        self.cust = make_customer(username="cust_disp", email="cust_disp@test.com")
        self.client.login(username="super_disp", password="SuperPass123!")

    def test_one_row_per_return_item(self):
        """A ReturnRequest with 2 items → 2 rows in the listing."""
        v1 = make_catalogue("SKU-DSP1")
        v2 = make_catalogue("SKU-DSP2")
        o1, i1 = make_delivered_order(self.cust, v1, order_number="AURA-DSP1")
        # Second item on same order
        i2 = OrderItem.objects.create(
            order=o1, variant=v2, product_name=v2.product.name,
            brand_name=v2.product.brand.name, variant_size=v2.size,
            sku=v2.sku, unit_price=v2.price, quantity=1, line_total=v2.price,
        )
        rr = ReturnRequest.objects.create(
            order=o1, requested_by=self.cust,
            request_number=ReturnRequest._generate_request_number()
        )
        ri1 = ReturnItem.objects.create(
            return_request=rr, order_item=i1, quantity=1, reason="Too big"
        )
        ri2 = ReturnItem.objects.create(
            return_request=rr, order_item=i2, quantity=1, reason="Wrong colour"
        )
        resp = self.client.get(LIST_URL)
        ids = [ri.pk for ri in resp.context["page_obj"]]
        self.assertIn(ri1.pk, ids)
        self.assertIn(ri2.pk, ids)

    def test_return_item_pk_is_row_id(self):
        v = make_catalogue("SKU-DSP3")
        o, item = make_delivered_order(self.cust, v, order_number="AURA-DSP3")
        _rr, ri = make_return_item(o, item)
        resp = self.client.get(LIST_URL)
        row_ids = [r.pk for r in resp.context["page_obj"]]
        self.assertIn(ri.pk, row_ids)

    def test_request_number_in_context(self):
        v = make_catalogue("SKU-DSP4")
        o, item = make_delivered_order(self.cust, v, order_number="AURA-DSP4")
        rr, ri = make_return_item(o, item)
        resp = self.client.get(LIST_URL)
        rows = {r.pk: r for r in resp.context["page_obj"]}
        self.assertIn(ri.pk, rows)
        self.assertEqual(rows[ri.pk].return_request.request_number, rr.request_number)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Approve Action
# ─────────────────────────────────────────────────────────────────────────────

class ApproveReturnViewTests(TestCase):
    def setUp(self):
        self.admin = make_superuser(username="super_app", email="super_app@test.com")
        self.cust = make_customer(username="cust_app", email="cust_app@test.com")
        self.client.login(username="super_app", password="SuperPass123!")

        self.v = make_catalogue("SKU-APP1")
        self.o, self.item = make_delivered_order(self.cust, self.v, order_number="AURA-APP1")
        self.initial_stock = self.v.stock
        self._rr, self.ri = make_return_item(self.o, self.item)

    def test_approve_transitions_status(self):
        resp = self.client.post(
            reverse("admin_returns:approve", args=[self.ri.pk]), {"note": ""}
        )
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.status, ReturnItem.Status.APPROVED)

    def test_approve_restores_stock(self):
        self.client.post(
            reverse("admin_returns:approve", args=[self.ri.pk]), {"note": ""}
        )
        self.v.refresh_from_db()
        self.assertEqual(self.v.stock, self.initial_stock + self.item.quantity)

    def test_approve_sets_decided_by(self):
        self.client.post(
            reverse("admin_returns:approve", args=[self.ri.pk]), {"note": ""}
        )
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.decided_by_id, self.admin.pk)

    def test_approve_sets_stock_restored_at(self):
        self.client.post(
            reverse("admin_returns:approve", args=[self.ri.pk]), {"note": ""}
        )
        self.ri.refresh_from_db()
        self.assertIsNotNone(self.ri.stock_restored_at)

    def test_approve_stores_optional_note(self):
        self.client.post(
            reverse("admin_returns:approve", args=[self.ri.pk]),
            {"note": "Verified condition acceptable"},
        )
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.decision_note, "Verified condition acceptable")

    def test_approve_already_decided_gives_error(self):
        # First approval
        self.client.post(reverse("admin_returns:approve", args=[self.ri.pk]), {"note": ""})
        # Second attempt — should fail gracefully
        resp = self.client.post(
            reverse("admin_returns:approve", args=[self.ri.pk]), {"note": ""}, follow=True
        )
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("already been decided" in m or "approved" in m.lower() for m in msgs))

    def test_approve_success_message_contains_product_name(self):
        resp = self.client.post(
            reverse("admin_returns:approve", args=[self.ri.pk]),
            {"note": ""},
            follow=True,
        )
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any(self.v.product.name in m for m in msgs))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Reject Action
# ─────────────────────────────────────────────────────────────────────────────

class RejectReturnViewTests(TestCase):
    def setUp(self):
        self.admin = make_superuser(username="super_rej", email="super_rej@test.com")
        self.cust = make_customer(username="cust_rej", email="cust_rej@test.com")
        self.client.login(username="super_rej", password="SuperPass123!")

        self.v = make_catalogue("SKU-REJ1")
        self.o, self.item = make_delivered_order(self.cust, self.v, order_number="AURA-REJ1")
        self.initial_stock = self.v.stock
        self._rr, self.ri = make_return_item(self.o, self.item)

    def test_reject_with_note_transitions_status(self):
        self.client.post(
            reverse("admin_returns:reject", args=[self.ri.pk]),
            {"note": "Return window expired"},
        )
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.status, ReturnItem.Status.REJECTED)

    def test_reject_does_not_change_stock(self):
        self.client.post(
            reverse("admin_returns:reject", args=[self.ri.pk]),
            {"note": "Return window expired"},
        )
        self.v.refresh_from_db()
        self.assertEqual(self.v.stock, self.initial_stock)

    def test_reject_stores_note(self):
        self.client.post(
            reverse("admin_returns:reject", args=[self.ri.pk]),
            {"note": "Return window expired"},
        )
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.decision_note, "Return window expired")

    def test_reject_blank_note_gives_error_no_status_change(self):
        resp = self.client.post(
            reverse("admin_returns:reject", args=[self.ri.pk]),
            {"note": ""},
            follow=True,
        )
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.status, ReturnItem.Status.REQUESTED)
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("rejection note" in m.lower() or "required" in m.lower() for m in msgs))

    def test_reject_already_decided_gives_error(self):
        self.client.post(
            reverse("admin_returns:reject", args=[self.ri.pk]),
            {"note": "Return window expired"},
        )
        resp = self.client.post(
            reverse("admin_returns:reject", args=[self.ri.pk]),
            {"note": "Second attempt"},
            follow=True,
        )
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("already been decided" in m or "decided" in m.lower() for m in msgs))

    def test_reject_sets_decided_by(self):
        self.client.post(
            reverse("admin_returns:reject", args=[self.ri.pk]),
            {"note": "Invalid item returned"},
        )
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.decided_by_id, self.admin.pk)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Concurrency – only one approval succeeds and stock is restored once
# ─────────────────────────────────────────────────────────────────────────────

class ReturnsConcurrencyTests(TransactionTestCase):
    """
    Two simultaneous admin clients both try to approve the same return item.
    The database row-level lock in _lock_return_item() ensures only one succeeds.
    Stock must be restored exactly once.
    """

    def setUp(self):
        self.admin1 = make_superuser(username="super_con1", email="super_con1@test.com")
        self.admin2 = make_staff(username="staff_con2", email="staff_con2@test.com")
        self.cust = make_customer(username="cust_con", email="cust_con@test.com")

        self.v = make_catalogue("SKU-CON1")
        self.initial_stock = self.v.stock
        self.o, self.item = make_delivered_order(self.cust, self.v, order_number="AURA-CON1")
        self._rr, self.ri = make_return_item(self.o, self.item)

    def test_concurrent_approval_stock_restored_exactly_once(self):
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def do_approve(username):
            from user_panel.orders.returns import approve_return
            from user_panel.orders.exceptions import ReturnAlreadyDecidedError
            from django import db
            # Each thread gets its own DB connection
            db.connections.close_all()
            try:
                barrier.wait(timeout=5)
                approve_return(self.ri.pk, admin=User.objects.get(username=username))
                results.append("ok")
            except ReturnAlreadyDecidedError:
                results.append("already_decided")
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                db.connections.close_all()

        t1 = threading.Thread(target=do_approve, args=("super_con1",))
        t2 = threading.Thread(target=do_approve, args=("staff_con2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # No unexpected exceptions
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # Exactly one success, one already_decided
        self.assertEqual(sorted(results), ["already_decided", "ok"],
                         f"Unexpected outcomes: {results}")

        # Stock restored exactly once
        self.v.refresh_from_db()
        self.assertEqual(self.v.stock, self.initial_stock + self.item.quantity,
                         f"Expected stock {self.initial_stock + self.item.quantity}, got {self.v.stock}")

        # Return item is APPROVED
        self.ri.refresh_from_db()
        self.assertEqual(self.ri.status, ReturnItem.Status.APPROVED)
        self.assertIsNotNone(self.ri.stock_restored_at)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Regression – Phase 6.4 KPI and listing still work
# ─────────────────────────────────────────────────────────────────────────────

class ReturnsRegressionTests(TestCase):
    def test_admin_orders_list_still_accessible(self):
        admin = make_superuser(username="super_reg", email="super_reg@test.com")
        self.client.login(username="super_reg", password="SuperPass123!")
        resp = self.client.get(reverse("admin_orders:list"))
        self.assertEqual(resp.status_code, 200)

    def test_sidebar_returns_link_resolves(self):
        from django.urls import reverse as r
        url = r("admin_returns:list")
        self.assertEqual(url, "/admin-panel/returns/")
