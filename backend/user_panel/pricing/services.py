"""Single source of truth for order pricing.

Product and category offers are resolved here using the same catalogue
eligibility hierarchy enforced by the cart: the variant, product, brand and
category must all be active and not soft-deleted. The larger offer wins; a
product offer wins an equal non-zero tie with a category offer.

All monetary values are ``Decimal`` and quantized to two decimal places using
``ROUND_HALF_UP``. Coupons are integrated in Phase 2: coupon discounts are
calculated against eligible post-offer merchandise and allocated deterministically
across order lines.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import (
    DecimalField,
    ExpressionWrapper,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, Greatest, Round

from admin_panel.offers.models import Offer, offer_today

MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")
PERCENT_DIVISOR = Decimal("100")

OFFER_SOURCE_NONE = "NONE"
OFFER_SOURCE_PRODUCT = "PRODUCT"
OFFER_SOURCE_CATEGORY = "CATEGORY"

COUPON_TYPE_PERCENTAGE = "PERCENTAGE"
COUPON_TYPE_FIXED = "FIXED"

_OFFER_PERCENTAGE_FIELD = DecimalField(max_digits=5, decimal_places=2)
_EFFECTIVE_PRICE_FIELD = DecimalField(max_digits=14, decimal_places=2)


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


def resolve_product_offer(product):
    """Return ``(source, percentage)`` for a product's winning offer.

    Product/brand/category eligibility is enforced here. Only active,
    non-deleted offers whose inclusive IST validity window contains today are
    considered. The highest percentage wins; a product offer wins an equal
    tie against a category offer.

    Callers that price a variant must still use ``resolve_offer()`` so variant
    listing eligibility is included as well.
    """
    brand = product.brand
    category = brand.category

    if (
        product.is_deleted
        or not product.is_listed
        or brand.is_deleted
        or not brand.is_listed
        or category.is_deleted
        or not category.is_listed
    ):
        return OFFER_SOURCE_NONE, ZERO

    today = offer_today()
    offers = list(
        Offer.objects.filter(
            is_deleted=False,
            is_active=True,
            percentage__gt=ZERO,
            valid_from__lte=today,
            valid_until__gte=today,
        )
        .filter(
            Q(
                target_type=Offer.TargetType.PRODUCT,
                product=product,
            )
            | Q(
                target_type=Offer.TargetType.CATEGORY,
                category=category,
            )
        )
        .values_list("target_type", "percentage")
    )

    product_percentage = max(
        (
            percentage
            for target_type, percentage in offers
            if target_type == Offer.TargetType.PRODUCT
        ),
        default=None,
    )
    category_percentage = max(
        (
            percentage
            for target_type, percentage in offers
            if target_type == Offer.TargetType.CATEGORY
        ),
        default=None,
    )

    if product_percentage is not None and (
        category_percentage is None
        or product_percentage >= category_percentage
    ):
        return OFFER_SOURCE_PRODUCT, _decimal(product_percentage)

    if category_percentage is not None:
        return OFFER_SOURCE_CATEGORY, _decimal(category_percentage)

    return OFFER_SOURCE_NONE, ZERO


def _active_offer_percentage_subquery(
    *,
    target_type,
    target_field,
    outer_ref,
):
    today = offer_today()
    return Subquery(
        Offer.objects.filter(
            is_deleted=False,
            is_active=True,
            percentage__gt=ZERO,
            target_type=target_type,
            valid_from__lte=today,
            valid_until__gte=today,
            **{target_field: outer_ref},
        )
        .order_by("-percentage", "id")
        .values("percentage")[:1],
        output_field=_OFFER_PERCENTAGE_FIELD,
    )


def winning_offer_percentage_expression(*, product_ref, category_ref):
    """Return the winning offer percentage as a database expression.

    This mirrors ``resolve_product_offer()`` for database-side listing
    operations. Both subqueries exclude soft-deleted, inactive, and
    out-of-window offers. The larger product/category offer wins; equal
    percentages have the same monetary result, while the Python resolver
    retains the product-source tie-break for snapshots.
    """
    product_offer = _active_offer_percentage_subquery(
        target_type=Offer.TargetType.PRODUCT,
        target_field="product_id",
        outer_ref=product_ref,
    )
    category_offer = _active_offer_percentage_subquery(
        target_type=Offer.TargetType.CATEGORY,
        target_field="category_id",
        outer_ref=category_ref,
    )

    return Greatest(
        Coalesce(
            product_offer,
            Value(ZERO, output_field=_OFFER_PERCENTAGE_FIELD),
        ),
        Coalesce(
            category_offer,
            Value(ZERO, output_field=_OFFER_PERCENTAGE_FIELD),
        ),
        output_field=_OFFER_PERCENTAGE_FIELD,
    )


def apply_offer_expression(price_expression, percentage_expression):
    """Return a database expression for an offer-adjusted price.

    Mirrors ``apply_offer()`` and keeps listing sorting/filtering aligned with
    the Python pricing path. The discount is rounded before subtraction,
    matching ``calculate_offer_discount()`` / ``apply_offer()`` exactly.
    Prices are non-negative, so PostgreSQL ``ROUND`` on numeric values
    matches ``ROUND_HALF_UP`` for that domain.
    """
    discount = Round(
        price_expression
        * percentage_expression
        / Value(PERCENT_DIVISOR, output_field=_OFFER_PERCENTAGE_FIELD),
        2,
    )

    return ExpressionWrapper(
        price_expression - discount,
        output_field=_EFFECTIVE_PRICE_FIELD,
    )


def resolve_offer(variant):
    """Return ``(source, percentage)`` for the winning offer."""
    if not is_variant_offer_eligible(variant):
        return OFFER_SOURCE_NONE, ZERO

    return resolve_product_offer(variant.product)


def calculate_offer_discount(amount, percentage):
    """Return the capped offer discount for ``amount``."""
    amount = _money(amount)
    percentage = _decimal(percentage)

    if percentage <= 0:
        return ZERO

    discount = _money(amount * percentage / PERCENT_DIVISOR)

    if discount > amount:
        discount = amount

    return discount


def apply_offer(amount, percentage):
    """Return ``amount`` after the percentage offer is applied."""
    amount = _money(amount)
    return _money(amount - calculate_offer_discount(amount, percentage))


def unit_price_after_offer(variant):
    """Return one variant's effective unit price after its winning offer."""
    unit_price = _money(variant.price)
    _source, percentage = resolve_offer(variant)
    return apply_offer(unit_price, percentage)


