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

from .exceptions import (
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

ZERO = Decimal("0.00")

SOURCE_CART = "cart"
SOURCE_DIRECT = "direct"

STATUS_CREATED = "created"
STATUS_DUPLICATE = "duplicate"
STATUS_REVIEW_REQUIRED = "review_required"

REASON_PRICE_CHANGED = "price_changed"
REASON_CART_CHANGED = "cart_changed"
REASON_ADDRESS_CHANGED = "address_changed"
REASON_SELECTION_CHANGED = "selection_changed"

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


@dataclass(frozen=True)
class CheckoutReview:
    submission_id: str
    address_id: int
    lines: tuple[CheckoutLine, ...]
    subtotal: Decimal
    discount_total: Decimal
    shipping_charge: Decimal
    tax_total: Decimal
    total: Decimal
    token: str
    source: str = SOURCE_CART


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


def _build_lines(state):
    return [
        CheckoutLine(
            cart_item_id=item.pk if item is not None else None,
            variant_id=variant.pk,
            product_name=variant.product.name,
            brand_name=variant.product.brand.name,
            variant_size=variant.size,
            sku=variant.sku,
            quantity=quantity,
            unit_price=variant.price,
            line_total=variant.price * quantity,
        )
        for item, variant, quantity in state
    ]


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
    subtotal = sum((line.line_total for line in lines), ZERO)
    subtotal = subtotal.quantize(Decimal("0.01"))

    return {
        "subtotal": subtotal,
        "discount_total": ZERO,
        "shipping_charge": ZERO,
        "tax_total": ZERO,
        "total": subtotal,
    }


def _lines_payload(lines):
    return [
        [
            line.cart_item_id,
            line.variant_id,
            line.quantity,
            str(line.unit_price),
        ]
        for line in lines
    ]


def _line_identity(entries):
    return [[entry[0], entry[1], entry[2]] for entry in entries]


def _totals_payload(totals):
    return {key: str(value) for key, value in totals.items()}


def _build_review(user, address, cart, state, *, source=SOURCE_CART):
    lines = _build_lines(state)
    totals = _calculate_totals(lines)

    submission_id = uuid.uuid4().hex
    issued_at = int(timezone.now().timestamp())

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
        shipping_charge=totals["shipping_charge"],
        tax_total=totals["tax_total"],
        total=totals["total"],
        token=token,
        source=source,
    )


def review_checkout(user, address_id):
    """Validate the cart/address and return prices plus a signed review token."""
    _assert_active_user(user)

    address = _get_valid_address(user, address_id)

    cart = Cart.objects.filter(user=user).first()
    if cart is None:
        raise EmptyCartError("Your cart is empty.")

    state = _collect_cart_state(cart)

    return _build_review(user, address, cart, state)


def validate_direct_purchase(variant_id, quantity=1):
    """Validate a Buy Now selection without touching the cart."""
    _state, variant = _direct_state(variant_id, quantity)
    return variant


def review_direct_checkout(user, variant_id, quantity, address_id):
    """Validate one selected variant and return a direct checkout review."""
    _assert_active_user(user)

    address = _get_valid_address(user, address_id)
    state, _variant = _direct_state(variant_id, quantity)

    return _build_review(
        user,
        address,
        None,
        state,
        source=SOURCE_DIRECT,
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


def _detect_change(payload, cart, address, lines, totals):
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

    return None


def _detect_direct_change(payload, address, variant, quantity, lines, totals):
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


def _create_order_with_retry(*, user, submission_id, address, totals):
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
                    shipping_charge=totals["shipping_charge"],
                    tax_total=totals["tax_total"],
                    total=totals["total"],
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


def _create_order_items(order, state):
    for variant, quantity in _aggregate_state(state):
        OrderItem.objects.create(
            order=order,
            variant=variant,
            product_name=variant.product.name,
            brand_name=variant.product.brand.name,
            variant_size=variant.size,
            sku=variant.sku,
            unit_price=variant.price,
            quantity=quantity,
            line_total=variant.price * quantity,
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
    lines = _build_lines(state)
    totals = _calculate_totals(lines)

    reason = _detect_change(payload, cart, address, lines, totals)
    if reason is not None:
        review = _build_review(user, address, cart, state)
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
    )

    if duplicate:
        return OrderPlacementResult(status=STATUS_DUPLICATE, order=order)

    _create_order_items(order, state)
    _deduct_stock(state)
    _clear_cart(cart)

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
    lines = _build_lines(state)
    totals = _calculate_totals(lines)

    reason = _detect_direct_change(
        payload,
        address,
        variant,
        quantity,
        lines,
        totals,
    )
    if reason is not None:
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
            reason=reason,
        )

    order, duplicate = _create_order_with_retry(
        user=user,
        submission_id=submission_id,
        address=address,
        totals=totals,
    )

    if duplicate:
        return OrderPlacementResult(status=STATUS_DUPLICATE, order=order)

    _create_order_items(order, state)
    _deduct_stock(state)

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
