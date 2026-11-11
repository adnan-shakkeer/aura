"""Checkout review and order-placement services.

Architecture
------------
Cart checkout and direct Buy Now checkout share validation, pricing and
order creation, but never share token state:

``review_checkout(user, address_id)`` / ``place_order(...)``
    Cart checkout validates the current cart, deducts stock and clears the
    cart inside the placement transaction.

``review_direct_checkout(...)`` / ``place_direct_order(...)``
    Buy Now checkout validates one selected variant. Placement locks only
    that variant, creates the order and deducts stock; it never reads,
    locks or clears any cart.

Both review functions return a ``CheckoutReview`` with a signed token
bound to the user and checkout source. Both placement functions return an
``OrderPlacementResult`` so callers can distinguish a created order, a
duplicate submission and a required re-review.

Lock order
----------
Cart checkout: ``Cart``, then ``ProductVariant`` rows in ascending PK
order, then ``CartItem`` rows, then order creation, stock updates and cart
clearing. Direct checkout locks only the selected ``ProductVariant``.

Money is always handled with ``Decimal``; browser-supplied totals are
never accepted as authoritative.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from user_panel.address.models import Address
from user_panel.cart.models import Cart
from user_panel.cart.services import (
    CartServiceError,
    _get_eligible_variant,
    _require_active_user,
    _validate_quantity,
    _validate_stock,
)
from user_panel.orders.coupons import (
    consume_coupon,
    get_coupon_by_code,
    normalize_coupon_code,
    validate_coupon,
)
from user_panel.pricing import services as pricing

from .exceptions import (
    CouponError,
    EmptyCartError,
    ExpiredReviewTokenError,
    InsufficientStockError,
    InvalidAddressError,
    InvalidQuantityError,
    InvalidReviewTokenError,
    OrderServiceError,
    UnexpectedOrderFailure,
    VariantUnavailableError,
)
from .models import Order, OrderItem


REVIEW_TOKEN_SALT = "orders.checkout.review"
DIRECT_REVIEW_TOKEN_SALT = "orders.checkout.direct"
REVIEW_TOKEN_MAX_AGE = 30 * 60

ORDER_NUMBER_MAX_ATTEMPTS = 5

SOURCE_CART = "cart"
SOURCE_DIRECT = "direct"

STATUS_CREATED = "created"
STATUS_DUPLICATE = "duplicate"
STATUS_REVIEW_REQUIRED = "review_required"

REASON_PRICE_CHANGED = "price_changed"
REASON_CART_CHANGED = "cart_changed"
REASON_ADDRESS_CHANGED = "address_changed"
REASON_SELECTION_CHANGED = "selection_changed"
REASON_COUPON_INVALID = "coupon_invalid"

ZERO = pricing.ZERO

_DIRECT_INVALID_TOKEN_MESSAGE = (
    "This Buy Now review is not valid. Please review your selection again."
)
_DIRECT_EXPIRED_TOKEN_MESSAGE = (
    "Your Buy Now session has expired. Please review your selection again."
)

_REQUIRED_ADDRESS_FIELDS = (
    "full_name",
    "phone_number",
    "address_line1",
    "city",
    "state",
    "pincode",
)


@dataclass(frozen=True)
class CheckoutLine:
    cart_item_id: int | None
    variant_id: int
    product_name: str
    brand_name: str
    variant_size: str
    sku: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    offer_source: str
    offer_percentage: Decimal
    offer_discount: Decimal
    coupon_discount: Decimal
    paid_amount: Decimal


@dataclass(frozen=True)
class CheckoutReview:
    submission_id: str
    address_id: int
    lines: tuple[CheckoutLine, ...]
    subtotal: Decimal
    discount_total: Decimal
    offer_discount_total: Decimal
    coupon_discount_total: Decimal
    shipping_charge: Decimal
    tax_total: Decimal
    total: Decimal
    token: str
    source: str = SOURCE_CART
    coupon_code: str = ""


@dataclass(frozen=True)
class OrderPlacementResult:
    status: str
    order: Order | None = None
    review: CheckoutReview | None = None
    reason: str | None = None
    recovered: bool = False


def _assert_active_user(user):
    try:
        _require_active_user(user)
    except CartServiceError as exc:
        raise OrderServiceError(str(exc), code="invalid_user") from None


def _get_valid_address(user, address_id):
    if isinstance(address_id, bool):
        raise InvalidAddressError("Please select a valid delivery address.")

    try:
        address_pk = int(address_id)
    except (TypeError, ValueError):
        raise InvalidAddressError(
            "Please select a valid delivery address."
        ) from None

    if address_pk < 1:
        raise InvalidAddressError("Please select a valid delivery address.")

    address = Address.objects.filter(
        pk=address_pk,
        user=user,
        is_deleted=False,
    ).first()

    if address is None:
        raise InvalidAddressError(
            "Please select a valid delivery address."
        )

    if any(
        not (getattr(address, field) or "").strip()
        for field in _REQUIRED_ADDRESS_FIELDS
    ):
        raise InvalidAddressError(
            "The selected delivery address is incomplete."
        )

    if address.address_type not in dict(Address.ADDRESS_TYPE_CHOICES):
        raise InvalidAddressError(
            "The selected delivery address is invalid."
        )

    return address


def _address_fingerprint(address):
    return [
        address.full_name,
        address.phone_number,
        address.address_line1,
        address.address_line2 or "",
        address.landmark or "",
        address.city,
        address.state,
        address.pincode,
        address.address_type,
    ]


def _collect_cart_state(cart, *, lock=False):
    """Return ``(cart_item, variant, quantity)`` tuples in lock-safe order.

    Items are ordered by variant primary key so variant rows are locked in
    ascending order. Eligibility, quantity and stock rules are delegated to
    the canonical cart helpers so they are never re-implemented here.
    """
    items = list(
        cart.items.select_related(
            "variant__product__brand__category"
        ).order_by("variant_id", "id")
    )

    if not items:
        raise EmptyCartError("Your cart is empty.")

    state = []

    for item in items:
        try:
            quantity = _validate_quantity(item.quantity)
        except CartServiceError as exc:
            raise InvalidQuantityError(str(exc)) from None

        try:
            variant = _get_eligible_variant(item.variant_id, lock=lock)
        except CartServiceError as exc:
            raise VariantUnavailableError(str(exc)) from None

        try:
            _validate_stock(variant, quantity)
        except CartServiceError as exc:
            raise InsufficientStockError(str(exc)) from None

        state.append((item, variant, quantity))

    if lock:
        list(cart.items.select_for_update().values_list("pk", flat=True))

    return state


def _state_eligible_merchandise(state):
    """Return the eligible post-offer merchandise value for checkout state."""
    line_pricings = [
        pricing.price_variant(variant, quantity)
        for _item, variant, quantity in state
    ]
    return pricing.eligible_merchandise_after_offers(line_pricings)


def _build_lines(state, coupon=None, eligible_merchandise=None):
    """Build checkout lines from state, with optional coupon allocation.

    When a coupon is provided, the coupon discount is calculated against the
    eligible post-offer merchandise and allocated deterministically across
    lines. Each line's paid_amount reflects both offer and coupon discounts.
    """
    lines = []

    line_pricings = []
    for item, variant, quantity in state:
        line_pricing = pricing.price_variant(variant, quantity)
        line_pricings.append(line_pricing)

    # Calculate per-line coupon allocations
    coupon_discount_total = ZERO
    coupon_allocations = []
    if coupon is not None:
        if eligible_merchandise is None:
            eligible_merchandise = pricing.eligible_merchandise_after_offers(
                line_pricings
            )
        coupon_discount_total = pricing.calculate_coupon_discount(
            discount_type=coupon.discount_type,
            discount_value=coupon.discount_value,
            max_discount_amount=coupon.max_discount_amount,
            eligible_merchandise=eligible_merchandise,
        )
        coupon_allocations = pricing.allocate_coupon_discount(
            coupon_discount_total, line_pricings
        )

    for index, (item, variant, quantity) in enumerate(state):
        line_pricing = line_pricings[index]
        coupon_allocation = (
            coupon_allocations[index] if index < len(coupon_allocations) else ZERO
        )
        paid_amount = pricing._money(
            line_pricing.line_total
            - line_pricing.offer_discount
            - coupon_allocation
        )
        if paid_amount < ZERO:
            paid_amount = ZERO

        lines.append(
            CheckoutLine(
                cart_item_id=item.pk if item is not None else None,
                variant_id=variant.pk,
                product_name=variant.product.name,
                brand_name=variant.product.brand.name,
                variant_size=variant.size,
                sku=variant.sku,
                quantity=quantity,
                unit_price=line_pricing.unit_price,
                line_total=line_pricing.line_total,
                offer_source=line_pricing.offer_source,
                offer_percentage=line_pricing.offer_percentage,
                offer_discount=line_pricing.offer_discount,
                coupon_discount=coupon_allocation,
                paid_amount=paid_amount,
            )
        )

    return lines, coupon_discount_total


def _direct_state(variant_id, quantity, *, lock=False):
    """Validate a single direct purchase and return its checkout state."""
    try:
        quantity = _validate_quantity(quantity)
    except CartServiceError as exc:
        raise InvalidQuantityError(str(exc)) from None

    try:
        variant = _get_eligible_variant(variant_id, lock=lock)
    except CartServiceError as exc:
        raise VariantUnavailableError(str(exc)) from None

    try:
        _validate_stock(variant, quantity)
    except CartServiceError as exc:
        raise InsufficientStockError(str(exc)) from None

    return [(None, variant, quantity)], variant


def _calculate_totals(lines):
    return pricing.summarize(lines)


def _lines_payload(lines):
    return [
        [
            line.cart_item_id,
            line.variant_id,
            line.quantity,
            str(line.unit_price),
            line.offer_source,
            str(line.offer_percentage),
            str(line.offer_discount),
            str(line.coupon_discount),
            str(line.paid_amount),
        ]
        for line in lines
    ]


def _line_identity(entries):
    return [[entry[0], entry[1], entry[2]] for entry in entries]


def _totals_payload(totals):
    return {key: str(value) for key, value in totals.items()}


def _coupon_state_expected(coupon, lines, totals):
    """Build the current authoritative coupon state for token comparison."""
    if coupon is None:
        return {
            "ccode": "",
            "ccid": None,
            "ctype": "",
            "cvalue": "",
            "cmax": "",
            "cmin": "0.00",
            "cdiscount": str(totals["coupon_discount_total"]),
            "callocs": [str(line.coupon_discount) for line in lines],
        }

    return {
        "ccode": coupon.code,
        "ccid": coupon.pk,
        "ctype": coupon.discount_type,
        "cvalue": str(coupon.discount_value),
        "cmax": (
            ""
            if coupon.max_discount_amount is None
            else str(coupon.max_discount_amount)
        ),
        "cmin": str(coupon.minimum_purchase),
        "cdiscount": str(totals["coupon_discount_total"]),
        "callocs": [str(line.coupon_discount) for line in lines],
    }


def _coupon_allocations_match(current, expected):
    if current is None:
        # Legacy tokens carry no allocations: tolerate an all-zero snapshot.
        return all(part == "0.00" for part in expected)
    if not isinstance(current, list):
        return False
    return list(current) == list(expected)


def _build_review(user, address, cart, state, *, source=SOURCE_CART, coupon=None):
    """Build a checkout review with optional coupon applied.

    ``coupon`` is a validated ``admin_panel.coupons.Coupon`` or ``None``.
    """
    lines, coupon_discount_total = _build_lines(state, coupon=coupon)

    totals = _calculate_totals(lines)

    submission_id = uuid.uuid4().hex
    issued_at = int(timezone.now().timestamp())

    coupon_expectations = _coupon_state_expected(coupon, lines, totals)

    payload = {
        "sid": submission_id,
        "uid": user.pk,
        "src": source,
        "aid": address.pk,
        "address": _address_fingerprint(address),
        "items": _lines_payload(lines),
        "totals": _totals_payload(totals),
        "iat": issued_at,
        "exp": issued_at + REVIEW_TOKEN_MAX_AGE,
        # Coupon fields for change detection at placement
        "ccode": coupon_expectations["ccode"],
        "ccid": coupon_expectations["ccid"],
        "ctype": coupon_expectations["ctype"],
        "cvalue": coupon_expectations["cvalue"],
        "cmax": coupon_expectations["cmax"],
        "cmin": coupon_expectations["cmin"],
        "cdiscount": coupon_expectations["cdiscount"],
        "callocs": coupon_expectations["callocs"],
    }

    if source == SOURCE_DIRECT:
        payload["vid"] = lines[0].variant_id
        payload["qty"] = lines[0].quantity
        token_salt = DIRECT_REVIEW_TOKEN_SALT
    else:
        payload["cid"] = cart.pk
        token_salt = REVIEW_TOKEN_SALT

    token = signing.dumps(
        payload,
        salt=token_salt,
        compress=True,
    )

    return CheckoutReview(
        submission_id=submission_id,
        address_id=address.pk,
        lines=tuple(lines),
        subtotal=totals["subtotal"],
        discount_total=totals["discount_total"],
        offer_discount_total=totals["offer_discount_total"],
        coupon_discount_total=totals["coupon_discount_total"],
        shipping_charge=totals["shipping_charge"],
        tax_total=totals["tax_total"],
        total=totals["total"],
        token=token,
        source=source,
        coupon_code=coupon.code if coupon is not None else "",
    )


def _resolve_checkout_coupon(user, coupon_code, state):
    """Resolve and validate the coupon code for a review, or return None.

    An invalid coupon is silently ignored during review (it was already
    validated when the customer applied it); placement revalidation still
    guards the authoritative outcome. No browser-supplied value is trusted.
    """
    normalized = normalize_coupon_code(coupon_code or "")
    if not normalized:
        return None

    coupon = get_coupon_by_code(normalized)
    if coupon is None:
        return None

    eligible_merchandise = _state_eligible_merchandise(state)

    try:
        validate_coupon(coupon, user, eligible_merchandise)
    except CouponError:
        return None

    return coupon


def review_checkout(user, address_id, coupon_code=None):
    """Validate the cart/address and return prices plus a signed review token."""
    _assert_active_user(user)

    address = _get_valid_address(user, address_id)

    cart = Cart.objects.filter(user=user).first()
    if cart is None:
        raise EmptyCartError("Your cart is empty.")

    state = _collect_cart_state(cart)

    coupon = _resolve_checkout_coupon(user, coupon_code, state)

    return _build_review(user, address, cart, state, coupon=coupon)


def eligible_merchandise_for_cart(user):
    """Return the eligible post-offer merchandise value for a customer's cart."""
    _assert_active_user(user)

    cart = Cart.objects.filter(user=user).first()
    if cart is None:
        raise EmptyCartError("Your cart is empty.")

    state = _collect_cart_state(cart)
    return _state_eligible_merchandise(state)