def price_variant(variant, quantity):
    """Price one line from live catalogue data and return a snapshot."""
    quantity = int(quantity)
    unit_price = _money(variant.price)
    line_total = _money(unit_price * quantity)

    offer_source, offer_percentage = resolve_offer(variant)
    offer_discount = calculate_offer_discount(
        line_total,
        offer_percentage,
    )

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


def eligible_merchandise_after_offers(line_pricings):
    """Return the total eligible merchandise value after offers.

    This is the sum of ``line_total - offer_discount`` across all lines,
    representing the merchandise subtotal that a coupon's minimum purchase
    and discount are evaluated against.
    """
    total = ZERO
    for line in line_pricings:
        eligible = _money(line.line_total) - _money(line.offer_discount)
        if eligible > ZERO:
            total += eligible
    return _money(total)


def calculate_coupon_discount(discount_type, discount_value, max_discount_amount, eligible_merchandise):
    """Calculate the final coupon discount amount.

    Rules:
    - Never allows the final discount to exceed the eligible merchandise subtotal.
    - Never allows the final payable amount to become negative.

    Percentage: final_discount = min(eligible * percentage / 100, max_discount_amount, eligible)
    Fixed:      final_discount = min(fixed_amount, eligible)
    """
    if eligible_merchandise <= ZERO:
        return ZERO

    discount_value = _money(discount_value)
    eligible = _money(eligible_merchandise)

    if discount_type == COUPON_TYPE_PERCENTAGE:
        max_cap = _money(max_discount_amount) if max_discount_amount is not None else ZERO
        percentage_discount = _money(eligible * discount_value / PERCENT_DIVISOR)
        final_discount = min(percentage_discount, max_cap, eligible)
    elif discount_type == COUPON_TYPE_FIXED:
        final_discount = min(discount_value, eligible)
    else:
        return ZERO

    if final_discount < ZERO:
        final_discount = ZERO
    if final_discount > eligible:
        final_discount = eligible

    return _money(final_discount)


def allocate_coupon_discount(coupon_discount_total, line_pricings):
    """Allocate a coupon discount across order lines deterministically.

    Uses post-offer line values (line_total - offer_discount) as the basis
    for proportional allocation. Never allocates more than a line's eligible
    post-offer amount. Distributes rounding remainders deterministically so
    the sum of allocations exactly equals the order-level coupon discount.

    Returns a list of Decimal coupon allocations, one per line, in the same
    order as ``line_pricings``.
    """
    coupon_discount_total = _money(coupon_discount_total)

    if coupon_discount_total <= ZERO:
        return [ZERO for _ in line_pricings]

    # Calculate each line's eligible post-offer value
    line_eligibles = []
    total_eligible = ZERO
    for line in line_pricings:
        eligible = _money(line.line_total) - _money(line.offer_discount)
        if eligible < ZERO:
            eligible = ZERO
        line_eligibles.append(eligible)
        total_eligible += eligible

    if total_eligible <= ZERO:
        return [ZERO for _ in line_pricings]

    # Proportional allocation with remainder distribution
    allocations = []
    remaining = coupon_discount_total

    for index, (line, eligible) in enumerate(zip(line_pricings, line_eligibles)):
        if remaining <= ZERO:
            allocations.append(ZERO)
            continue

        if eligible <= ZERO:
            allocations.append(ZERO)
            continue

        # Last line takes the remainder (deterministic)
        if index == len(line_pricings) - 1:
            allocation = min(remaining, eligible)
        else:
            share = _money(remaining * eligible / total_eligible)
            allocation = min(share, eligible)

        allocation = _money(allocation)
        if allocation > remaining:
            allocation = remaining

        allocations.append(allocation)
        remaining = _money(remaining - allocation)

    # Ensure exact sum: if any remainder remains due to rounding, add it to
    # the last line that has capacity.
    if remaining > ZERO:
        for index in range(len(allocations) - 1, -1, -1):
            if remaining <= ZERO:
                break
            headroom = line_eligibles[index] - allocations[index]
            if headroom > ZERO:
                add_amount = min(remaining, headroom)
                allocations[index] = _money(allocations[index] + add_amount)
                remaining = _money(remaining - add_amount)

    return allocations

