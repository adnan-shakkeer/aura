"""Checkout review and order-placement services.

Architecture
------------
Two public operations are exposed:

``review_checkout(user, address_id)``
    Read-only validation and price calculation. It validates the cart,
    the delivery address and stock using the canonical cart rules, then
    returns a ``CheckoutReview`` containing the calculated lines/totals
    and a signed review token.

``place_order(user, address_id, review_token)``
    Re-validates everything inside a single database transaction and
    converts the cart into an order, deducts stock and clears the cart.
    It returns an ``OrderPlacementResult`` so callers can distinguish a
    created order, a duplicate submission and a required re-review.

Lock order (matching ``cart.services`` to avoid deadlocks)
-----------------------------------------------------------
1. ``Cart`` row (``select_for_update``).
2. ``ProductVariant`` rows in ascending primary-key order.
3. ``CartItem`` rows.
4. Order / OrderItem creation.
5. Stock updates.
6. Cart clearing.

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

from .models import Order, OrderItem


REVIEW_TOKEN_SALT = "orders.checkout.review"
REVIEW_TOKEN_MAX_AGE = 30 * 60

ORDER_NUMBER_MAX_ATTEMPTS = 5

ZERO = Decimal("0.00")

STATUS_CREATED = "created"
STATUS_DUPLICATE = "duplicate"
STATUS_REVIEW_REQUIRED = "review_required"

REASON_PRICE_CHANGED = "price_changed"
REASON_CART_CHANGED = "cart_changed"
REASON_ADDRESS_CHANGED = "address_changed"

_REQUIRED_ADDRESS_FIELDS = (
    "full_name",
    "phone_number",
    "address_line1",
    "city",
    "state",
    "pincode",
)


class OrderServiceError(Exception):
    """Raised when a checkout/order operation violates a business rule."""

    code = "order_error"

    def __init__(self, message=None, *, code=None):
        if code is not None:
            self.code = code
        super().__init__(message or "We could not process your order.")


class EmptyCartError(OrderServiceError):
    code = "empty_cart"


class VariantUnavailableError(OrderServiceError):
    code = "variant_unavailable"


class InsufficientStockError(OrderServiceError):
    code = "insufficient_stock"


class InvalidQuantityError(OrderServiceError):
    code = "invalid_quantity"


class InvalidAddressError(OrderServiceError):
    code = "invalid_address"


class InvalidReviewTokenError(OrderServiceError):
    code = "invalid_review_token"


class ExpiredReviewTokenError(OrderServiceError):
    code = "expired_review_token"


class UnexpectedOrderFailure(OrderServiceError):
    code = "unexpected_failure"


@dataclass(frozen=True)
class CheckoutLine:
    cart_item_id: int
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
            cart_item_id=item.pk,
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


def _build_review(user, address, cart, state):
    lines = _build_lines(state)
    totals = _calculate_totals(lines)

    submission_id = uuid.uuid4().hex
    issued_at = int(timezone.now().timestamp())

    payload = {
        "sid": submission_id,
        "uid": user.pk,
        "cid": cart.pk,
        "aid": address.pk,
        "address": _address_fingerprint(address),
        "items": _lines_payload(lines),
        "totals": _totals_payload(totals),
        "iat": issued_at,
        "exp": issued_at + REVIEW_TOKEN_MAX_AGE,
    }

    token = signing.dumps(
        payload,
        salt=REVIEW_TOKEN_SALT,
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


def _decode_review_token(review_token):
    """Return ``(payload, expired)``; signature is always verified."""
    if not isinstance(review_token, str) or not review_token:
        raise InvalidReviewTokenError(
            "This checkout review is not valid. Please review your cart again."
        )

    try:
        payload = signing.loads(
            review_token,
            salt=REVIEW_TOKEN_SALT,
            max_age=REVIEW_TOKEN_MAX_AGE,
        )
        expired = False
    except signing.SignatureExpired:
        try:
            payload = signing.loads(review_token, salt=REVIEW_TOKEN_SALT)
        except signing.BadSignature:
            raise InvalidReviewTokenError(
                "This checkout review is not valid. Please review your cart again."
            ) from None
        expired = True
    except (signing.BadSignature, TypeError, ValueError):
        raise InvalidReviewTokenError(
            "This checkout review is not valid. Please review your cart again."
        ) from None

    if not isinstance(payload, dict):
        raise InvalidReviewTokenError(
            "This checkout review is not valid. Please review your cart again."
        )

    return payload, expired


def _assert_token_identity(payload, user):
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
        raise InvalidReviewTokenError(
            "This checkout review is not valid. Please review your cart again."
        )


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
