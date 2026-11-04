"""Tests for the Phase 6.1.1 item-level return architecture."""

import re
import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.exceptions import FieldDoesNotExist
from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    transaction,
)
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import (
    TestCase,
    TransactionTestCase,
    skipUnlessDBFeature,
)
from django.utils import timezone

from admin_panel.products.models import ProductVariant

from .cancellation import cancel_order_item
from .exceptions import (
    ItemAlreadyCancelledError,
    ItemNotCancellableError,
    ReturnAlreadyDecidedError,
    ReturnAlreadyRequestedError,
    ReturnIntegrityError,
    ReturnNotAllowedError,
    ReturnQuantityMismatchError,
)
from .financials import get_order_financials
from .models import Order, ReturnItem, ReturnRequest
from .returns import approve_return, reject_return, request_return
from .tests_phase6_1 import (
    add_item,
    make_admin,
    make_catalogue,
    make_order,
    make_user,
    make_variant,
    mark_delivered_and_paid,
)


def make_grouped_return(order, user, entries):
    return_request = ReturnRequest.objects.create(
        order=order,
        requested_by=user,
    )
    items = [
        ReturnItem.objects.create(
            return_request=return_request,
            order_item=order_item,
            quantity=order_item.quantity,
            reason=reason,
        )
        for order_item, reason in entries
    ]
    return return_request, items


class ReturnArchitectureTests(TestCase):
    def setUp(self):
        self.user = make_user("arch")
        self.admin = make_admin("archadmin")
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(self.user)
        self.item = add_item(self.order, self.variant)

    def test_return_request_contains_only_grouping_and_audit_fields(self):
        removed_fields = [
            "reason",
            "status",
            "requested_at",
            "decided_at",
            "decided_by",
            "decision_note",
            "updated_at",
        ]
        for field_name in removed_fields:
            with self.assertRaises(FieldDoesNotExist):
                ReturnRequest._meta.get_field(field_name)

        self.assertIsNotNone(
            ReturnRequest._meta.get_field("request_number")
        )
        self.assertIsNotNone(ReturnRequest._meta.get_field("created_at"))

    def test_return_item_owns_concrete_decision_state(self):
        status_field = ReturnItem._meta.get_field("status")
        self.assertFalse(status_field.is_relation)
        self.assertEqual(
            status_field.default,
            ReturnItem.Status.REQUESTED,
        )

        for field_name in [
            "reason",
            "status",
            "requested_at",
            "decided_at",
            "decided_by",
            "decision_note",
            "stock_restored_at",
        ]:
            self.assertIsNotNone(
                ReturnItem._meta.get_field(field_name)
            )

    def test_request_number_format_and_uniqueness(self):
        first = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )
        second = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )

        pattern = r"RET-\d{8}-[0-9A-F]{8}"
        self.assertRegex(first.request_number, pattern)
        self.assertRegex(second.request_number, pattern)
        self.assertNotEqual(
            first.request_number,
            second.request_number,
        )

    def test_constraints_reject_invalid_decision_states(self):
        return_request = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )

        invalid_values = [
            {"reason": ""},
            {"decided_at": timezone.now()},
            {
                "status": ReturnItem.Status.REJECTED,
                "decided_at": timezone.now(),
                "decision_note": "",
            },
            {
                "status": ReturnItem.Status.APPROVED,
                "decided_at": timezone.now(),
            },
            {"stock_restored_at": timezone.now()},
        ]

        for index, values in enumerate(invalid_values):
            data = {
                "return_request": return_request,
                "order_item": self.item if index == 0 else self._new_item(index),
                "quantity": 1,
                "reason": "Valid reason",
            }
            data.update(values)
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ReturnItem.objects.create(**data)

    def _new_item(self, index):
        variant = make_variant(
            self.product,
            sku=f"SKU-ARCH-{index}",
        )
        return add_item(self.order, variant, quantity=1)