def eligible_merchandise_for_purchase(variant_id, quantity):
    """Return the eligible post-offer merchandise value for a direct purchase."""
    state, _variant = _direct_state(variant_id, quantity)
    return _state_eligible_merchandise(state)


def validate_direct_purchase(variant_id, quantity=1):
    """Validate a Buy Now selection without touching the cart."""
    _state, variant = _direct_state(variant_id, quantity)
    return variant


def review_direct_checkout(user, variant_id, quantity, address_id, coupon_code=None):
    """Validate one selected variant and return a direct checkout review."""
    _assert_active_user(user)

    address = _get_valid_address(user, address_id)
    state, _variant = _direct_state(variant_id, quantity)

    coupon = _resolve_checkout_coupon(user, coupon_code, state)

    return _build_review(
        user,
        address,
        None,
        state,
        source=SOURCE_DIRECT,
        coupon=coupon,
    )


def _decode_review_token(
    review_token,
    *,
    salt=REVIEW_TOKEN_SALT,
    invalid_message=(
        "This checkout review is not valid. Please review your cart again."
    ),
):
    """Return ``(payload, expired)``; signature is always verified."""
    if not isinstance(review_token, str) or not review_token:
        raise InvalidReviewTokenError(invalid_message)

    try:
        payload = signing.loads(
            review_token,
            salt=salt,
            max_age=REVIEW_TOKEN_MAX_AGE,
        )
        expired = False
    except signing.SignatureExpired:
        try:
            payload = signing.loads(review_token, salt=salt)
        except signing.BadSignature:
            raise InvalidReviewTokenError(invalid_message) from None
        expired = True
    except (signing.BadSignature, TypeError, ValueError):
        raise InvalidReviewTokenError(invalid_message) from None

    if not isinstance(payload, dict):
        raise InvalidReviewTokenError(invalid_message)

    return payload, expired


