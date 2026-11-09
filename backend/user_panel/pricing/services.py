"""Single source of truth for order pricing.

Product and category offers are resolved here using the same catalogue
eligibility hierarchy enforced by the cart: the variant, product, brand and
category must all be active and not soft-deleted. The larger offer wins; a
product offer wins an equal non-zero tie with a category offer.

All monetary values are ``Decimal`` and quantized to two decimal places using
``ROUND_HALF_UP``. Coupons are not implemented in Phase 1 and remain zero.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")
PERCENT_DIVISOR = Decimal("100")

OFFER_SOURCE_NONE = "NONE"
OFFER_SOURCE_PRODUCT = "PRODUCT"
OFFER_SOURCE_CATEGORY = "CATEGORY"


@dataclass(frozen=True)
class LinePricing:
    variant_id: int
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    offer_source: str
    offer_percentage: Decimal
    offer_discount: Decimal
    coupon_discount: Decimal
    paid_amount: Decimal


def _money(value):
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal(value):
    if value is None:
        return ZERO
    return Decimal(value)


def is_variant_offer_eligible(variant):
    """Return whether an offer may apply to ``variant``.

    This mirrors the eligibility hierarchy used by
    ``user_panel.cart.services._get_eligible_variant`` without issuing an
    extra query, because the caller already loaded the related rows.
    """
    product = variant.product
    brand = product.brand
    category = brand.category

    return not (
        variant.is_deleted
        or not variant.is_listed
        or product.is_deleted
        or not product.is_listed
        or brand.is_deleted
        or not brand.is_listed
        or category.is_deleted
        or not category.is_listed
    )


def resolve_offer(variant):
    """Return ``(source, percentage)`` for the winning offer."""
    if not is_variant_offer_eligible(variant):
        return OFFER_SOURCE_NONE, ZERO

    product_percentage = _decimal(
        getattr(variant.product, "offer_percentage", ZERO)
    )
    category_percentage = _decimal(
        variant.product.brand.category.offer_percentage
    )

    if product_percentage > 0 and product_percentage >= category_percentage:
        return OFFER_SOURCE_PRODUCT, product_percentage

    if category_percentage > 0:
        return OFFER_SOURCE_CATEGORY, category_percentage

    return OFFER_SOURCE_NONE, ZERO


def price_variant(variant, quantity):
    """Price one line from live catalogue data and return a snapshot."""
    quantity = int(quantity)
    unit_price = _money(variant.price)
    line_total = _money(unit_price * quantity)

    offer_source, offer_percentage = resolve_offer(variant)
    if offer_percentage > 0:
        offer_discount = _money(
            line_total * offer_percentage / PERCENT_DIVISOR
        )
    else:
        offer_discount = ZERO

    if offer_discount > line_total:
        offer_discount = line_total

    coupon_discount = ZERO
    paid_amount = _money(line_total - offer_discount - coupon_discount)

    return LinePricing(
        variant_id=variant.pk,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        offer_source=offer_source,
        offer_percentage=offer_percentage,
        offer_discount=offer_discount,
        coupon_discount=coupon_discount,
        paid_amount=paid_amount,
    )


def summarize(line_pricings):
    """Return order-level totals from line snapshots.

    The function accepts any objects exposing ``line_total``,
    ``offer_discount`` and an optional ``coupon_discount`` so the checkout
    review lines and the pricing snapshots share the same arithmetic.
    """
    subtotal = ZERO
    offer_discount_total = ZERO
    coupon_discount_total = ZERO

    for line in line_pricings:
        subtotal += _money(line.line_total)
        offer_discount_total += _money(line.offer_discount)
        coupon_discount_total += _money(
            getattr(line, "coupon_discount", ZERO)
        )

    subtotal = _money(subtotal)
    offer_discount_total = _money(offer_discount_total)
    coupon_discount_total = _money(coupon_discount_total)
    discount_total = _money(
        offer_discount_total + coupon_discount_total
    )
    shipping_charge = ZERO
    tax_total = ZERO
    total = _money(
        subtotal - discount_total + shipping_charge + tax_total
    )

    if total < ZERO:
        total = ZERO

    return {
        "subtotal": subtotal,
        "offer_discount_total": offer_discount_total,
        "coupon_discount_total": coupon_discount_total,
        "discount_total": discount_total,
        "shipping_charge": shipping_charge,
        "tax_total": tax_total,
        "total": total,
    }
