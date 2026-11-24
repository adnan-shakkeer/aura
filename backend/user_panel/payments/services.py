"""Server-authoritative Razorpay order, verification and expiry services."""

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from user_panel.orders.cancellation import cancel_order_for_expiry
from user_panel.orders.financials import _item_paid_amount
from user_panel.orders.models import Order

from . import gateway
from .exceptions import (
    PaymentAttemptNotFoundError,
    PaymentConfigurationError,
    PaymentGatewayError,
    PaymentNotAllowedError,
    PaymentVerificationError,
)
from .models import PaymentAttempt, Refund


ZERO = Decimal("0.00")
PAISE = Decimal("100")
ACTIVE_ATTEMPT_STATUSES = (
    PaymentAttempt.Status.CREATED,
    PaymentAttempt.Status.PENDING,
    PaymentAttempt.Status.AUTHORIZED,
)

# Outcomes returned by ``reconcile_retry_attempt``. Only READY and FAILED
# permit a retry to continue; every other state must not create a new
# payment attempt.
RETRY_STATE_READY = "ready"
RETRY_STATE_FAILED = "failed"
RETRY_STATE_CAPTURED = "captured"
RETRY_STATE_IN_PROGRESS = "in_progress"
RETRY_STATE_UNAVAILABLE = "unavailable"


def amount_to_paise(amount):
    value = Decimal(amount).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return int((value * PAISE).to_integral_value(rounding=ROUND_HALF_UP))


def paise_to_amount(paise):
    return (Decimal(int(paise)) / PAISE).quantize(Decimal("0.01"))


def payment_window_expiry(order):
    return order.created_at + timedelta(
        minutes=settings.RAZORPAY_PAYMENT_TIMEOUT_MINUTES
    )


def payment_window_is_open(order):
    return timezone.now() < payment_window_expiry(order)


def captured_attempt_for_order(order):
    return (
        order.payment_attempts.filter(status=PaymentAttempt.Status.CAPTURED)
        .order_by("-captured_at", "-id")
        .first()
    )


def is_order_payable(order):
    if order.payment_method != Order.PaymentMethod.RAZORPAY:
        return False
    if order.status != Order.OrderStatus.PENDING:
        return False
    if order.payment_status not in (
        Order.PaymentStatus.PENDING,
        Order.PaymentStatus.FAILED,
    ):
        return False
    if Decimal(order.total) <= ZERO:
        return False
    if not payment_window_is_open(order):
        return False
    if captured_attempt_for_order(order) is not None:
        return False
    return True


