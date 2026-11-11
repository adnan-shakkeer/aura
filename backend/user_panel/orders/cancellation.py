"""Customer-owned cancellation of individual order items."""

from django.db import transaction
from django.utils import timezone

from admin_panel.products.models import ProductVariant

from .coupons import release_coupon_usage_for_order
from .exceptions import (
    ItemAlreadyCancelledError,
    ItemNotCancellableError,
    OrderItemNotFoundError,
    OrderPermissionError,
    OrderServiceError,
)
from .models import Order, OrderItem, OrderStatusEvent


COUPON_RELEASE_NOTE = "Order cancelled by customer."


def _assert_customer(user):
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        raise OrderPermissionError(
            "You do not have permission to cancel this item."
        )


def _lock_order_item(order_item_id):
    item = (
        OrderItem.objects.select_related("order")
        .filter(pk=order_item_id)
        .first()
    )
    if item is None:
        raise OrderItemNotFoundError("Order item not found.")

    order = Order.objects.select_for_update().filter(pk=item.order_id).first()
    if order is None:
        raise OrderItemNotFoundError("Order item not found.")

    item = (
        OrderItem.objects.select_for_update()
        .select_related("order", "variant")
        .get(pk=item.pk)
    )

    return order, item


def _restore_stock(item):
    variant = (
        ProductVariant.objects.select_for_update()
        .filter(pk=item.variant_id)
        .first()
    )
    if variant is None:
        raise OrderServiceError(
            "The product variant for this item no longer exists."
        )

    variant.stock += item.quantity
    variant.save(update_fields=["stock", "updated_at"])


@transaction.atomic
def cancel_order_item(user, order_item_id, *, reason=""):
    """Cancel one full order item while its order is still pending.

    Stock is restored exactly once. When the last active item is cancelled,
    the order itself becomes cancelled and an order-level event is recorded.
    """
    _assert_customer(user)

    order, item = _lock_order_item(order_item_id)

    if order.user_id != user.pk:
        raise OrderPermissionError(
            "You do not have permission to cancel this item."
        )

    if order.status != Order.OrderStatus.PENDING:
        raise ItemNotCancellableError(
            "This item can no longer be cancelled."
        )

    if item.cancelled_at is not None:
        raise ItemAlreadyCancelledError("This item is already cancelled.")

    item.cancelled_at = timezone.now()
    item.cancellation_reason = (reason or "").strip()
    item.save(update_fields=["cancelled_at", "cancellation_reason"])

    _restore_stock(item)

    has_active_items = (
        order.items.filter(cancelled_at__isnull=True)
        .exclude(pk=item.pk)
        .exists()
    )

    if not has_active_items:
        previous_status = order.status
        order.status = Order.OrderStatus.CANCELLED
        order.save(update_fields=["status", "updated_at"])
        OrderStatusEvent.objects.create(
            order=order,
            status=Order.OrderStatus.CANCELLED,
            previous_status=previous_status,
            changed_by=user,
            note="All items were cancelled by the customer.",
        )

        # The whole order is cancelled: its consumed coupon usage is
        # released exactly once (idempotent).
        release_coupon_usage_for_order(
            order,
            reason=COUPON_RELEASE_NOTE,
        )

    return item


@transaction.atomic
def cancel_order(user, order_id, *, reason=""):
    """Cancel all remaining active items in an order while it is still pending.

    The order status is set to CANCELLED and an OrderStatusEvent is recorded.
    Stock is restored exactly once for each active item's variant.
    The entire operation is atomic.
    """
    _assert_customer(user)

    order = Order.objects.select_for_update().filter(pk=order_id).first()
    if order is None:
        raise OrderServiceError("Order not found.")

    if order.user_id != user.pk:
        raise OrderPermissionError(
            "You do not have permission to cancel this order."
        )

    if order.status != Order.OrderStatus.PENDING:
        raise ItemNotCancellableError(
            "This order can no longer be cancelled."
        )

    # Lock all active items belonging to this order
    active_items = list(
        OrderItem.objects.select_for_update()
        .filter(order=order, cancelled_at__isnull=True)
        .order_by("id")
    )

    if not active_items:
        raise ItemAlreadyCancelledError(
            "All items in this order are already cancelled."
        )

    now = timezone.now()
    clean_reason = (reason or "").strip()

    # Sort items by variant_id to guarantee deterministic lock acquisition and prevent deadlocks
    sorted_items = sorted(active_items, key=lambda x: x.variant_id)
    for item in sorted_items:
        item.cancelled_at = now
        item.cancellation_reason = clean_reason
        item.save(update_fields=["cancelled_at", "cancellation_reason"])
        _restore_stock(item)

    previous_status = order.status
    order.status = Order.OrderStatus.CANCELLED
    order.save(update_fields=["status", "updated_at"])

    event_note = (
        f"Order cancelled by customer. Reason: {clean_reason}"
        if clean_reason
        else "Order cancelled by customer."
    )
    OrderStatusEvent.objects.create(
        order=order,
        status=Order.OrderStatus.CANCELLED,
        previous_status=previous_status,
        changed_by=user,
        note=event_note,
    )

    # The whole order is cancelled: its consumed coupon usage is released
    # exactly once (idempotent).
    release_coupon_usage_for_order(
        order,
        reason=COUPON_RELEASE_NOTE,
    )

    return order
