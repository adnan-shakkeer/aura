"""Tests for Phase 6.1 order-management models and domain services."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant
from user_panel.address.models import Address

from .cancellation import cancel_order_item
from .exceptions import (
    AdminPermissionError,
    InvalidOrderStatusTransitionError,
    ItemAlreadyCancelledError,
    ItemNotCancellableError,
    OrderPermissionError,
    ReturnAlreadyDecidedError,
    ReturnAlreadyRequestedError,
    ReturnDecisionNoteRequiredError,
    ReturnNotAllowedError,
    ReturnReasonRequiredError,
)
from .financials import get_order_financials
from .models import (
    Order,
    OrderItem,
    OrderStatusEvent,
    ReturnItem,
    ReturnRequest,
)
from .order_lifecycle import transition_order_status
from .returns import approve_return, reject_return, request_return

User = get_user_model()


def make_user(username="alice", **overrides):
    values = {
        "username": username,
        "email": f"{username}@example.com",
        "password": "pass1234",
    }
    values.update(overrides)
    return User.objects.create_user(**values)


def make_admin(username="admin"):
    return make_user(username, is_staff=True)


def make_catalogue(*, sku="SKU-1", price="1200.00", stock=10, name="Noir"):
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


def make_variant(product, *, sku, price="1200.00", stock=10, size="50ml"):
    return ProductVariant.objects.create(
        product=product,
        size=size,
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )


def make_address(user):
    return Address.objects.create(
        user=user,
        full_name="Alice Doe",
        phone_number="9876543210",
        address_line1="12 Test Street",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        address_type="HOME",
    )


def make_order(user, *, status=Order.OrderStatus.PENDING):
    return Order.objects.create(
        user=user,
        status=status,
        subtotal=Decimal("0.00"),
        total=Decimal("0.00"),
        delivery_full_name="Alice Doe",
        delivery_phone_number="9876543210",
        delivery_address_line1="12 Test Street",
        delivery_city="Mumbai",
        delivery_state="Maharashtra",
        delivery_pincode="400001",
    )


def add_item(order, variant, *, quantity=2):
    line_total = variant.price * quantity
    item = OrderItem.objects.create(
        order=order,
        variant=variant,
        product_name=variant.product.name,
        brand_name=variant.product.brand.name,
        variant_size=variant.size,
        sku=variant.sku,
        unit_price=variant.price,
        quantity=quantity,
        line_total=line_total,
    )
    order.subtotal += line_total
    order.total += line_total
    order.save(update_fields=["subtotal", "total", "updated_at"])
    return item


def mark_delivered_and_paid(order):
    order.status = Order.OrderStatus.DELIVERED
    order.payment_status = Order.PaymentStatus.PAID
    order.payment_collected_at = timezone.now()
    order.payment_collected_amount = order.total
    order.save(
        update_fields=[
            "status",
            "payment_status",
            "payment_collected_at",
            "payment_collected_amount",
            "updated_at",
        ]
    )
    return order


class OrderStatusTransitionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.admin = make_admin()
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(self.user)
        self.item = add_item(self.order, self.variant)

    def test_confirmed_status_is_retired(self):
        self.assertNotIn("CONFIRMED", Order.OrderStatus.values)
        self.assertIn("OUT_FOR_DELIVERY", Order.OrderStatus.values)

    def test_full_admin_lifecycle_records_events_and_collects_cod(self):
        transition_order_status(
            self.order.pk,
            Order.OrderStatus.SHIPPED,
            actor=self.admin,
        )
        transition_order_status(
            self.order.pk,
            Order.OrderStatus.OUT_FOR_DELIVERY,
            actor=self.admin,
        )
        transition_order_status(
            self.order.pk,
            Order.OrderStatus.DELIVERED,
            actor=self.admin,
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.DELIVERED)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(
            self.order.payment_collected_amount,
            Decimal("2400.00"),
        )
        self.assertIsNotNone(self.order.payment_collected_at)
        self.assertEqual(self.order.payment_collected_by_id, self.admin.pk)

        events = list(self.order.status_events.all())
        self.assertEqual(
            [event.status for event in events],
            [
                Order.OrderStatus.SHIPPED,
                Order.OrderStatus.OUT_FOR_DELIVERY,
                Order.OrderStatus.DELIVERED,
            ],
        )
        self.assertEqual(
            [event.previous_status for event in events],
            [
                Order.OrderStatus.PENDING,
                Order.OrderStatus.SHIPPED,
                Order.OrderStatus.OUT_FOR_DELIVERY,
            ],
        )
        self.assertTrue(all(event.changed_by_id == self.admin.pk for event in events))

    def test_delivery_collects_adjusted_total_after_partial_cancellation(self):
        second_variant = make_variant(
            self.product,
            sku="SKU-2",
            price="800.00",
            stock=10,
        )
        add_item(self.order, second_variant, quantity=1)
        cancel_order_item(self.user, self.item.pk)

        for status in (
            Order.OrderStatus.SHIPPED,
            Order.OrderStatus.OUT_FOR_DELIVERY,
            Order.OrderStatus.DELIVERED,
        ):
            transition_order_status(self.order.pk, status, actor=self.admin)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.payment_collected_amount,
            Decimal("800.00"),
        )
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)

    def test_repeating_delivery_does_not_duplicate_payment_or_event(self):
        for status in (
            Order.OrderStatus.SHIPPED,
            Order.OrderStatus.OUT_FOR_DELIVERY,
            Order.OrderStatus.DELIVERED,
        ):
            transition_order_status(self.order.pk, status, actor=self.admin)

        self.order.refresh_from_db()
        collected_at = self.order.payment_collected_at
        event_count = self.order.status_events.count()

        transition_order_status(
            self.order.pk,
            Order.OrderStatus.DELIVERED,
            actor=self.admin,
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_collected_at, collected_at)
        self.assertEqual(self.order.status_events.count(), event_count)

    def test_invalid_transition_is_rejected(self):
        with self.assertRaises(InvalidOrderStatusTransitionError):
            transition_order_status(
                self.order.pk,
                Order.OrderStatus.DELIVERED,
                actor=self.admin,
            )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.PENDING)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(self.order.status_events.count(), 0)

    def test_admin_cannot_set_cancelled(self):
        with self.assertRaises(InvalidOrderStatusTransitionError):
            transition_order_status(
                self.order.pk,
                Order.OrderStatus.CANCELLED,
                actor=self.admin,
            )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.PENDING)

    def test_customer_cannot_change_status(self):
        with self.assertRaises(AdminPermissionError):
            transition_order_status(
                self.order.pk,
                Order.OrderStatus.SHIPPED,
                actor=self.user,
            )


class CancellationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.other = make_user("bob")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            stock=10
        )
        self.variant_two = make_variant(
            self.product,
            sku="SKU-2",
            price="800.00",
            stock=10,
        )
        self.order = make_order(self.user)
        self.item_one = add_item(self.order, self.variant, quantity=2)
        self.item_two = add_item(self.order, self.variant_two, quantity=1)

    def test_cancelling_one_item_keeps_order_pending_and_restores_stock(self):
        cancel_order_item(
            self.user,
            self.item_one.pk,
            reason="Changed my mind",
        )

        self.item_one.refresh_from_db()
        self.variant.refresh_from_db()
        self.order.refresh_from_db()

        self.assertIsNotNone(self.item_one.cancelled_at)
        self.assertEqual(
            self.item_one.cancellation_reason,
            "Changed my mind",
        )
        self.assertEqual(self.variant.stock, 12)
        self.assertEqual(self.order.status, Order.OrderStatus.PENDING)
        self.assertEqual(self.order.status_events.count(), 0)

    def test_cancelling_last_active_item_cancels_order_and_records_event(self):
        cancel_order_item(self.user, self.item_one.pk)
        cancel_order_item(self.user, self.item_two.pk)

        self.order.refresh_from_db()
        self.variant.refresh_from_db()
        self.variant_two.refresh_from_db()

        self.assertEqual(self.order.status, Order.OrderStatus.CANCELLED)
        self.assertEqual(self.variant.stock, 12)
        self.assertEqual(self.variant_two.stock, 11)

        event = self.order.status_events.get()
        self.assertEqual(event.status, Order.OrderStatus.CANCELLED)
        self.assertEqual(event.previous_status, Order.OrderStatus.PENDING)
        self.assertEqual(event.changed_by_id, self.user.pk)

    def test_cancellation_reason_is_optional(self):
        cancel_order_item(self.user, self.item_one.pk)

        self.item_one.refresh_from_db()
        self.assertEqual(self.item_one.cancellation_reason, "")

    def test_quantity_is_fully_restored(self):
        cancel_order_item(self.user, self.item_one.pk)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 12)

    def test_non_owner_cannot_cancel(self):
        with self.assertRaises(OrderPermissionError):
            cancel_order_item(self.other, self.item_one.pk)

        self.item_one.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertIsNone(self.item_one.cancelled_at)
        self.assertEqual(self.variant.stock, 10)

    def test_cannot_cancel_once_fulfilment_has_started(self):
        self.order.status = Order.OrderStatus.SHIPPED
        self.order.save(update_fields=["status", "updated_at"])

        with self.assertRaises(ItemNotCancellableError):
            cancel_order_item(self.user, self.item_one.pk)

    def test_double_cancellation_does_not_restore_stock_twice(self):
        cancel_order_item(self.user, self.item_one.pk)

        with self.assertRaises(ItemAlreadyCancelledError):
            cancel_order_item(self.user, self.item_one.pk)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 12)

    def test_soft_deleted_variant_still_receives_stock(self):
        self.variant.is_deleted = True
        self.variant.is_listed = False
        self.variant.save(update_fields=["is_deleted", "is_listed"])

        cancel_order_item(self.user, self.item_one.pk)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 12)


class ReturnTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.other = make_user("bob")
        self.admin = make_admin()
        self.category, self.brand, self.product, self.variant = make_catalogue(
            stock=10
        )
        self.order = make_order(self.user)
        self.item = add_item(self.order, self.variant, quantity=2)

    def make_delivered(self):
        mark_delivered_and_paid(self.order)
        return self.order

    def test_return_requires_delivered_order(self):
        with self.assertRaises(ReturnNotAllowedError):
            request_return(self.user, self.item.pk, reason="Damaged")

    def test_return_reason_is_mandatory(self):
        self.make_delivered()

        with self.assertRaises(ReturnReasonRequiredError):
            request_return(self.user, self.item.pk, reason="   ")

        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_non_owner_cannot_request_return(self):
        self.make_delivered()

        with self.assertRaises(OrderPermissionError):
            request_return(self.other, self.item.pk, reason="Damaged")

    def test_request_records_full_quantity_without_restocking(self):
        self.make_delivered()

        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Scent was not as expected",
        )

        self.variant.refresh_from_db()
        return_item = return_request.items.get()

        self.assertTrue(return_request.request_number.startswith("RET-"))
        self.assertEqual(return_request.requested_by_id, self.user.pk)
        self.assertEqual(return_item.status, ReturnItem.Status.REQUESTED)
        self.assertEqual(
            return_item.reason,
            "Scent was not as expected",
        )
        self.assertEqual(return_item.order_item_id, self.item.pk)
        self.assertEqual(return_item.quantity, self.item.quantity)
        self.assertIsNotNone(return_item.requested_at)
        self.assertIsNone(return_item.decided_at)
        self.assertIsNone(return_item.stock_restored_at)
        self.assertEqual(self.variant.stock, 10)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.DELIVERED)

    def test_second_request_is_blocked(self):
        self.make_delivered()
        request_return(self.user, self.item.pk, reason="Damaged")

        with self.assertRaises(ReturnAlreadyRequestedError):
            request_return(self.user, self.item.pk, reason="Changed mind")

        self.assertEqual(ReturnRequest.objects.count(), 1)

    def test_rejection_requires_a_decision_note(self):
        self.make_delivered()
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        with self.assertRaises(ReturnDecisionNoteRequiredError):
            reject_return(return_item.pk, admin=self.admin, note="  ")

        return_item.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REQUESTED)
        self.assertIsNone(return_item.decided_at)
        self.assertEqual(return_item.decision_note, "")

    def test_rejection_is_final_and_permanently_blocks_re_request(self):
        self.make_delivered()
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        reject_return(
            return_item.pk,
            admin=self.admin,
            note="Outside the accepted condition policy",
        )

        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REJECTED)
        self.assertEqual(
            return_item.decision_note,
            "Outside the accepted condition policy",
        )
        self.assertEqual(return_item.decided_by_id, self.admin.pk)
        self.assertIsNotNone(return_item.decided_at)
        self.assertIsNone(return_item.stock_restored_at)
        self.assertEqual(self.variant.stock, 10)

        with self.assertRaises(ReturnAlreadyRequestedError):
            request_return(self.user, self.item.pk, reason="Trying again")

        with self.assertRaises(ReturnAlreadyDecidedError):
            approve_return(return_item.pk, admin=self.admin)

    def test_approval_restores_stock_once_without_changing_order_status(self):
        self.make_delivered()
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        approve_return(return_item.pk, admin=self.admin, note="Approved")

        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.order.refresh_from_db()

        self.assertEqual(return_item.status, ReturnItem.Status.APPROVED)
        self.assertEqual(return_item.decision_note, "Approved")
        self.assertEqual(return_item.decided_by_id, self.admin.pk)
        self.assertIsNotNone(return_item.decided_at)
        self.assertIsNotNone(return_item.stock_restored_at)
        self.assertEqual(self.variant.stock, 12)
        self.assertEqual(self.order.status, Order.OrderStatus.DELIVERED)
        self.assertEqual(self.order.status_events.count(), 0)

        with self.assertRaises(ReturnAlreadyDecidedError):
            approve_return(return_item.pk, admin=self.admin)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 12)

    def test_only_admin_can_decide_a_return(self):
        self.make_delivered()
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        with self.assertRaises(AdminPermissionError):
            approve_return(return_item.pk, admin=self.user)

        with self.assertRaises(AdminPermissionError):
            reject_return(return_item.pk, admin=self.user, note="No")

    def test_approval_restores_stock_for_soft_deleted_variant(self):
        self.make_delivered()
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        self.variant.is_deleted = True
        self.variant.is_listed = False
        self.variant.save(update_fields=["is_deleted", "is_listed"])

        approve_return(return_item.pk, admin=self.admin)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 12)

    def test_cancelled_item_cannot_be_returned(self):
        category, brand, product, second_variant = make_catalogue(
            sku="SKU-SECOND",
            price="500.00",
            stock=5,
        )
        second_item = add_item(self.order, second_variant, quantity=1)
        self.order.status = Order.OrderStatus.PENDING
        self.order.save(update_fields=["status", "updated_at"])
        cancel_order_item(self.user, self.item.pk)

        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status", "updated_at"])

        with self.assertRaises(ReturnNotAllowedError):
            request_return(self.user, self.item.pk, reason="Damaged")

        self.assertIsNotNone(second_item.pk)


class FinancialsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.admin = make_admin()
        self.category, self.brand, self.product, self.variant = make_catalogue(
            price="1200.00",
            stock=10,
        )
        self.variant_two = make_variant(
            self.product,
            sku="SKU-2",
            price="800.00",
            stock=10,
        )
        self.order = make_order(self.user)
        self.item_one = add_item(self.order, self.variant, quantity=2)
        self.item_two = add_item(self.order, self.variant_two, quantity=1)

    def test_no_adjustments_returns_original_total(self):
        financials = get_order_financials(self.order)

        self.assertEqual(financials.original_total, Decimal("3200.00"))
        self.assertEqual(financials.cancelled_total, Decimal("0.00"))
        self.assertEqual(financials.approved_return_total, Decimal("0.00"))
        self.assertEqual(financials.adjusted_total, Decimal("3200.00"))
        self.assertEqual(financials.total_due, Decimal("3200.00"))
        self.assertEqual(financials.credit_due, Decimal("0.00"))

    def test_cancelled_item_reduces_adjusted_total_and_amount_due(self):
        cancel_order_item(self.user, self.item_one.pk)

        financials = get_order_financials(self.order)

        self.assertEqual(financials.cancelled_total, Decimal("2400.00"))
        self.assertEqual(financials.adjusted_total, Decimal("800.00"))
        self.assertEqual(financials.total_due, Decimal("800.00"))

    def test_requested_and_rejected_returns_do_not_reduce_payable_value(self):
        mark_delivered_and_paid(self.order)
        return_request = request_return(
            self.user,
            self.item_one.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        request_financials = get_order_financials(self.order)
        self.assertEqual(
            request_financials.approved_return_total,
            Decimal("0.00"),
        )
        self.assertEqual(request_financials.credit_due, Decimal("0.00"))

        reject_return(return_item.pk, admin=self.admin, note="Not eligible")
        return_item.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REJECTED)

        reject_financials = get_order_financials(self.order)
        self.assertEqual(
            reject_financials.approved_return_total,
            Decimal("0.00"),
        )
        self.assertEqual(reject_financials.credit_due, Decimal("0.00"))

    def test_approved_return_on_paid_order_creates_informational_credit(self):
        mark_delivered_and_paid(self.order)
        return_request = request_return(
            self.user,
            self.item_one.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        approve_return(return_item.pk, admin=self.admin)

        financials = get_order_financials(self.order)

        self.assertEqual(
            financials.approved_return_total,
            Decimal("2400.00"),
        )
        self.assertEqual(financials.adjusted_total, Decimal("800.00"))
        self.assertEqual(financials.amount_collected, Decimal("3200.00"))
        self.assertEqual(financials.total_due, Decimal("0.00"))
        self.assertEqual(financials.credit_due, Decimal("2400.00"))


class ReturnConstraintTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(self.user)
        self.item = add_item(self.order, self.variant)

    def test_order_item_can_only_have_one_return_record(self):
        first = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )
        ReturnItem.objects.create(
            return_request=first,
            order_item=self.item,
            quantity=self.item.quantity,
            reason="First",
        )
        second = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ReturnItem.objects.create(
                    return_request=second,
                    order_item=self.item,
                    quantity=self.item.quantity,
                    reason="Second",
                )

    def test_status_event_is_order_level_history(self):
        event = OrderStatusEvent.objects.create(
            order=self.order,
            status=Order.OrderStatus.SHIPPED,
            previous_status=Order.OrderStatus.PENDING,
        )

        self.assertEqual(event.order_id, self.order.pk)
        self.assertEqual(event.status, Order.OrderStatus.SHIPPED)