@transaction.atomic
def get_or_create_payment_attempt(order):
    """Return one active attempt for an order, creating it if necessary."""
    locked_order = Order.objects.select_for_update().get(pk=order.pk)

    if not is_order_payable(locked_order):
        raise PaymentNotAllowedError(
            "This order can no longer be paid online. Please review the order."
        )

    now = timezone.now()
    active_attempt = (
        locked_order.payment_attempts.select_for_update()
        .filter(status__in=ACTIVE_ATTEMPT_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )

    if active_attempt is not None:
        if active_attempt.expires_at <= now:
            active_attempt.status = PaymentAttempt.Status.EXPIRED
            active_attempt.save(update_fields=["status", "updated_at"])
        else:
            return active_attempt

    amount_paise = amount_to_paise(locked_order.total)
    if amount_paise < 1:
        raise PaymentNotAllowedError(
            "This order does not require an online payment."
        )

    try:
        with transaction.atomic():
            return PaymentAttempt.objects.create(
                order=locked_order,
                provider=PaymentAttempt.Provider.RAZORPAY,
                status=PaymentAttempt.Status.CREATED,
                amount_paise=amount_paise,
                currency=settings.RAZORPAY_CURRENCY,
                receipt=locked_order.order_number,
                expires_at=payment_window_expiry(locked_order),
            )
    except IntegrityError:
        active_attempt = (
            locked_order.payment_attempts.select_for_update()
            .filter(status__in=ACTIVE_ATTEMPT_STATUSES)
            .order_by("-created_at", "-id")
            .first()
        )
        if active_attempt is not None:
            return active_attempt
        raise


def ensure_provider_order(attempt):
    """Create the Razorpay order outside any database transaction."""
    if attempt.provider_order_id:
        return attempt

    try:
        response = gateway.create_order(
            amount_paise=attempt.amount_paise,
            receipt=attempt.receipt,
            notes={
                "order_number": attempt.order.order_number,
                "payment_attempt_id": str(attempt.pk),
            },
        )
    except PaymentGatewayError as exc:
        PaymentAttempt.objects.filter(
            pk=attempt.pk,
            status=PaymentAttempt.Status.CREATED,
        ).update(
            status=PaymentAttempt.Status.FAILED,
            failure_code="order_creation_failed",
            failure_description=str(exc),
            updated_at=timezone.now(),
        )
        raise

    provider_order_id = (response or {}).get("id")
    if not provider_order_id:
        PaymentAttempt.objects.filter(
            pk=attempt.pk,
            status=PaymentAttempt.Status.CREATED,
        ).update(
            status=PaymentAttempt.Status.FAILED,
            failure_code="order_creation_invalid_response",
            failure_description="Razorpay did not return an order id.",
            updated_at=timezone.now(),
        )
        raise PaymentGatewayError(
            "We could not start the online payment. Please try again."
        )

    PaymentAttempt.objects.filter(pk=attempt.pk).update(
        provider_order_id=provider_order_id,
        status=PaymentAttempt.Status.PENDING,
        raw_order_response=response,
        updated_at=timezone.now(),
    )
    attempt.refresh_from_db()
    return attempt


def prepare_checkout(order):
    """Return ``(attempt, checkout_config)`` with no secrets in the config."""
    attempt = get_or_create_payment_attempt(order)
    attempt = ensure_provider_order(attempt)

    user = getattr(order, "user", None)
    email = (getattr(user, "email", "") or "").strip() if user else ""

    config = {
        "key": settings.RAZORPAY_KEY_ID,
        "amount": attempt.amount_paise,
        "currency": attempt.currency,
        "name": "AURA",
        "description": f"Order {order.order_number}",
        "order_id": attempt.provider_order_id,
        "prefill": {
            "name": order.delivery_full_name,
            "email": email,
            "contact": order.delivery_phone_number,
        },
        "notes": {
            "order_number": order.order_number,
            "payment_attempt_id": str(attempt.pk),
        },
        "theme": {"color": "#1A1A1A"},
    }
    return attempt, config


def _validate_captured_payment(attempt, payment):
    if not isinstance(payment, dict):
        raise PaymentVerificationError("The payment response was invalid.")

    provider_order_id = payment.get("order_id")
    if provider_order_id and provider_order_id != attempt.provider_order_id:
        raise PaymentVerificationError(
            "The payment does not belong to this order."
        )

    try:
        paid_paise = int(payment.get("amount") or 0)
    except (TypeError, ValueError):
        paid_paise = 0

    if paid_paise != int(attempt.amount_paise):
        raise PaymentVerificationError(
            "The paid amount does not match the order total."
        )

    currency = (payment.get("currency") or "").upper()
    if currency and currency != attempt.currency:
        raise PaymentVerificationError(
            "The payment currency does not match the order."
        )

    if (payment.get("status") or "").lower() != "captured":
        raise PaymentVerificationError(
            "The payment has not been captured yet."
        )

    if not payment.get("id"):
        raise PaymentVerificationError(
            "The payment reference is missing."
        )


def record_refund_obligation(
    order,
    amount,
    *,
    kind,
    dedupe_key,
    reason="",
    payment_attempt=None,
):
    """Record one manual refund obligation without performing a refund."""
    amount = Decimal(amount or ZERO).quantize(Decimal("0.01"))
    if amount <= ZERO:
        return None

    amount_paise = amount_to_paise(amount)
    if amount_paise < 1:
        return None

    refund, _created = Refund.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            "order": order,
            "payment_attempt": payment_attempt,
            "kind": kind,
            "status": Refund.Status.PENDING,
            "amount_paise": amount_paise,
            "currency": settings.RAZORPAY_CURRENCY,
            "reason": reason or "",
        },
    )
    return refund