def _token_source(payload):
    """Return the token source; pre-source cart tokens remain compatible."""
    return payload.get("src", SOURCE_CART)


def _assert_token_identity(
    payload,
    user,
    *,
    invalid_message=(
        "This checkout review is not valid. Please review your cart again."
    ),
):
    if payload.get("uid") != user.pk:
        raise InvalidReviewTokenError(
            "This checkout review is not valid for your account."
        )

    submission_id = payload.get("sid")
    max_length = Order._meta.get_field("idempotency_key").max_length

    if (
        not isinstance(submission_id, str)
        or not submission_id
        or len(submission_id) > max_length
    ):
        raise InvalidReviewTokenError(invalid_message)


def _find_existing_order(submission_id, user):
    order = Order.objects.filter(idempotency_key=submission_id).first()

    if order is None:
        return None

    if order.user_id != user.pk:
        raise InvalidReviewTokenError(
            "This checkout review is not valid for your account."
        )

    return order


def _coupon_values_agree(payload, expected):
    """Compare the signed coupon snapshot against the current coupon state.

    One of the two arguments must be ``None``. Keys absent from the signed
    payload fall back to coupon-less defaults so pre-coupon tokens stay
    compatible and require at most one re-review.
    """
    defaults = {
        "ccode": "",
        "ccid": None,
        "ctype": "",
        "cvalue": "",
        "cmax": "",
        "cmin": "0.00",
        "cdiscount": "0.00",
    }

    for key, value in expected.items():
        current = payload.get(key)
        if key == "callocs":
            if not _coupon_allocations_match(current, value):
                return False
            continue
        if current is None:
            current = defaults[key]
        if current != value:
            return False
    return True


