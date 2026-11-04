"""Customer return requests and admin return decisions."""

from django.db import IntegrityError, transaction
from django.utils import timezone

from admin_panel.products.models import ProductVariant

from .exceptions import (
    AdminPermissionError,
    OrderItemNotFoundError,
    OrderPermissionError,
    OrderServiceError,
    ReturnAlreadyDecidedError,
    ReturnAlreadyRequestedError,
    ReturnDecisionNoteRequiredError,
    ReturnIntegrityError,
    ReturnNotAllowedError,
    ReturnNotFoundError,
    ReturnQuantityMismatchError,
    ReturnReasonRequiredError,
)
from .models import Order, OrderItem, ReturnItem, ReturnRequest


def _assert_customer(user):
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        raise OrderPermissionError(
            "You do not have permission to request this return."
        )


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
            "Only an administrator can decide a return."
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
        .select_related("order")
        .get(pk=item.pk)
    )

    return order, item


def _lock_return_item(return_item_id):
    snapshot = (
        ReturnItem.objects.select_related("order_item")
        .filter(pk=return_item_id)
        .first()
    )
    if snapshot is None:
        raise ReturnNotFoundError("Return item not found.")

    order = (
        Order.objects.select_for_update()
        .filter(pk=snapshot.order_item.order_id)
        .first()
    )
    if order is None:
        raise ReturnNotFoundError("Return item not found.")

    return_request = (
        ReturnRequest.objects.select_for_update()
        .filter(pk=snapshot.return_request_id)
        .first()
    )
    if return_request is None:
        raise ReturnNotFoundError("Return item not found.")

    order_item = (
        OrderItem.objects.select_for_update()
        .filter(pk=snapshot.order_item_id)
        .first()
    )
    if order_item is None:
        raise ReturnNotFoundError("Return item not found.")

    return_item = (
        ReturnItem.objects.select_for_update()
        .select_related("order_item")
        .filter(pk=snapshot.pk)
        .first()
    )
    if return_item is None:
        raise ReturnNotFoundError("Return item not found.")

    if (
        return_request.order_id != order.pk
        or order_item.order_id != order.pk
        or return_item.order_item_id != order_item.pk
        or return_item.return_request_id != return_request.pk
    ):
        raise ReturnIntegrityError(
            "The return record is inconsistent with its order."
        )

    return order, return_request, return_item, order_item


def _restore_stock(order_item, quantity):
    variant = (
        ProductVariant.objects.select_for_update()
        .filter(pk=order_item.variant_id)
        .first()
    )
    if variant is None:
        raise OrderServiceError(
            "The product variant for this item no longer exists."
        )

    variant.stock += quantity
    variant.save(update_fields=["stock", "updated_at"])


def _is_request_number_collision(exc):
    cause = exc.__cause__
    diagnostics = getattr(cause, "diag", None)
    if diagnostics is None:
        return False

    constraint_name = getattr(diagnostics, "constraint_name", None)
    if constraint_name and constraint_name.startswith(
        "orders_returnrequest_request_number"
    ):
        return True

    return (
        getattr(diagnostics, "table_name", None)
        == ReturnRequest._meta.db_table
        and getattr(diagnostics, "column_name", None) == "request_number"
    )


def _create_return_request(order, user):
    for _attempt in range(5):
        request_number = ReturnRequest._generate_request_number()
        try:
            with transaction.atomic():
                return ReturnRequest.objects.create(
                    order=order,
                    requested_by=user,
                    request_number=request_number,
                )
        except IntegrityError as exc:
            if not _is_request_number_collision(exc):
                raise

    raise OrderServiceError(
        "We could not allocate a unique return request number."
    )


@transaction.atomic
def request_return(user, order_item_id, *, reason):
    """Create the single return request allowed for a delivered item."""
    _assert_customer(user)

    reason = (reason or "").strip()
    if not reason:
        raise ReturnReasonRequiredError(
            "A reason for the return is required."
        )

    order, item = _lock_order_item(order_item_id)

    if order.user_id != user.pk:
        raise OrderPermissionError(
            "You do not have permission to request this return."
        )

    if order.status != Order.OrderStatus.DELIVERED:
        raise ReturnNotAllowedError(
            "Only delivered items can be returned."
        )

    if item.cancelled_at is not None:
        raise ReturnNotAllowedError("Cancelled items cannot be returned.")

    if ReturnItem.objects.filter(order_item=item).exists():
        raise ReturnAlreadyRequestedError(
            "A return request already exists for this item."
        )

    return_request = _create_return_request(order, user)
    ReturnItem.objects.create(
        return_request=return_request,
        order_item=item,
        quantity=item.quantity,
        reason=reason,
    )

    return return_request