@transaction.atomic
def record_payment_success(attempt_id, payment, *, via):
    """Persist a verified capture exactly once and mark the order paid."""
    attempt = (
        PaymentAttempt.objects.select_for_update()
        .select_related("order")
        .filter(pk=attempt_id)
        .first()
    )
    if attempt is None:
        raise PaymentAttemptNotFoundError("Payment attempt not found.")

    order = Order.objects.select_for_update().get(pk=attempt.order_id)
    payment_id = (payment or {}).get("id") or ""

    if (
        attempt.status == PaymentAttempt.Status.CAPTURED
        and attempt.provider_payment_id == payment_id
    ):
        return order

    _validate_captured_payment(attempt, payment)

    now = timezone.now()
    attempt.status = PaymentAttempt.Status.CAPTURED
    attempt.provider_payment_id = payment_id
    attempt.raw_payment_response = payment
    attempt.captured_at = now
    attempt.captured_via = via
    attempt.failure_code = ""
    attempt.failure_description = ""
    attempt.save(
        update_fields=[
            "status",
            "provider_payment_id",
            "raw_payment_response",
            "captured_at",
            "captured_via",
            "failure_code",
            "failure_description",
            "updated_at",
        ]
    )

    if order.payment_status == Order.PaymentStatus.PAID:
        other_captures = (
            order.payment_attempts.filter(
                status=PaymentAttempt.Status.CAPTURED
            )
            .exclude(pk=attempt.pk)
            .exists()
        )
        if other_captures:
            record_refund_obligation(
                order,
                order.total,
                kind=Refund.Kind.DUPLICATE_PAYMENT,
                dedupe_key=f"order:{order.pk}:duplicate-payment:{attempt.pk}",
                reason="A second successful online payment was captured.",
                payment_attempt=attempt,
            )
        return order

    if order.status != Order.OrderStatus.PENDING:
        # A late capture against an expired or cancelled order must not
        # reopen or mark the order paid. Track the money as a manual refund
        # obligation instead.
        record_refund_obligation(
            order,
            order.total,
            kind=Refund.Kind.LATE_PAYMENT,
            dedupe_key=f"order:{order.pk}:late-payment:{attempt.pk}",
            reason=(
                "Payment was captured after the order had already expired "
                "or was cancelled. The order was not reopened."
            ),
            payment_attempt=attempt,
        )
        return order

    order.payment_status = Order.PaymentStatus.PAID
    order.payment_collected_at = now
    order.payment_collected_amount = order.total
    order.payment_collected_by = None
    order.save(
        update_fields=[
            "payment_status",
            "payment_collected_at",
            "payment_collected_amount",
            "payment_collected_by",
            "updated_at",
        ]
    )

    if order.status == Order.OrderStatus.CANCELLED:
        record_refund_obligation(
            order,
            order.total,
            kind=Refund.Kind.LATE_PAYMENT,
            dedupe_key=f"order:{order.pk}:late-payment:{attempt.pk}",
            reason=(
                "Payment was captured after the order had already expired "
                "or was cancelled. The order was not reopened."
            ),
            payment_attempt=attempt,
        )

    return order


def confirm_checkout_payment(
    order,
    *,
    provider_order_id,
    payment_id,
    signature,
):
    """Verify a browser-returned payment and persist its captured state."""
    attempt = (
        order.payment_attempts.filter(provider_order_id=provider_order_id)
        .order_by("-created_at", "-id")
        .first()
    )
    if attempt is None:
        raise PaymentAttemptNotFoundError(
            "We could not find this online payment."
        )

    gateway.verify_payment_signature(
        provider_order_id=provider_order_id,
        payment_id=payment_id,
        signature=signature,
    )

    payment = gateway.fetch_payment(payment_id)

    if (payment.get("order_id") or "") != provider_order_id:
        raise PaymentVerificationError(
            "The payment does not belong to this order."
        )

    status = (payment.get("status") or "").lower()
    if status == "authorized":
        payment = gateway.capture_payment(
            payment_id,
            attempt.amount_paise,
            attempt.currency,
        )
        status = (payment.get("status") or "").lower()

    if status != "captured":
        raise PaymentVerificationError(
            "The payment has not been captured yet. Please try again."
        )

    return record_payment_success(
        attempt.pk,
        payment,
        via=PaymentAttempt.CapturedVia.CHECKOUT,
    )


