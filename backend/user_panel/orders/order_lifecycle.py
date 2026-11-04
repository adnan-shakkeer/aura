"""Order-level fulfilment transitions and cash-on-delivery collection."""

from django.db import transaction
from django.utils import timezone

from .exceptions import (
    AdminPermissionError,
    InvalidOrderStatusError,
    InvalidOrderStatusTransitionError,
    OrderNotFoundError,
)
from .financials import get_order_financials
from .models import Order, OrderStatusEvent

_ADMIN_TRANSITIONS = {
    Order.OrderStatus.PENDING: {Order.OrderStatus.SHIPPED},
    Order.OrderStatus.SHIPPED: {Order.OrderStatus.OUT_FOR_DELIVERY},
    Order.OrderStatus.OUT_FOR_DELIVERY: {Order.OrderStatus.DELIVERED},
}

_ADMIN_TARGET_STATUSES = {
    Order.OrderStatus.SHIPPED,
    Order.OrderStatus.OUT_FOR_DELIVERY,
    Order.OrderStatus.DELIVERED,
}


def _assert_admin(actor):
    if (
        actor is None
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
        or not (
            getattr(actor, "is_staff", False)
            or getattr(actor, "is_superuser", False)
        )
    ):
        raise AdminPermissionError(
            "Only an administrator can update an order's status."
        )


def _collect_payment(order, actor):
    financials = get_order_financials(order)
    now = timezone.now()

    order.payment_status = Order.PaymentStatus.PAID
    if order.payment_collected_at is None:
        order.payment_collected_at = now
    if order.payment_collected_amount is None:
        order.payment_collected_amount = financials.total_due
    if order.payment_collected_by_id is None:
        order.payment_collected_by = actor


@transaction.atomic
def transition_order_status(order_id, new_status, *, actor, note=""):
    """Move an order through the admin-controlled fulfilment workflow.

    Cancellation is deliberately excluded: it is owned by the cancellation
    service and only happens when every item has been cancelled.
    """
    _assert_admin(actor)

    new_status = str(new_status)
    if new_status not in Order.OrderStatus.values:
        raise InvalidOrderStatusError("Unknown order status.")

    if new_status not in _ADMIN_TARGET_STATUSES:
        raise InvalidOrderStatusTransitionError(
            "Administrators cannot move an order to this status."
        )

    order = Order.objects.select_for_update().filter(pk=order_id).first()
    if order is None:
        raise OrderNotFoundError("Order not found.")

    if order.status == new_status:
        if (
            new_status == Order.OrderStatus.DELIVERED
            and order.payment_status != Order.PaymentStatus.PAID
        ):
            _collect_payment(order, actor)
            order.save(
                update_fields=[
                    "payment_status",
                    "payment_collected_at",
                    "payment_collected_amount",
                    "payment_collected_by",
                    "updated_at",
                ]
            )
        return order

    if new_status not in _ADMIN_TRANSITIONS.get(order.status, set()):
        raise InvalidOrderStatusTransitionError(
            "This status change is not allowed for the current order."
        )

    previous_status = order.status
    order.status = new_status

    update_fields = ["status", "updated_at"]

    if new_status == Order.OrderStatus.DELIVERED:
        _collect_payment(order, actor)
        update_fields.extend(
            [
                "payment_status",
                "payment_collected_at",
                "payment_collected_amount",
                "payment_collected_by",
            ]
        )

    order.save(update_fields=update_fields)

    OrderStatusEvent.objects.create(
        order=order,
        status=new_status,
        previous_status=previous_status,
        changed_by=actor,
        note=(note or "").strip(),
    )

    return order