class RequestNumberRetryTests(TestCase):
    def setUp(self):
        self.user = make_user("retry")
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(self.user)
        self.item = add_item(self.order, self.variant)
        mark_delivered_and_paid(self.order)

    def test_collision_retries_with_a_new_number_in_a_savepoint(self):
        collision = "RET-20260101-AAAAAAAA"
        replacement = "RET-20260101-BBBBBBBB"
        ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
            request_number=collision,
        )
        generator = Mock(side_effect=[collision, replacement])

        with patch.object(
            ReturnRequest,
            "_generate_request_number",
            new=staticmethod(generator),
        ):
            return_request = request_return(
                self.user,
                self.item.pk,
                reason="Damaged",
            )

        self.assertEqual(generator.call_count, 2)
        self.assertEqual(return_request.request_number, replacement)
        self.assertEqual(ReturnRequest.objects.count(), 2)
        self.assertEqual(return_request.items.count(), 1)

    def test_non_request_number_integrity_error_is_not_retried(self):
        generator = Mock(return_value="RET-20260101-CCCCCCCC")

        with patch.object(
            ReturnRequest,
            "_generate_request_number",
            new=staticmethod(generator),
        ), patch.object(
            ReturnRequest.objects,
            "create",
            side_effect=IntegrityError("other constraint"),
        ):
            with self.assertRaises(IntegrityError):
                request_return(self.user, self.item.pk, reason="Damaged")

        self.assertEqual(generator.call_count, 1)


class MultiItemReturnTests(TestCase):
    def setUp(self):
        self.user = make_user("multi")
        self.admin = make_admin("multiadmin")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            price="1200.00",
            stock=10,
        )
        self.variant_two = make_variant(
            self.product,
            sku="SKU-MULTI-2",
            price="800.00",
            stock=10,
        )
        self.variant_three = make_variant(
            self.product,
            sku="SKU-MULTI-3",
            price="500.00",
            stock=10,
        )
        self.order = make_order(self.user)
        self.item_one = add_item(self.order, self.variant, quantity=2)
        self.item_two = add_item(self.order, self.variant_two, quantity=1)
        self.item_three = add_item(
            self.order,
            self.variant_three,
            quantity=1,
        )
        mark_delivered_and_paid(self.order)

    def test_sibling_items_receive_independent_decisions(self):
        return_request, return_items = make_grouped_return(
            self.order,
            self.user,
            [
                (self.item_one, "First reason"),
                (self.item_two, "Second reason"),
                (self.item_three, "Third reason"),
            ],
        )

        approve_return(
            return_items[0].pk,
            admin=self.admin,
            note="Approved first",
        )
        reject_return(
            return_items[1].pk,
            admin=self.admin,
            note="Rejected second",
        )

        for return_item in return_items:
            return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.variant_two.refresh_from_db()
        self.variant_three.refresh_from_db()
        self.order.refresh_from_db()

        self.assertEqual(
            return_items[0].status,
            ReturnItem.Status.APPROVED,
        )
        self.assertEqual(return_items[0].decision_note, "Approved first")
        self.assertIsNotNone(return_items[0].stock_restored_at)
        self.assertEqual(
            return_items[1].status,
            ReturnItem.Status.REJECTED,
        )
        self.assertEqual(return_items[1].decision_note, "Rejected second")
        self.assertIsNone(return_items[1].stock_restored_at)
        self.assertEqual(
            return_items[2].status,
            ReturnItem.Status.REQUESTED,
        )
        self.assertIsNone(return_items[2].decided_at)
        self.assertIsNone(return_items[2].stock_restored_at)

        self.assertEqual(self.variant.stock, 12)
        self.assertEqual(self.variant_two.stock, 10)
        self.assertEqual(self.variant_three.stock, 10)
        self.assertEqual(self.order.status, Order.OrderStatus.DELIVERED)
        self.assertEqual(self.order.status_events.count(), 0)
        self.assertEqual(return_request.items.count(), 3)

    def test_financials_count_only_approved_siblings(self):
        _, return_items = make_grouped_return(
            self.order,
            self.user,
            [
                (self.item_one, "First reason"),
                (self.item_two, "Second reason"),
                (self.item_three, "Third reason"),
            ],
        )
        approve_return(return_items[0].pk, admin=self.admin)
        approve_return(return_items[2].pk, admin=self.admin)
        reject_return(
            return_items[1].pk,
            admin=self.admin,
            note="Rejected second",
        )

        financials = get_order_financials(self.order)
        expected_return = self.item_one.line_total + self.item_three.line_total

        self.assertEqual(
            financials.approved_return_total,
            expected_return,
        )
        self.assertEqual(financials.cancelled_total, Decimal("0.00"))
        self.assertEqual(
            financials.adjusted_total,
            self.order.total - expected_return,
        )