@transaction.atomic
def mark_payment_failed(attempt_id, payment):
    attempt = (
        PaymentAttempt.objects.select_for_update()
        .select_related("order")
        .filter(pk=attempt_id)
        .first()
    )
    if attempt is None:
        return None

    if attempt.status == PaymentAttempt.Status.CAPTURED:
        return attempt

    now = timezone.now()
    payment = payment or {}
    attempt.status = PaymentAttempt.Status.FAILED
    if payment.get("id"):
        attempt.provider_payment_id = payment.get("id")
    attempt.failure_code = (
        payment.get("error_code")
        or payment.get("error_reason")
        or "payment_failed"
    )
    attempt.failure_description = (
        payment.get("error_description")
        or payment.get("error_reason")
        or "The payment was not completed."
    )
    attempt.raw_payment_response = payment
    attempt.save(
        update_fields=[
            "status",
            "provider_payment_id",
            "failure_code",
            "failure_description",
            "raw_payment_response",
            "updated_at",
        ]
    )

    order = attempt.order
    if (
        order.payment_status != Order.PaymentStatus.PAID
        and order.status == Order.OrderStatus.PENDING
    ):
        order.payment_status = Order.PaymentStatus.FAILED
        order.save(update_fields=["payment_status", "updated_at"])

    return attempt


@transaction.atomic
def reconcile_payment_with_provider(order):
    """Reconcile a Razorpay order against the provider API.

    Looks up the payment(s) directly from Razorpay using the server-side Key
    ID/Key Secret, then records a captured payment. This is the server-
    authoritative fallback when the browser callback never reaches us.

    Returns the (possibly updated) PaymentAttempt. Raises
    PaymentAttemptNotFoundError when the order has no attempt, and raises
    PaymentGatewayError/PaymentConfigurationError/PaymentVerificationError
    when the provider state cannot be determined safely, so callers can avoid
    destructive actions such as expiring a possibly-paid order.
    """
    attempt = (
        order.payment_attempts.select_for_update()
        .order_by("-created_at", "-id")
        .first()
    )
    if attempt is None:
        raise PaymentAttemptNotFoundError(
            "No payment attempt found for this order."
        )

    if attempt.status == PaymentAttempt.Status.CAPTURED:
        return attempt

    if attempt.provider_payment_id:
        payment = gateway.fetch_payment(attempt.provider_payment_id)
        payments = [payment] if payment else []
    else:
        payments = gateway.fetch_order_payments(attempt.provider_order_id)

    if not payments:
        # No payment exists for this order; nothing was captured.
        return attempt

    def _status(payment):
        return (payment.get("status") or "").lower()

    chosen = None
    for preferred in ("captured", "authorized"):
        chosen = next(
            (p for p in payments if _status(p) == preferred),
            None,
        )
        if chosen is not None:
            break
    if chosen is None:
        chosen = payments[0]

    provider_order_id = chosen.get("order_id") or ""
    if provider_order_id and provider_order_id != attempt.provider_order_id:
        raise PaymentVerificationError(
            "The payment does not belong to this order."
        )

    paid_paise = int(chosen.get("amount") or 0)
    if paid_paise != int(attempt.amount_paise):
        raise PaymentVerificationError(
            "The paid amount does not match the order total."
        )

    currency = (chosen.get("currency") or "").upper()
    if currency and currency != attempt.currency:
        raise PaymentVerificationError(
            "The payment currency does not match the order."
        )

    status = _status(chosen)

    if status == "authorized":
        # Automatic capture is approved: capture the authorized payment.
        chosen = gateway.capture_payment(
            chosen.get("id"),
            attempt.amount_paise,
            attempt.currency,
        )
        status = _status(chosen)

    if status == "captured":
        record_payment_success(
            attempt.pk,
            chosen,
            via=PaymentAttempt.CapturedVia.CHECKOUT,
        )
        attempt.refresh_from_db()
        return attempt

    if status in ("failed", "expired", "refunded"):
        mark_payment_failed(attempt.pk, chosen)
        attempt.refresh_from_db()

    return attempt


