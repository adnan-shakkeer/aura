"""Wallet refund integration for cancellations and approved returns.

Eligibility is decided from authoritative order/payment records, never from
frontend input or current catalogue prices. Refund amounts are supplied by the
existing financial snapshot helpers in the calling workflow.

Wallet credits replace the ordinary manual Razorpay refund obligation for
eligible cancellations and returns. ``LATE_PAYMENT`` and
``DUPLICATE_PAYMENT`` obligations are intentionally untouched: they are
created by the payment service, not by these helpers.
"""

from decimal import Decimal, ROUND_HALF_UP

from user_panel.orders.models import Order

from .models import WalletTransaction
from .services import credit_wallet, get_or_create_wallet


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def _money(value):
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _positive_amount(amount):
    try:
        value = _money(amount or ZERO)
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not value.is_finite():
        return None
    if value <= ZERO:
        return None
    return value


def _order_is_paid(order):
    """Return True only when money was actually collected for ``order``.

    Razorpay payments set ``payment_collected_amount`` when captured, and a
    delivered COD order sets it when cash is collected. An unpaid COD order or
    an expired unpaid Razorpay order therefore never qualifies.
    """
    if order is None:
        return False
    if order.payment_status != Order.PaymentStatus.PAID:
        return False

    collected = order.payment_collected_amount
    if collected is not None and _money(collected) > ZERO:
        return True

    # Defensive fallback for legacy paid Razorpay orders whose collected
    # snapshot is missing but a captured attempt exists.
    if order.payment_method == Order.PaymentMethod.RAZORPAY:
        from user_panel.payments.services import (  # noqa: PLC0415
            captured_attempt_for_order,
        )

        return captured_attempt_for_order(order) is not None

    return False


def _credit(
    order,
    amount,
    *,
    kind,
    dedupe_key,
    description="",
    order_item=None,
):
    value = _positive_amount(amount)
    if value is None:
        return None
    if order is None or order.user_id is None:
        return None
    if not _order_is_paid(order):
        return None

    wallet = get_or_create_wallet(order.user)

    return credit_wallet(
        wallet=wallet,
        amount=value,
        kind=kind,
        dedupe_key=dedupe_key,
        description=description,
        order=order,
        order_item=order_item,
    )


def credit_cancellation_refund(
    order,
    amount,
    *,
    dedupe_key,
    description="",
    order_item=None,
):
    """Credit an eligible paid cancellation refund to the wallet."""
    return _credit(
        order,
        amount,
        kind=WalletTransaction.Kind.REFUND_ORDER,
        dedupe_key=dedupe_key,
        description=description,
        order_item=order_item,
    )


def credit_return_refund(
    order,
    order_item,
    amount,
    *,
    dedupe_key,
    description="",
):
    """Credit an eligible approved-return refund to the wallet."""
    return _credit(
        order,
        amount,
        kind=WalletTransaction.Kind.REFUND_RETURN,
        dedupe_key=dedupe_key,
        description=description,
        order_item=order_item,
    )
