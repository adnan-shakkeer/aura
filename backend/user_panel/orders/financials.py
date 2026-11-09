"""Dynamic financial calculations for orders.

Order and line-item monetary snapshots are immutable. Cancellations and
approved returns are applied on top of those snapshots when totals are
displayed, so no adjustment total is ever persisted on the order.
"""

from dataclasses import dataclass
from decimal import Decimal

from .models import Order, ReturnItem

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class OrderFinancials:
    subtotal: Decimal
    original_total: Decimal
    cancelled_total: Decimal
    approved_return_total: Decimal
    adjusted_total: Decimal
    amount_collected: Decimal
    total_due: Decimal
    credit_due: Decimal


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"))


def _item_paid_amount(item):
    """Return the historical amount paid for one line.

    Items created before pricing snapshots existed (or by legacy code paths)
    have no snapshot values. In that case the full line total was the paid
    amount; this preserves the pre-Phase-1 financial outcome.
    """
    paid_amount = _money(item.paid_amount or ZERO)
    discount_allocated = _money(item.discount_allocated or ZERO)
    line_total = _money(item.line_total)

    if (
        paid_amount == ZERO
        and discount_allocated == ZERO
        and line_total > ZERO
    ):
        return line_total

    return paid_amount


def get_order_financials(order):
    """Return the current payable position for ``order``.

    ``adjusted_total`` is the original order total less cancelled lines and
    approved returns. ``total_due`` is the outstanding balance after money
    already collected, while ``credit_due`` is an informational amount owed
    back to the customer. No refund records are created here.
    """
    cancelled_total = ZERO
    approved_return_total = ZERO

    items = order.items.select_related("return_item").all()

    for item in items:
        if item.cancelled_at is not None:
            cancelled_total += _item_paid_amount(item)
            continue

        return_item = getattr(item, "return_item", None)
        if (
            return_item is not None
            and return_item.status == ReturnItem.Status.APPROVED
        ):
            approved_return_total += _item_paid_amount(item)

    original_total = _money(order.total)
    cancelled_total = _money(cancelled_total)
    approved_return_total = _money(approved_return_total)
    adjusted_total = max(
        original_total - cancelled_total - approved_return_total,
        ZERO,
    )

    if order.payment_status == Order.PaymentStatus.PAID:
        amount_collected = order.payment_collected_amount
        if amount_collected is None:
            amount_collected = original_total
    else:
        amount_collected = ZERO

    amount_collected = _money(amount_collected)
    total_due = max(adjusted_total - amount_collected, ZERO)
    credit_due = max(amount_collected - adjusted_total, ZERO)

    return OrderFinancials(
        subtotal=_money(order.subtotal),
        original_total=original_total,
        cancelled_total=cancelled_total,
        approved_return_total=approved_return_total,
        adjusted_total=_money(adjusted_total),
        amount_collected=amount_collected,
        total_due=_money(total_due),
        credit_due=_money(credit_due),
    )