class ReturnIntegrityTests(TestCase):
    def setUp(self):
        self.user = make_user("integrity")
        self.admin = make_admin("integrityadmin")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            stock=10,
        )
        self.order = make_order(self.user)
        self.item = add_item(self.order, self.variant, quantity=2)
        mark_delivered_and_paid(self.order)

    def test_quantity_mismatch_blocks_approval(self):
        _, return_items = make_grouped_return(
            self.order,
            self.user,
            [(self.item, "Damaged")],
        )
        ReturnItem.objects.filter(pk=return_items[0].pk).update(quantity=1)

        with self.assertRaises(ReturnQuantityMismatchError):
            approve_return(return_items[0].pk, admin=self.admin)

        return_items[0].refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(
            return_items[0].status,
            ReturnItem.Status.REQUESTED,
        )
        self.assertIsNone(return_items[0].stock_restored_at)
        self.assertEqual(self.variant.stock, 10)

    def test_cross_order_return_is_rejected(self):
        other_order = make_order(self.user)
        other_item = add_item(other_order, self.variant, quantity=1)
        return_request = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )
        return_item = ReturnItem.objects.create(
            return_request=return_request,
            order_item=other_item,
            quantity=other_item.quantity,
            reason="Cross order",
        )

        with self.assertRaises(ReturnIntegrityError):
            approve_return(return_item.pk, admin=self.admin)

        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REQUESTED)
        self.assertEqual(self.variant.stock, 10)

    def test_approval_requires_delivered_order(self):
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()
        self.order.status = Order.OrderStatus.PENDING
        self.order.save(update_fields=["status", "updated_at"])

        with self.assertRaises(ReturnNotAllowedError):
            approve_return(return_item.pk, admin=self.admin)

        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REQUESTED)
        self.assertEqual(self.variant.stock, 10)

    def test_cancelled_item_cannot_be_approved(self):
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()
        self.item.cancelled_at = timezone.now()
        self.item.save(update_fields=["cancelled_at"])

        with self.assertRaises(ReturnNotAllowedError):
            approve_return(return_item.pk, admin=self.admin)

        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REQUESTED)
        self.assertEqual(self.variant.stock, 10)

    def test_variant_failure_rolls_back_approval(self):
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        with patch.object(
            ProductVariant,
            "save",
            side_effect=DatabaseError("variant save failed"),
        ):
            with self.assertRaises(DatabaseError):
                approve_return(return_item.pk, admin=self.admin)

        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REQUESTED)
        self.assertIsNone(return_item.stock_restored_at)
        self.assertEqual(self.variant.stock, 10)

    def test_item_failure_rolls_back_restored_stock(self):
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        with patch.object(
            ReturnItem,
            "save",
            side_effect=DatabaseError("item save failed"),
        ):
            with self.assertRaises(DatabaseError):
                approve_return(return_item.pk, admin=self.admin)

        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.REQUESTED)
        self.assertIsNone(return_item.stock_restored_at)
        self.assertEqual(self.variant.stock, 10)

    def test_approval_preserves_monetary_snapshots(self):
        original_order = (
            self.order.subtotal,
            self.order.total,
        )
        original_item = (
            self.item.unit_price,
            self.item.line_total,
        )
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        approve_return(
            return_request.items.get().pk,
            admin=self.admin,
        )

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(
            (self.order.subtotal, self.order.total),
            original_order,
        )
        self.assertEqual(
            (self.item.unit_price, self.item.line_total),
            original_item,
        )

    def test_cancelled_item_is_not_double_counted_as_returned(self):
        self.order.status = Order.OrderStatus.PENDING
        self.order.save(update_fields=["status", "updated_at"])
        cancel_order_item(self.user, self.item.pk)
        return_request = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )
        now = timezone.now()
        ReturnItem.objects.create(
            return_request=return_request,
            order_item=self.item,
            quantity=self.item.quantity,
            reason="Inconsistent historical state",
            status=ReturnItem.Status.APPROVED,
            decided_at=now,
            decided_by=self.admin,
            stock_restored_at=now,
        )

        financials = get_order_financials(self.order)
        self.assertEqual(
            financials.cancelled_total,
            self.item.line_total,
        )
        self.assertEqual(
            financials.approved_return_total,
            Decimal("0.00"),
        )
        self.assertEqual(
            financials.adjusted_total,
            self.order.total - self.item.line_total,
        )


