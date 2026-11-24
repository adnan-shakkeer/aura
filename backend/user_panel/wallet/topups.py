"""Razorpay wallet top-up services.

A top-up is a standalone financial operation: it never creates an Order or
OrderItem. Wallet credit happens only after the server verifies the payment
signature, fetches the payment from Razorpay and confirms a captured payment
for the exact top-up amount and currency.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from user_panel.payments import gateway
from user_panel.payments.exceptions import PaymentError, PaymentGatewayError

from .exceptions import (
    WalletError,
    WalletTopUpAmountError,
    WalletTopUpNotFoundError,
    WalletTopUpStateError,
    WalletTopUpVerificationError,
)
from .models import Wallet, WalletTopUp, WalletTransaction
from .services import credit_wallet, get_or_create_wallet


ZERO = Decimal("0.00")
CENT = Decimal("0.01")
TOPUP_MIN_AMOUNT = Decimal("1")
TOPUP_MAX_AMOUNT = Decimal("100000")

STATUS_CREATED = "created"
STATUS_PENDING = "pending"
STATUS_CAPTURED = "captured"

PROVIDER_CAPTURED = "captured"
PROVIDER_FAILED = "failed"
PROVIDER_PENDING = "pending"
PROVIDER_NO_PAYMENT = "no_payment"
PROVIDER_UNCERTAIN = "uncertain"

_ACTIVE_STATUSES = (
    WalletTopUp.Status.CREATED,
    WalletTopUp.Status.PENDING,
)

_TERMINAL_STATUSES = (
    WalletTopUp.Status.FAILED,
    WalletTopUp.Status.EXPIRED,
)


@dataclass(frozen=True)
class TopUpInitiation:
    status: str
    topup: WalletTopUp
    checkout: dict | None = None


@dataclass(frozen=True)
class TopUpReconciliation:
    topup: WalletTopUp
    provider_state: str


def _money(value):
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _amount_paise(amount):
    return int(
        (_money(amount) * Decimal("100")).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def normalize_topup_amount(raw_amount):
    """Validate a customer-submitted top-up amount on the server."""
    if raw_amount in (None, ""):
        raise WalletTopUpAmountError("Please enter a top-up amount.")

    try:
        value = Decimal(str(raw_amount).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise WalletTopUpAmountError("Please enter a valid top-up amount.")

    if not value.is_finite():
        raise WalletTopUpAmountError("Please enter a valid top-up amount.")

    quantized = value.quantize(CENT, rounding=ROUND_HALF_UP)
    if value != quantized:
        raise WalletTopUpAmountError(
            "The top-up amount cannot have more than two decimal places."
        )

    if quantized < TOPUP_MIN_AMOUNT:
        raise WalletTopUpAmountError("The minimum top-up is ₹1.")

    if quantized > TOPUP_MAX_AMOUNT:
        raise WalletTopUpAmountError(
            "The maximum top-up per transaction is ₹1,00,000."
        )

    return quantized


def _receipt():
    return f"WALLET-{uuid.uuid4().hex[:24].upper()}"


def _epoch_to_datetime(value):
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _payment_matches(topup, payment):
    if not isinstance(payment, dict):
        return False
    if (payment.get("order_id") or "") != topup.provider_order_id:
        return False
    try:
        paid_paise = int(payment.get("amount") or 0)
    except (TypeError, ValueError):
        return False
    if paid_paise != _amount_paise(topup.amount):
        return False
    currency = (payment.get("currency") or "").upper()
    return currency == settings.RAZORPAY_CURRENCY


def _validate_captured_payment(topup, payment, *, expected_payment_id=None):
    if not isinstance(payment, dict):
        raise WalletTopUpVerificationError("The payment response was invalid.")

    if (payment.get("order_id") or "") != topup.provider_order_id:
        raise WalletTopUpVerificationError(
            "The payment does not belong to this top-up."
        )

    try:
        paid_paise = int(payment.get("amount") or 0)
    except (TypeError, ValueError):
        paid_paise = 0

    if paid_paise != _amount_paise(topup.amount):
        raise WalletTopUpVerificationError(
            "The paid amount does not match the top-up amount."
        )

    currency = (payment.get("currency") or "").upper()
    if currency != settings.RAZORPAY_CURRENCY:
        raise WalletTopUpVerificationError(
            "The payment currency does not match."
        )

    if (payment.get("status") or "").lower() != "captured":
        raise WalletTopUpVerificationError(
            "The payment has not been captured yet."
        )

    payment_id = payment.get("id")
    if not payment_id:
        raise WalletTopUpVerificationError(
            "The payment reference is missing."
        )

    if expected_payment_id and payment_id != expected_payment_id:
        raise WalletTopUpVerificationError(
            "The payment reference does not match."
        )


def _build_checkout(topup):
    user = topup.user
    profile = getattr(user, "profile", None)

    return {
        "key": settings.RAZORPAY_KEY_ID,
        "amount": _amount_paise(topup.amount),
        "currency": settings.RAZORPAY_CURRENCY,
        "name": "AURA",
        "description": "AURA Wallet top-up",
        "order_id": topup.provider_order_id,
        "prefill": {
            "name": user.get_full_name() or user.username,
            "email": user.email or "",
            "contact": getattr(profile, "mobile_number", "") or "",
        },
        "notes": {
            "wallet_topup_receipt": topup.receipt,
            "wallet_topup_id": str(topup.pk),
            "purpose": "WALLET_TOPUP",
        },
        "theme": {"color": "#1A1A1A"},
    }


def _active_topup(user):
    return (
        WalletTopUp.objects
        .filter(user=user, status__in=_ACTIVE_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )


def _get_or_create_pending_topup(user, amount):
    """Create at most one active top-up per customer under a wallet lock."""
    wallet = get_or_create_wallet(user)

    with transaction.atomic():
        locked_wallet = (
            Wallet.objects.select_for_update()
            .filter(pk=wallet.pk)
            .first()
        )
        if locked_wallet is None:
            raise WalletTopUpStateError(
                "We could not prepare your wallet top-up. Please try again."
            )

        active = _active_topup(user)
        if active is not None:
            return active, False

        topup = WalletTopUp.objects.create(
            wallet=locked_wallet,
            user=user,
            amount=amount,
            receipt=_receipt(),
            status=WalletTopUp.Status.CREATED,
        )
        return topup, True


def initiate_topup(user, raw_amount, *, retry_topup_id=None):
    """Create or reuse a top-up and start its Razorpay order.

    Every new Add Money attempt creates a fresh WalletTopUp record and a fresh
    Razorpay provider order. Previous Razorpay orders are never reused, even
    when the customer enters the same amount.

    Before creating a new order, every previously unresolved attempt
    (CREATED or PENDING) is reconciled. Provider-confirmed captures are
    credited exactly once; provider-confirmed no-payment attempts are closed
    locally as FAILED (nothing to capture); uncertain provider states block a
    new order. Terminal (FAILED / EXPIRED) attempts are preserved.
    """
    amount = normalize_topup_amount(raw_amount)

    if not settings.RAZORPAY_ENABLED:
        raise WalletTopUpStateError(
            "Online payment is temporarily unavailable. Please try again later."
        )

    # Retry path: reconcile the specific previous attempt first.
    if retry_topup_id is not None:
        retry_topup = (
            WalletTopUp.objects
            .filter(pk=retry_topup_id, user=user)
            .first()
        )
        if retry_topup is None:
            raise WalletTopUpNotFoundError(
                "We could not find this wallet top-up."
            )

        result = reconcile_topup(retry_topup)
        if result.provider_state == PROVIDER_CAPTURED:
            return TopUpInitiation(
                status=STATUS_CAPTURED,
                topup=result.topup,
            )
        if result.provider_state == PROVIDER_UNCERTAIN:
            raise WalletTopUpStateError(
                "This wallet top-up payment is still being confirmed. "
                "Please check its status before trying again."
            )
        # FAILED / EXPIRED / NO_PAYMENT: preserve terminal state, fall through
        # to create a fresh order below.

    # Reconcile every previously unresolved attempt (CREATED / PENDING).
    active_topups = list(
        WalletTopUp.objects
        .filter(
            user=user,
            status__in=(
                WalletTopUp.Status.CREATED,
                WalletTopUp.Status.PENDING,
            ),
        )
        .order_by("-created_at", "-id")
    )

    captured_topup = None
    uncertain_found = False

    for topup in active_topups:
        result = reconcile_topup(topup)
        if result.provider_state == PROVIDER_CAPTURED:
            captured_topup = result.topup
        elif result.provider_state == PROVIDER_UNCERTAIN:
            uncertain_found = True
        elif result.provider_state == PROVIDER_NO_PAYMENT:
            # Razorpay confirmed no payments on this order. Terminal from
            # AURA's perspective; nothing to late-capture. Close as FAILED.
            WalletTopUp.objects.filter(
                pk=topup.pk,
                status__in=(
                    WalletTopUp.Status.CREATED,
                    WalletTopUp.Status.PENDING,
                ),
            ).update(status=WalletTopUp.Status.FAILED)
        # PROVIDER_FAILED / EXPIRED: terminal, preserved.

    if uncertain_found:
        raise WalletTopUpStateError(
            "A previous wallet top-up payment is still being confirmed. "
            "Please check its status before trying again."
        )

    if captured_topup is not None:
        # A previous attempt was just captured and credited exactly once.
        return TopUpInitiation(
            status=STATUS_CAPTURED,
            topup=captured_topup,
        )

    topup, created = _get_or_create_pending_topup(user, amount)

    if not created:
        # A concurrent request created an active top-up first; reconcile it.
        result = reconcile_topup(topup)
        topup = result.topup
        if result.provider_state == PROVIDER_CAPTURED:
            return TopUpInitiation(status=STATUS_CAPTURED, topup=topup)
        raise WalletTopUpStateError(
            "A wallet top-up is already in progress. Please complete it or "
            "check its status before starting another."
        )

    try:
        response = gateway.create_order(
            amount_paise=_amount_paise(amount),
            receipt=topup.receipt,
            notes={
                "wallet_topup_receipt": topup.receipt,
                "wallet_topup_id": str(topup.pk),
                "purpose": "WALLET_TOPUP",
            },
        )
    except PaymentError:
        WalletTopUp.objects.filter(
            pk=topup.pk,
            status=WalletTopUp.Status.CREATED,
        ).update(status=WalletTopUp.Status.FAILED)
        raise

    provider_order_id = (response or {}).get("id")
    if not provider_order_id:
        WalletTopUp.objects.filter(
            pk=topup.pk,
            status=WalletTopUp.Status.CREATED,
        ).update(status=WalletTopUp.Status.FAILED)
        raise PaymentGatewayError(
            "We could not start the wallet top-up. Please try again."
        )

    updated = WalletTopUp.objects.filter(
        pk=topup.pk,
        status=WalletTopUp.Status.CREATED,
    ).update(
        provider_order_id=provider_order_id,
        status=WalletTopUp.Status.PENDING,
    )
    if not updated:
        WalletTopUp.objects.filter(pk=topup.pk).update(
            status=WalletTopUp.Status.FAILED
        )
        raise WalletTopUpStateError(
            "We could not start the wallet top-up. Please try again."
        )

    topup.refresh_from_db()
    return TopUpInitiation(
        status=STATUS_CREATED,
        topup=topup,
        checkout=_build_checkout(topup),
    )


def _finalize_captured(topup, payment, *, captured_at=None):
    """Credit a verified captured payment exactly once."""
    _validate_captured_payment(topup, payment)

    payment_id = payment.get("id")
    if captured_at is None:
        captured_at = (
            _epoch_to_datetime(payment.get("created_at"))
            or timezone.now()
        )

    with transaction.atomic():
        locked = (
            WalletTopUp.objects.select_for_update()
            .filter(pk=topup.pk, user_id=topup.user_id)
            .first()
        )
        if locked is None:
            raise WalletTopUpNotFoundError(
                "We could not find this wallet top-up."
            )

        if locked.status == WalletTopUp.Status.CAPTURED:
            return locked

        if (
            locked.provider_payment_id
            and locked.provider_payment_id != payment_id
        ):
            raise WalletTopUpStateError(
                "This top-up was already confirmed with a different payment."
            )

        locked.status = WalletTopUp.Status.CAPTURED
        locked.provider_payment_id = payment_id
        locked.captured_at = captured_at
        locked.save(
            update_fields=[
                "status",
                "provider_payment_id",
                "captured_at",
            ]
        )

        credit_wallet(
            wallet=locked.wallet,
            amount=locked.amount,
            kind=WalletTransaction.Kind.TOPUP,
            dedupe_key=f"wallet-topup:{locked.pk}",
            description=f"AURA Wallet top-up {locked.receipt}",
            topup=locked,
        )

    topup.refresh_from_db()
    return topup


def verify_topup(
    user,
    topup_id,
    *,
    provider_order_id,
    payment_id,
    signature,
):
    """Verify a browser-returned top-up payment and credit it exactly once."""
    topup = (
        WalletTopUp.objects
        .filter(pk=topup_id, user=user)
        .first()
    )
    if topup is None:
        raise WalletTopUpNotFoundError(
            "We could not find this wallet top-up."
        )

    if topup.status == WalletTopUp.Status.CAPTURED:
        return topup

    if not topup.provider_order_id:
        raise WalletTopUpStateError(
            "This top-up is not ready for payment verification."
        )

    if (provider_order_id or "").strip() != topup.provider_order_id:
        raise WalletTopUpVerificationError(
            "The payment does not belong to this top-up."
        )

    gateway.verify_payment_signature(
        provider_order_id=topup.provider_order_id,
        payment_id=payment_id,
        signature=signature,
    )

    payment = gateway.fetch_payment(payment_id)

    if (payment.get("order_id") or "") != topup.provider_order_id:
        raise WalletTopUpVerificationError(
            "The payment does not belong to this top-up."
        )

    status = (payment.get("status") or "").lower()
    captured_at = None
    if status == "authorized":
        captured_at = timezone.now()
        payment = gateway.capture_payment(
            payment_id,
            _amount_paise(topup.amount),
            settings.RAZORPAY_CURRENCY,
        )
        status = (payment.get("status") or "").lower()

    if status != "captured":
        raise WalletTopUpVerificationError(
            "The payment has not been captured yet."
        )

    _validate_captured_payment(
        topup,
        payment,
        expected_payment_id=payment_id,
    )

    return _finalize_captured(
        topup,
        payment,
        captured_at=captured_at,
    )


def reconcile_topup(topup):
    """Reconcile a top-up with Razorpay using request-triggered checks.

    Wallet top-ups have no payment window: a top-up is never expired or
    failed because time has passed. It remains pending until the provider
    reports a captured payment or a definitive failure.
    """
    if topup.status == WalletTopUp.Status.CAPTURED:
        return TopUpReconciliation(topup, PROVIDER_CAPTURED)

    if topup.status in _TERMINAL_STATUSES:
        return TopUpReconciliation(topup, PROVIDER_FAILED)

    if not topup.provider_order_id:
        return TopUpReconciliation(topup, PROVIDER_NO_PAYMENT)

    try:
        payments = gateway.fetch_order_payments(topup.provider_order_id)
    except PaymentError:
        # Provider state is unknown: never fail or expire on uncertainty.
        return TopUpReconciliation(topup, PROVIDER_UNCERTAIN)

    captured = None
    authorized = None
    uncertain = False
    saw_failed = False
    saw_captured_mismatch = False

    for payment in payments:
        if not isinstance(payment, dict):
            uncertain = True
            continue

        status = (payment.get("status") or "").lower()

        if status == "captured":
            if _payment_matches(topup, payment):
                captured = payment
                break
            saw_captured_mismatch = True
        elif status == "authorized":
            if _payment_matches(topup, payment):
                authorized = payment
            else:
                uncertain = True
        elif status in ("created", "pending"):
            uncertain = True
        elif status == "failed":
            saw_failed = True
        else:
            uncertain = True

    captured_at = None

    if captured is None and authorized is not None:
        captured_at = timezone.now()
        try:
            response = gateway.capture_payment(
                authorized.get("id"),
                _amount_paise(topup.amount),
                settings.RAZORPAY_CURRENCY,
            )
        except PaymentError:
            return TopUpReconciliation(topup, PROVIDER_UNCERTAIN)

        if (response.get("status") or "").lower() == "captured":
            captured = response
        else:
            return TopUpReconciliation(topup, PROVIDER_PENDING)

    if captured is not None:
        try:
            finalized = _finalize_captured(
                topup,
                captured,
                captured_at=captured_at,
            )
        except WalletError:
            return TopUpReconciliation(topup, PROVIDER_UNCERTAIN)
        return TopUpReconciliation(finalized, PROVIDER_CAPTURED)

    if uncertain or saw_captured_mismatch:
        return TopUpReconciliation(topup, PROVIDER_PENDING)

    if topup.status in _TERMINAL_STATUSES:
        return TopUpReconciliation(topup, PROVIDER_FAILED)

    if saw_failed:
        WalletTopUp.objects.filter(
            pk=topup.pk,
            status=topup.status,
        ).update(status=WalletTopUp.Status.FAILED)
        topup.refresh_from_db()
        return TopUpReconciliation(topup, PROVIDER_FAILED)

    if not payments:
        return TopUpReconciliation(topup, PROVIDER_NO_PAYMENT)

    return TopUpReconciliation(topup, PROVIDER_UNCERTAIN)