@transaction.atomic
def request_return_order(user, order_id, *, reason):
    """Create return requests for all remaining eligible items in a delivered order.

    All newly created ReturnItems are grouped under a single ReturnRequest.
    Cancelled items remain cancelled. Already returned or decided items are preserved.
    The entire operation is atomic.
    """
    _assert_customer(user)

    reason = (reason or "").strip()
    if not reason:
        raise ReturnReasonRequiredError(
            "A reason for the return is required."
        )

    order = Order.objects.select_for_update().filter(pk=order_id).first()
    if order is None:
        raise OrderServiceError("Order not found.")

    if order.user_id != user.pk:
        raise OrderPermissionError(
            "You do not have permission to request this return."
        )

    if order.status != Order.OrderStatus.DELIVERED:
        raise ReturnNotAllowedError(
            "Only delivered orders can be returned."
        )

    items = list(
        OrderItem.objects.select_for_update()
        .filter(order=order)
        .order_by("id")
    )

    existing_return_item_ids = set(
        ReturnItem.objects.filter(order_item__in=items).values_list(
            "order_item_id", flat=True
        )
    )

    eligible_items = [
        item
        for item in items
        if item.cancelled_at is None and item.pk not in existing_return_item_ids
    ]

    if not eligible_items:
        raise ReturnNotAllowedError(
            "No eligible items remain to be returned in this order."
        )

    return_request = _create_return_request(order, user)

    for item in eligible_items:
        ReturnItem.objects.create(
            return_request=return_request,
            order_item=item,
            quantity=item.quantity,
            reason=reason,
        )

    return return_request


@transaction.atomic
def approve_return(return_item_id, *, admin, note=""):
    """Approve a pending return and restore its full quantity to stock."""
    _assert_admin(admin)

    order, _return_request, return_item, order_item = _lock_return_item(
        return_item_id
    )

    if return_item.status != ReturnItem.Status.REQUESTED:
        raise ReturnAlreadyDecidedError(
            "This return item has already been decided."
        )

    if return_item.stock_restored_at is not None:
        raise ReturnIntegrityError(
            "This return item has inconsistent stock-restoration state."
        )

    if order.status != Order.OrderStatus.DELIVERED:
        raise ReturnNotAllowedError(
            "Only returns from delivered orders can be approved."
        )

    if order_item.cancelled_at is not None:
        raise ReturnNotAllowedError("Cancelled items cannot be returned.")

    if return_item.quantity != order_item.quantity:
        raise ReturnQuantityMismatchError(
            "The return quantity does not match the order item."
        )

    now = timezone.now()
    _restore_stock(order_item, return_item.quantity)

    return_item.status = ReturnItem.Status.APPROVED
    return_item.decided_at = now
    return_item.decided_by = admin
    return_item.decision_note = (note or "").strip()
    return_item.stock_restored_at = now
    return_item.save(
        update_fields=[
            "status",
            "decided_at",
            "decided_by",
            "decision_note",
            "stock_restored_at",
        ]
    )

    return return_item


@transaction.atomic
def reject_return(return_item_id, *, admin, note):
    """Reject a pending return; the rejection permanently blocks re-request."""
    _assert_admin(admin)

    note = (note or "").strip()
    if not note:
        raise ReturnDecisionNoteRequiredError(
            "A decision note is required when rejecting a return."
        )

    _order, _return_request, return_item, _order_item = _lock_return_item(
        return_item_id
    )

    if return_item.status != ReturnItem.Status.REQUESTED:
        raise ReturnAlreadyDecidedError(
            "This return item has already been decided."
        )

    if return_item.stock_restored_at is not None:
        raise ReturnIntegrityError(
            "This return item has inconsistent stock-restoration state."
        )

    return_item.status = ReturnItem.Status.REJECTED
    return_item.decided_at = timezone.now()
    return_item.decided_by = admin
    return_item.decision_note = note
    return_item.save(
        update_fields=[
            "status",
            "decided_at",
            "decided_by",
            "decision_note",
        ]
    )

    return return_item