@transaction.atomic
def reconcile_retry_attempt(order):
    """Reconcile the latest attempt before a customer starts a retry.

    Returns ``(state, attempt)`` where ``state`` is one of the
    ``RETRY_STATE_*`` constants. A retry may only create/reuse a checkout
    when the state is ``READY`` or ``FAILED``. A provider payment that is
    captured is recorded as success; a payment that is still authorized or
    otherwise in progress blocks a fresh attempt so the customer cannot be
    charged twice. Provider errors leave the pending state untouched.
    """
    captured = captured_attempt_for_order(order)
    if captured is not None:
        return RETRY_STATE_CAPTURED, captured

    attempt = (
        order.payment_attempts.select_for_update()
        .order_by("-created_at", "-id")
        .first()
    )
    if attempt is None:
        return RETRY_STATE_READY, None

    if attempt.status == PaymentAttempt.Status.CAPTURED:
        return RETRY_STATE_CAPTURED, attempt

    if not attempt.provider_order_id:
        # Nothing reached Razorpay yet, so there is no provider state to
        # reconcile. The existing attempt (if active) can be reused safely.
        return RETRY_STATE_READY, attempt

    try:
        # Read every payment on the provider order so a later successful
        # payment is not missed when an earlier one is stored on the attempt.
        payments = gateway.fetch_order_payments(attempt.provider_order_id)
        if not payments and attempt.provider_payment_id:
            payment = gateway.fetch_payment(attempt.provider_payment_id)
            payments = [payment] if payment else []
    except (PaymentConfigurationError, PaymentGatewayError):
        return RETRY_STATE_UNAVAILABLE, attempt

    if not payments:
        if attempt.status == PaymentAttempt.Status.AUTHORIZED:
            # An authorized attempt with no readable payment is uncertain.
            return RETRY_STATE_IN_PROGRESS, attempt
        return RETRY_STATE_READY, attempt

    def _status(payment):
        return (payment.get("status") or "").lower()

    chosen = next(
        (p for p in payments if _status(p) == "captured"),
        None,
    )
    if chosen is None:
        chosen = next(
            (p for p in payments if _status(p) == "authorized"),
            None,
        )
    if chosen is None:
        in_progress = next(
            (
                p
                for p in payments
                if _status(p) in ("created", "pending")
            ),
            None,
        )
        if in_progress is not None:
            return RETRY_STATE_IN_PROGRESS, attempt
        chosen = payments[0]

    provider_order_id = chosen.get("order_id") or ""
    if provider_order_id and provider_order_id != attempt.provider_order_id:
        return RETRY_STATE_UNAVAILABLE, attempt

    try:
        paid_paise = int(chosen.get("amount") or 0)
    except (TypeError, ValueError):
        return RETRY_STATE_UNAVAILABLE, attempt

    if paid_paise != int(attempt.amount_paise):
        return RETRY_STATE_UNAVAILABLE, attempt

    currency = (chosen.get("currency") or "").upper()
    if currency and currency != attempt.currency:
        return RETRY_STATE_UNAVAILABLE, attempt

    status = _status(chosen)

    if status == "authorized":
        try:
            chosen = gateway.capture_payment(
                chosen.get("id"),
                attempt.amount_paise,
                attempt.currency,
            )
        except (PaymentConfigurationError, PaymentGatewayError):
            return RETRY_STATE_IN_PROGRESS, attempt
        status = _status(chosen)

    if status == "captured":
        record_payment_success(
            attempt.pk,
            chosen,
            via=PaymentAttempt.CapturedVia.CHECKOUT,
        )
        attempt.refresh_from_db()
        return RETRY_STATE_CAPTURED, attempt

    if status in ("failed", "expired", "refunded"):
        mark_payment_failed(attempt.pk, chosen)
        attempt.refresh_from_db()
        return RETRY_STATE_FAILED, attempt

    # A provider payment exists but is neither captured nor definitively
    # failed. Never create another attempt while it may still complete.
    return RETRY_STATE_IN_PROGRESS, attempt