def _detect_change(payload, cart, address, lines, totals, coupon):
    """Return the reason a previously reviewed checkout no longer matches."""
    if payload.get("cid") != cart.pk:
        return REASON_CART_CHANGED

    if payload.get("aid") != address.pk:
        return REASON_ADDRESS_CHANGED

    if payload.get("address") != _address_fingerprint(address):
        return REASON_ADDRESS_CHANGED

    current_items = _lines_payload(lines)
    token_items = payload.get("items") or []

    if _line_identity(token_items) != _line_identity(current_items):
        return REASON_CART_CHANGED

    if token_items != current_items:
        return REASON_PRICE_CHANGED

    if payload.get("totals") != _totals_payload(totals):
        return REASON_PRICE_CHANGED

    expected_coupon = _coupon_state_expected(coupon, lines, totals)
    if not _coupon_values_agree(payload, expected_coupon):
        return REASON_PRICE_CHANGED

    return None


def _detect_direct_change(payload, address, variant, quantity, lines, totals, coupon):
    """Return the reason a previously reviewed Buy Now selection changed."""
    if (
        payload.get("vid") != variant.pk
        or payload.get("qty") != quantity
    ):
        return REASON_SELECTION_CHANGED

    if payload.get("aid") != address.pk:
        return REASON_ADDRESS_CHANGED

    if payload.get("address") != _address_fingerprint(address):
        return REASON_ADDRESS_CHANGED

    current_items = _lines_payload(lines)
    token_items = payload.get("items") or []

    if _line_identity(token_items) != _line_identity(current_items):
        return REASON_SELECTION_CHANGED

    if token_items != current_items:
        return REASON_PRICE_CHANGED

    if payload.get("totals") != _totals_payload(totals):
        return REASON_PRICE_CHANGED

    expected_coupon = _coupon_state_expected(coupon, lines, totals)
    if not _coupon_values_agree(payload, expected_coupon):
        return REASON_PRICE_CHANGED

    return None