@skipUnlessDBFeature("has_select_for_update")
class ReturnConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = make_user("concurrent")
        self.admin = make_admin("concurrentadmin")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            stock=10,
        )
        self.order = make_order(self.user)
        self.item = add_item(self.order, self.variant, quantity=2)
        mark_delivered_and_paid(self.order)

    def _run_concurrently(self, targets):
        barrier = threading.Barrier(len(targets))
        results = []
        errors = []

        def worker(target):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                results.append(target())
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=worker, args=(target,))
            for target in targets
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        return results, errors

    def test_concurrent_duplicate_requests_create_one_record(self):
        results, errors = self._run_concurrently(
            [
                lambda: request_return(
                    self.user,
                    self.item.pk,
                    reason="First",
                ),
                lambda: request_return(
                    self.user,
                    self.item.pk,
                    reason="Second",
                ),
            ]
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReturnAlreadyRequestedError)
        self.assertEqual(ReturnRequest.objects.count(), 1)
        self.assertEqual(ReturnItem.objects.count(), 1)

    def test_concurrent_approvals_restore_stock_once(self):
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        results, errors = self._run_concurrently(
            [
                lambda: approve_return(
                    return_item.pk,
                    admin=self.admin,
                ),
                lambda: approve_return(
                    return_item.pk,
                    admin=self.admin,
                ),
            ]
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReturnAlreadyDecidedError)
        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(return_item.status, ReturnItem.Status.APPROVED)
        self.assertIsNotNone(return_item.stock_restored_at)
        self.assertEqual(self.variant.stock, 12)

    def test_concurrent_approval_and_rejection_have_one_winner(self):
        return_request = request_return(
            self.user,
            self.item.pk,
            reason="Damaged",
        )
        return_item = return_request.items.get()

        results, errors = self._run_concurrently(
            [
                lambda: approve_return(
                    return_item.pk,
                    admin=self.admin,
                ),
                lambda: reject_return(
                    return_item.pk,
                    admin=self.admin,
                    note="Rejected",
                ),
            ]
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReturnAlreadyDecidedError)
        return_item.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertIn(
            return_item.status,
            [ReturnItem.Status.APPROVED, ReturnItem.Status.REJECTED],
        )
        if return_item.status == ReturnItem.Status.APPROVED:
            self.assertEqual(self.variant.stock, 12)
            self.assertIsNotNone(return_item.stock_restored_at)
        else:
            self.assertEqual(self.variant.stock, 10)
            self.assertIsNone(return_item.stock_restored_at)

    def test_concurrent_sibling_approvals_both_succeed(self):
        second_variant = make_variant(
            self.product,
            sku="SKU-CONCURRENT-2",
            stock=10,
        )
        second_item = add_item(self.order, second_variant, quantity=1)
        return_request, return_items = make_grouped_return(
            self.order,
            self.user,
            [
                (self.item, "First"),
                (second_item, "Second"),
            ],
        )

        results, errors = self._run_concurrently(
            [
                lambda: approve_return(
                    return_items[0].pk,
                    admin=self.admin,
                ),
                lambda: approve_return(
                    return_items[1].pk,
                    admin=self.admin,
                ),
            ]
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(errors, [])
        self.variant.refresh_from_db()
        second_variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 12)
        self.assertEqual(second_variant.stock, 11)
        self.assertEqual(
            return_request.items.filter(
                status=ReturnItem.Status.APPROVED
            ).count(),
            2,
        )

    def test_concurrent_final_item_cancellations_emit_one_event(self):
        second_variant = make_variant(
            self.product,
            sku="SKU-CANCEL-2",
            stock=10,
        )
        second_item = add_item(self.order, second_variant, quantity=1)
        self.item.quantity = 1
        self.item.line_total = self.item.unit_price
        self.item.save(update_fields=["quantity", "line_total"])
        self.order.status = Order.OrderStatus.PENDING
        self.order.save(update_fields=["status", "updated_at"])

        results, errors = self._run_concurrently(
            [
                lambda: cancel_order_item(self.user, self.item.pk),
                lambda: cancel_order_item(self.user, second_item.pk),
            ]
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(errors, [])
        self.order.refresh_from_db()
        self.item.refresh_from_db()
        second_item.refresh_from_db()
        self.variant.refresh_from_db()
        second_variant.refresh_from_db()
        self.assertEqual(self.order.status, Order.OrderStatus.CANCELLED)
        self.assertIsNotNone(self.item.cancelled_at)
        self.assertIsNotNone(second_item.cancelled_at)
        self.assertEqual(self.order.status_events.count(), 1)
        self.assertEqual(
            self.order.status_events.get().status,
            Order.OrderStatus.CANCELLED,
        )
        self.assertEqual(self.variant.stock, 11)
        self.assertEqual(second_variant.stock, 11)

    def test_concurrent_same_item_cancellations_restore_stock_once(self):
        self.order.status = Order.OrderStatus.PENDING
        self.order.save(update_fields=["status", "updated_at"])

        results, errors = self._run_concurrently(
            [
                lambda: cancel_order_item(self.user, self.item.pk),
                lambda: cancel_order_item(self.user, self.item.pk),
            ]
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(
            errors[0],
            (ItemAlreadyCancelledError, ItemNotCancellableError),
        )
        self.variant.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.variant.stock, 12)
        self.assertIsNotNone(self.item.cancelled_at)
        self.assertEqual(self.order.status_events.count(), 1)


class ReturnMigrationTests(TransactionTestCase):
    reset_sequences = True
    migrate_from = [("orders", "0003_phase6_1_order_management")]
    migrate_to = [("orders", "0004_return_item_decision_architecture")]

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.executor.loader.build_graph()
        self.old_apps = self.executor.loader.project_state(
            self.migrate_from
        ).apps
        self._seed_legacy_returns()

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)
        super().tearDown()

    def _seed_legacy_returns(self):
        apps = self.old_apps
        User = apps.get_model("auth", "User")
        Category = apps.get_model("categories", "Category")
        Brand = apps.get_model("brands", "Brand")
        Product = apps.get_model("products", "Product")
        ProductVariant = apps.get_model("products", "ProductVariant")
        Order = apps.get_model("orders", "Order")
        OrderItem = apps.get_model("orders", "OrderItem")
        ReturnRequest = apps.get_model("orders", "ReturnRequest")
        ReturnItem = apps.get_model("orders", "ReturnItem")

        self.admin = User.objects.create(
            username="legacyadmin",
            email="legacyadmin@example.com",
            password="legacy",
            is_staff=True,
        )
        self.user = User.objects.create(
            username="legacyuser",
            email="legacyuser@example.com",
            password="legacy",
        )

        category = Category.objects.create(
            name="Legacy Category",
            description="Legacy category",
        )
        brand = Brand.objects.create(
            category_id=category.pk,
            name="Legacy Brand",
            description="Legacy brand",
            listing_image="brands/listing/test.jpg",
            listing_description="Listing description",
            hero_image="brands/hero/test.jpg",
            hero_description="Hero description",
        )
        product = Product.objects.create(
            brand_id=brand.pk,
            name="Legacy Product",
            description="Legacy product",
            fragrance_concentration="EAU_DE_PARFUM",
        )
        variants = [
            ProductVariant.objects.create(
                product_id=product.pk,
                size="50ml",
                sku=f"LEGACY-SKU-{index}",
                price=Decimal("100.00"),
                stock=10,
            )
            for index in range(5)
        ]

        self.order_number = "AURA-LEGACY-0001"
        order = Order.objects.create(
            order_number=self.order_number,
            user_id=self.user.pk,
            status="DELIVERED",
            subtotal=Decimal("1000.00"),
            total=Decimal("1000.00"),
            delivery_full_name="Legacy User",
            delivery_phone_number="9876543210",
            delivery_address_line1="1 Legacy Street",
            delivery_city="Mumbai",
            delivery_state="Maharashtra",
            delivery_pincode="400001",
        )
        self.order_id = order.pk

        order_items = []
        for index in range(5):
            order_items.append(
                OrderItem.objects.create(
                    order_id=order.pk,
                    variant_id=variants[index].pk,
                    product_name="Legacy Product",
                    brand_name="Legacy Brand",
                    variant_size="50ml",
                    sku=f"LEGACY-SKU-{index}",
                    unit_price=Decimal("100.00"),
                    quantity=2,
                    line_total=Decimal("200.00"),
                )
            )

        now = timezone.now()
        requested_time = now - timedelta(days=3)
        rejected_time = now - timedelta(days=2)
        approved_time = now - timedelta(days=1)

        requested_request = ReturnRequest.objects.create(
            order_id=order.pk,
            requested_by_id=self.user.pk,
            reason="Requested reason",
            status="REQUESTED",
        )
        self.requested_request_id = requested_request.pk
        self.requested_item_id = ReturnItem.objects.create(
            return_request_id=requested_request.pk,
            order_item_id=order_items[0].pk,
            quantity=order_items[0].quantity,
        ).pk
        ReturnRequest.objects.filter(pk=requested_request.pk).update(
            requested_at=requested_time,
            updated_at=requested_time,
        )

        rejected_request = ReturnRequest.objects.create(
            order_id=order.pk,
            requested_by_id=self.user.pk,
            reason="Rejected reason",
            status="REJECTED",
            decided_at=rejected_time,
            decided_by_id=self.admin.pk,
            decision_note="Rejected note",
        )
        self.rejected_request_id = rejected_request.pk
        self.rejected_item_id = ReturnItem.objects.create(
            return_request_id=rejected_request.pk,
            order_item_id=order_items[1].pk,
            quantity=order_items[1].quantity,
        ).pk
        ReturnRequest.objects.filter(pk=rejected_request.pk).update(
            requested_at=rejected_time,
            updated_at=rejected_time,
        )

        approved_request = ReturnRequest.objects.create(
            order_id=order.pk,
            requested_by_id=self.user.pk,
            reason="Approved reason",
            status="APPROVED",
            decided_at=approved_time,
            decided_by_id=self.admin.pk,
            decision_note="Approved note",
        )
        self.approved_request_id = approved_request.pk
        self.approved_item_id = ReturnItem.objects.create(
            return_request_id=approved_request.pk,
            order_item_id=order_items[2].pk,
            quantity=order_items[2].quantity,
        ).pk
        ReturnRequest.objects.filter(pk=approved_request.pk).update(
            requested_at=approved_time,
            updated_at=approved_time,
        )

        grouped_request = ReturnRequest.objects.create(
            order_id=order.pk,
            requested_by_id=self.user.pk,
            reason="Grouped reason",
            status="REQUESTED",
        )
        self.grouped_request_id = grouped_request.pk
        self.grouped_item_ids = [
            ReturnItem.objects.create(
                return_request_id=grouped_request.pk,
                order_item_id=order_items[3].pk,
                quantity=order_items[3].quantity,
            ).pk,
            ReturnItem.objects.create(
                return_request_id=grouped_request.pk,
                order_item_id=order_items[4].pk,
                quantity=order_items[4].quantity,
            ).pk,
        ]
        ReturnRequest.objects.filter(pk=grouped_request.pk).update(
            requested_at=requested_time,
            updated_at=requested_time,
        )

        self.requested_time = requested_time
        self.rejected_time = rejected_time
        self.approved_time = approved_time
        self.admin_id = self.admin.pk
        self.user_id = self.user.pk

    def _migrate_to_new(self):
        self.executor.loader.build_graph()
        self.executor.migrate(self.migrate_to)
        self.executor.loader.build_graph()

    def _legacy_request_model(self):
        return self.old_apps.get_model("orders", "ReturnRequest")

    def _legacy_item_model(self):
        return self.old_apps.get_model("orders", "ReturnItem")

    def test_migration_rejects_orphan_return_request(self):
        ReturnRequest = self._legacy_request_model()
        orphan = ReturnRequest.objects.create(
            order_id=self.order_id,
            requested_by_id=self.user_id,
            reason="Orphan reason",
            status="REQUESTED",
        )

        with self.assertRaises(RuntimeError):
            self._migrate_to_new()

        ReturnRequest.objects.filter(pk=orphan.pk).delete()

    def test_migration_rejects_approved_multi_item_request(self):
        ReturnRequest = self._legacy_request_model()
        ReturnRequest.objects.filter(pk=self.grouped_request_id).update(
            status="APPROVED",
            decided_at=self.approved_time,
            decided_by_id=self.admin_id,
            decision_note="Approved group",
        )

        with self.assertRaises(RuntimeError):
            self._migrate_to_new()

        ReturnRequest.objects.filter(pk=self.grouped_request_id).update(
            status="REQUESTED",
            decided_at=None,
            decided_by_id=None,
            decision_note="",
        )

    def test_legacy_return_data_is_preserved_and_constraints_are_present(self):
        self._migrate_to_new()

        requested = ReturnRequest.objects.get(pk=self.requested_request_id)
        rejected = ReturnRequest.objects.get(pk=self.rejected_request_id)
        approved = ReturnRequest.objects.get(pk=self.approved_request_id)
        grouped = ReturnRequest.objects.get(pk=self.grouped_request_id)

        for field_name in [
            "reason",
            "status",
            "requested_at",
            "decided_at",
            "decided_by",
            "decision_note",
            "updated_at",
        ]:
            with self.assertRaises(FieldDoesNotExist):
                ReturnRequest._meta.get_field(field_name)

        numbers = {
            requested.request_number,
            rejected.request_number,
            approved.request_number,
            grouped.request_number,
        }
        self.assertEqual(len(numbers), 4)
        for number in numbers:
            self.assertRegex(number, r"RET-\d{8}-[0-9A-F]{8}")
        self.assertEqual(requested.created_at, self.requested_time)

        requested_item = ReturnItem.objects.get(pk=self.requested_item_id)
        self.assertEqual(requested_item.reason, "Requested reason")
        self.assertEqual(requested_item.status, ReturnItem.Status.REQUESTED)
        self.assertEqual(requested_item.requested_at, self.requested_time)
        self.assertIsNone(requested_item.decided_at)
        self.assertIsNone(requested_item.stock_restored_at)

        rejected_item = ReturnItem.objects.get(pk=self.rejected_item_id)
        self.assertEqual(rejected_item.status, ReturnItem.Status.REJECTED)
        self.assertEqual(rejected_item.decided_at, self.rejected_time)
        self.assertEqual(rejected_item.decided_by_id, self.admin_id)
        self.assertEqual(rejected_item.decision_note, "Rejected note")
        self.assertIsNone(rejected_item.stock_restored_at)

        approved_item = ReturnItem.objects.get(pk=self.approved_item_id)
        self.assertEqual(approved_item.status, ReturnItem.Status.APPROVED)
        self.assertEqual(approved_item.decided_at, self.approved_time)
        self.assertEqual(approved_item.decided_by_id, self.admin_id)
        self.assertEqual(approved_item.decision_note, "Approved note")
        self.assertEqual(
            approved_item.stock_restored_at,
            self.approved_time,
        )

        grouped_items = list(
            ReturnItem.objects.filter(
                pk__in=self.grouped_item_ids
            ).order_by("pk")
        )
        self.assertEqual(len(grouped_items), 2)
        for item in grouped_items:
            self.assertEqual(item.reason, "Grouped reason")
            self.assertEqual(item.status, ReturnItem.Status.REQUESTED)
            self.assertEqual(item.requested_at, self.requested_time)

        order = Order.objects.get(order_number=self.order_number)
        self.assertEqual(order.subtotal, Decimal("1000.00"))
        self.assertEqual(order.total, Decimal("1000.00"))

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor,
                "orders_returnitem",
            )
        self.assertIn("orders_return_item_status_idx", constraints)
        self.assertIn("return_item_reason_required", constraints)
        self.assertIn(
            "return_item_requested_has_no_decision",
            constraints,
        )
        self.assertIn("return_item_decided_has_timestamp", constraints)
        self.assertIn("return_item_rejected_note_required", constraints)
        self.assertIn(
            "return_item_stock_restored_only_approved",
            constraints,
        )
        self.assertTrue(
            ReturnRequest._meta.get_field("request_number").unique
        )

        self.executor.loader.build_graph()
        self.executor.migrate(self.migrate_from)
        self.executor.loader.build_graph()
        old_apps = self.executor.loader.project_state(
            self.migrate_from
        ).apps
        OldReturnRequest = old_apps.get_model(
            "orders",
            "ReturnRequest",
        )
        old_request = OldReturnRequest.objects.get(
            pk=self.rejected_request_id
        )
        self.assertEqual(old_request.reason, "Rejected reason")
        self.assertEqual(old_request.status, "REJECTED")
        self.assertEqual(old_request.decision_note, "Rejected note")
        self.assertEqual(old_request.decided_by_id, self.admin_id)

    def test_reverse_migration_rejects_divergent_sibling_decisions(self):
        self._migrate_to_new()
        now = timezone.now()
        ReturnItem.objects.filter(pk=self.grouped_item_ids[0]).update(
            status=ReturnItem.Status.APPROVED,
            decided_at=now,
            decided_by_id=self.admin_id,
            decision_note="Diverged",
            stock_restored_at=now,
        )

        self.executor.loader.build_graph()
        with self.assertRaises(IrreversibleError):
            self.executor.migrate(self.migrate_from)

        self.executor.loader.build_graph()
        self.executor.migrate(self.migrate_to)