@transaction.atomic
def expire_order_payment(order_id):
    """Expire one unpaid Razorpay order and release its reservations once."""
    order = (
        Order.objects.select_for_update()
        .filter(pk=order_id)
        .first()
    )
    if order is None:
        return False

    if order.payment_method != Order.PaymentMethod.RAZORPAY:
        return False
    if order.status != Order.OrderStatus.PENDING:
        return False
    if order.payment_status not in (
        Order.PaymentStatus.PENDING,
        Order.PaymentStatus.FAILED,
    ):
        return False
    if payment_window_is_open(order):
        return False
    if order.payment_attempts.filter(
        status=PaymentAttempt.Status.CAPTURED
    ).exists():
        return False

    # Before releasing reservations, confirm with the provider that no
    # payment was captured (e.g. the browser callback never fired).
    try:
        reconciled = reconcile_payment_with_provider(order)
    except PaymentAttemptNotFoundError:
        reconciled = None
    except (
        PaymentConfigurationError,
        PaymentGatewayError,
        PaymentVerificationError,
    ):
        # Provider state is unknown or suspicious: never expire a possibly
        # paid order. Leave it for a later retry.
        return False

    if reconciled is not None and reconciled.status == (
        PaymentAttempt.Status.CAPTURED
    ):
        return False

    order.refresh_from_db(fields=["status", "payment_status"])
    if order.payment_status == Order.PaymentStatus.PAID:
        return False
    if order.status != Order.OrderStatus.PENDING:
        return False

    cancelled = cancel_order_for_expiry(order)
    if not cancelled:
        return False

    now = timezone.now()
    order.payment_status = Order.PaymentStatus.UNPAID
    order.save(update_fields=["payment_status", "updated_at"])
    order.payment_attempts.filter(
        status__in=ACTIVE_ATTEMPT_STATUSES
    ).update(status=PaymentAttempt.Status.EXPIRED, updated_at=now)
    return True


def expire_unpaid_orders(*, limit=None):
    cutoff = timezone.now() - timedelta(
        minutes=settings.RAZORPAY_PAYMENT_TIMEOUT_MINUTES
    )
    queryset = (
        Order.objects.filter(
            payment_method=Order.PaymentMethod.RAZORPAY,
            status=Order.OrderStatus.PENDING,
            payment_status__in=(
                Order.PaymentStatus.PENDING,
                Order.PaymentStatus.FAILED,
            ),
            created_at__lte=cutoff,
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)
    )
    if limit:
        queryset = queryset[:limit]

    expired = 0
    for order_id in list(queryset):
        if expire_order_payment(order_id):
            expired += 1
    return expired


def expire_overdue_orders_for_user(user, *, limit=None):
    """Expire a customer's overdue unpaid Razorpay orders on a page request.

    Only orders already past the payment window are handed to
    ``expire_order_payment`` so no provider call happens for orders that may
    still be paid. Each order is processed through the existing, locked,
    reconciling expiry path, so repeated or concurrent requests are safe.
    """
    cutoff = timezone.now() - timedelta(
        minutes=settings.RAZORPAY_PAYMENT_TIMEOUT_MINUTES
    )
    queryset = (
        Order.objects.filter(
            user=user,
            payment_method=Order.PaymentMethod.RAZORPAY,
            status=Order.OrderStatus.PENDING,
            payment_status__in=(
                Order.PaymentStatus.PENDING,
                Order.PaymentStatus.FAILED,
            ),
            created_at__lte=cutoff,
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)
    )
    if limit:
        queryset = queryset[:limit]

    expired = 0
    for order_id in list(queryset):
        if expire_order_payment(order_id):
            expired += 1
    return expired


def expire_overdue_order_for_user(user, order_number):
    """Expire one customer's overdue unpaid Razorpay order, if still eligible."""
    cutoff = timezone.now() - timedelta(
        minutes=settings.RAZORPAY_PAYMENT_TIMEOUT_MINUTES
    )
    order_id = (
        Order.objects.filter(
            user=user,
            order_number=order_number,
            payment_method=Order.PaymentMethod.RAZORPAY,
            status=Order.OrderStatus.PENDING,
            payment_status__in=(
                Order.PaymentStatus.PENDING,
                Order.PaymentStatus.FAILED,
            ),
            created_at__lte=cutoff,
        )
        .values_list("id", flat=True)
        .first()
    )
    if order_id is None:
        return False
    return expire_order_payment(order_id)


def refund_amount_for_order_item(item):
    """Return the historical paid amount eligible for a manual refund."""
    return _item_paid_amount(item)