def _aggregate_state(state):
    """Collapse any duplicate variants before creating items/deducting stock."""
    aggregated = {}

    for _item, variant, quantity in state:
        if variant.pk in aggregated:
            aggregated[variant.pk][1] += quantity
        else:
            aggregated[variant.pk] = [variant, quantity]

    return [(variant, quantity) for variant, quantity in aggregated.values()]


def _integrity_constraint(exc):
    for candidate in (exc.__cause__, exc.__context__, exc):
        diag = getattr(candidate, "diag", None)
        name = getattr(diag, "constraint_name", None)
        if name:
            return name

    text = str(exc)

    if "unique_order_idempotency_key" in text:
        return "unique_order_idempotency_key"

    if "order_number" in text:
        return "orders_order_order_number_key"

    return None


def _is_order_number_conflict(constraint):
    return bool(constraint) and "order_number" in constraint


def _create_order_with_retry(*, user, submission_id, address, totals, coupon_code=""):
    """Create the order, retrying a bounded number of times on number clash.

    Returns ``(order, is_duplicate)``. Each attempt runs inside a savepoint
    so a failed INSERT never leaves the outer transaction broken.
    """
    last_error = None

    for _attempt in range(ORDER_NUMBER_MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                order = Order.objects.create(
                    user=user,
                    idempotency_key=submission_id,
                    status=Order.OrderStatus.PENDING,
                    payment_method=Order.PaymentMethod.COD,
                    payment_status=Order.PaymentStatus.UNPAID,
                    subtotal=totals["subtotal"],
                    discount_total=totals["discount_total"],
                    offer_discount_total=totals["offer_discount_total"],
                    coupon_discount_total=totals["coupon_discount_total"],
                    shipping_charge=totals["shipping_charge"],
                    tax_total=totals["tax_total"],
                    total=totals["total"],
                    coupon_code=coupon_code,
                    delivery_full_name=address.full_name,
                    delivery_phone_number=address.phone_number,
                    delivery_address_line1=address.address_line1,
                    delivery_address_line2=address.address_line2 or "",
                    delivery_landmark=address.landmark or "",
                    delivery_city=address.city,
                    delivery_state=address.state,
                    delivery_pincode=address.pincode,
                    delivery_address_type=address.address_type,
                )
            return order, False
        except IntegrityError as exc:
            constraint = _integrity_constraint(exc)

            if constraint == "unique_order_idempotency_key":
                existing = _find_existing_order(submission_id, user)
                if existing is not None:
                    return existing, True
                raise

            if _is_order_number_conflict(constraint):
                last_error = exc
                continue

            raise

    raise UnexpectedOrderFailure(
        "We could not allocate an order number. Please try again."
    ) from last_error


def _create_order_items(order, state, lines):
    """Persist line snapshots using the already reviewed prices.

    Order items aggregate duplicate cart rows for the same variant, so their
    monetary snapshots are summed from the reviewed checkout lines. This
    guarantees the persisted order total always equals the sum of the item
    paid amounts, even when per-line rounding differs.
    """
    lines_by_variant = {}
    for line in lines:
        lines_by_variant.setdefault(line.variant_id, []).append(line)

    for variant, quantity in _aggregate_state(state):
        variant_lines = lines_by_variant[variant.pk]
        first_line = variant_lines[0]
        line_total = sum(
            (line.line_total for line in variant_lines),
            pricing.ZERO,
        )
        offer_discount = sum(
            (line.offer_discount for line in variant_lines),
            pricing.ZERO,
        )
        coupon_discount = sum(
            (line.coupon_discount for line in variant_lines),
            pricing.ZERO,
        )
        paid_amount = sum(
            (line.paid_amount for line in variant_lines),
            pricing.ZERO,
        )

        OrderItem.objects.create(
            order=order,
            variant=variant,
            product_name=variant.product.name,
            brand_name=variant.product.brand.name,
            variant_size=variant.size,
            sku=variant.sku,
            unit_price=first_line.unit_price,
            quantity=quantity,
            line_total=line_total,
            offer_source=first_line.offer_source,
            offer_percentage_applied=first_line.offer_percentage,
            offer_discount_allocated=offer_discount,
            coupon_discount_allocated=coupon_discount,
            discount_allocated=offer_discount + coupon_discount,
            paid_amount=paid_amount,
        )


def _deduct_stock(state):
    for variant, quantity in _aggregate_state(state):
        variant.stock -= quantity
        variant.save(update_fields=["stock", "updated_at"])


def _clear_cart(cart):
    cart.items.all().delete()
    cart.save(update_fields=["updated_at"])


@transaction.atomic
def _place_order_atomic(user, address_id, review_token):
    _assert_active_user(user)

    payload, expired = _decode_review_token(review_token)
    _assert_token_identity(payload, user)

    if _token_source(payload) != SOURCE_CART:
        raise InvalidReviewTokenError(
            "This checkout review is not valid. Please review your bag again."
        )

    submission_id = payload["sid"]

    existing = _find_existing_order(submission_id, user)
    if existing is not None:
        return OrderPlacementResult(
            status=STATUS_DUPLICATE,
            order=existing,
            recovered=expired,
        )

    if expired:
        raise ExpiredReviewTokenError(
            "Your checkout session has expired. "
            "Please review your cart again."
        )

    address = _get_valid_address(user, address_id)

    cart = Cart.objects.select_for_update().filter(user=user).first()

    existing = _find_existing_order(submission_id, user)
    if existing is not None:
        return OrderPlacementResult(status=STATUS_DUPLICATE, order=existing)

    if cart is None:
        raise EmptyCartError("Your cart is empty.")

    state = _collect_cart_state(cart, lock=True)

    # Extract coupon from the signed token for revalidation.
    token_coupon_code = payload.get("ccode", "")
    coupon = None
    if token_coupon_code:
        coupon = get_coupon_by_code(token_coupon_code)

    if coupon is not None:
        # Revalidate the coupon against current state.
        eligible_merchandise = _state_eligible_merchandise(state)
        try:
            validate_coupon(coupon, user, eligible_merchandise)
        except CouponError:
            # The coupon became invalid: require a fresh review without it.
            review = _build_review(user, address, cart, state)
            return OrderPlacementResult(
                status=STATUS_REVIEW_REQUIRED,
                review=review,
                reason=REASON_COUPON_INVALID,
            )

    lines, _coupon_discount_total = _build_lines(state, coupon=coupon)
    totals = _calculate_totals(lines)

    reason = _detect_change(payload, cart, address, lines, totals, coupon)
    if reason is not None:
        review = _build_review(
            user, address, cart, state, coupon=coupon
        )
        return OrderPlacementResult(
            status=STATUS_REVIEW_REQUIRED,
            review=review,
            reason=reason,
        )

    order, duplicate = _create_order_with_retry(
        user=user,
        submission_id=submission_id,
        address=address,
        totals=totals,
        coupon_code=coupon.code if coupon is not None else "",
    )

    if duplicate:
        return OrderPlacementResult(status=STATUS_DUPLICATE, order=order)

    _create_order_items(order, state, lines)
    _deduct_stock(state)
    _clear_cart(cart)

    # Consume the coupon for COD placement.
    if coupon is not None:
        consume_coupon(
            coupon=coupon,
            order=order,
            user=user,
            discount_amount=totals["coupon_discount_total"],
        )

    return OrderPlacementResult(status=STATUS_CREATED, order=order)


def place_order(user, address_id, review_token):
    """Convert a reviewed cart into a COD order.

    Raises ``OrderServiceError`` subclasses for expected failures; anything
    else is wrapped as ``UnexpectedOrderFailure`` after the transaction has
    rolled back.
    """
    try:
        return _place_order_atomic(user, address_id, review_token)
    except OrderServiceError:
        raise
    except Exception as exc:
        raise UnexpectedOrderFailure(
            "We could not place your order. Please try again."
        ) from exc


@transaction.atomic
def _place_direct_order_atomic(user, address_id, review_token):
    _assert_active_user(user)

    payload, expired = _decode_review_token(
        review_token,
        salt=DIRECT_REVIEW_TOKEN_SALT,
        invalid_message=_DIRECT_INVALID_TOKEN_MESSAGE,
    )
    _assert_token_identity(
        payload,
        user,
        invalid_message=_DIRECT_INVALID_TOKEN_MESSAGE,
    )

    if _token_source(payload) != SOURCE_DIRECT:
        raise InvalidReviewTokenError(_DIRECT_INVALID_TOKEN_MESSAGE)

    submission_id = payload["sid"]

    existing = _find_existing_order(submission_id, user)
    if existing is not None:
        return OrderPlacementResult(
            status=STATUS_DUPLICATE,
            order=existing,
            recovered=expired,
        )

    if expired:
        raise ExpiredReviewTokenError(_DIRECT_EXPIRED_TOKEN_MESSAGE)

    address = _get_valid_address(user, address_id)

    variant_id = payload.get("vid")
    quantity = payload.get("qty")

    if (
        not isinstance(variant_id, int)
        or isinstance(variant_id, bool)
        or variant_id < 1
        or not isinstance(quantity, int)
        or isinstance(quantity, bool)
    ):
        raise InvalidReviewTokenError(_DIRECT_INVALID_TOKEN_MESSAGE)

    state, variant = _direct_state(
        variant_id,
        quantity,
        lock=True,
    )

    # Extract coupon from the signed token for revalidation.
    token_coupon_code = payload.get("ccode", "")
    coupon = None
    if token_coupon_code:
        coupon = get_coupon_by_code(token_coupon_code)

    if coupon is not None:
        # Revalidate the coupon against current state.
        eligible_merchandise = _state_eligible_merchandise(state)
        try:
            validate_coupon(coupon, user, eligible_merchandise)
        except CouponError:
            # The coupon became invalid: require a fresh review without it.
            review = _build_review(
                user,
                address,
                None,
                state,
                source=SOURCE_DIRECT,
            )
            return OrderPlacementResult(
                status=STATUS_REVIEW_REQUIRED,
                review=review,
                reason=REASON_COUPON_INVALID,
            )

    lines, _coupon_discount_total = _build_lines(state, coupon=coupon)
    totals = _calculate_totals(lines)

    reason = _detect_direct_change(
        payload,
        address,
        variant,
        quantity,
        lines,
        totals,
        coupon,
    )
    if reason is not None:
        review = _build_review(
            user,
            address,
            None,
            state,
            source=SOURCE_DIRECT,
            coupon=coupon,
        )
        return OrderPlacementResult(
            status=STATUS_REVIEW_REQUIRED,
            review=review,
            reason=reason,
        )

    order, duplicate = _create_order_with_retry(
        user=user,
        submission_id=submission_id,
        address=address,
        totals=totals,
        coupon_code=coupon.code if coupon is not None else "",
    )

    if duplicate:
        return OrderPlacementResult(status=STATUS_DUPLICATE, order=order)

    _create_order_items(order, state, lines)
    _deduct_stock(state)

    # Consume the coupon for COD placement.
    if coupon is not None:
        consume_coupon(
            coupon=coupon,
            order=order,
            user=user,
            discount_amount=totals["coupon_discount_total"],
        )

    return OrderPlacementResult(status=STATUS_CREATED, order=order)


def place_direct_order(user, address_id, review_token):
    """Convert one Buy Now selection into a COD order without touching carts."""
    try:
        return _place_direct_order_atomic(user, address_id, review_token)
    except OrderServiceError:
        raise
    except Exception as exc:
        raise UnexpectedOrderFailure(
            "We could not place your order. Please try again."
        ) from exc
