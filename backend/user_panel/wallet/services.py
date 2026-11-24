"""Server-authoritative wallet ledger services.

Every wallet balance change must go through :func:`credit_wallet` or
:func:`debit_wallet`. Both are atomic, lock the wallet row and append a
``WalletTransaction``. ``Wallet.balance`` is only a cache and can always be
verified against the ledger with :func:`reconcile_wallet`.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.db.models import Sum

from .exceptions import (
    WalletDuplicateTransactionError,
    WalletError,
    WalletInsufficientBalanceError,
    WalletInvalidAmountError,
    WalletInvalidKindError,
    WalletNotFoundError,
    WalletOwnershipError,
)
from .models import Wallet, WalletTopUp, WalletTransaction


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class WalletReconciliation:
    wallet_id: int
    cached_balance: Decimal
    ledger_balance: Decimal
    is_consistent: bool


def _money(value):
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def normalize_amount(amount):
    """Return ``amount`` as a positive 2dp Decimal or raise."""
    try:
        value = _money(amount)
    except (InvalidOperation, TypeError, ValueError):
        raise WalletInvalidAmountError(
            "The wallet amount is not a valid number."
        )
    if not value.is_finite():
        raise WalletInvalidAmountError(
            "The wallet amount is not a valid number."
        )
    if value <= ZERO:
        raise WalletInvalidAmountError(
            "The wallet amount must be greater than zero."
        )
    return value


def _assert_customer(user):
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
    ):
        raise WalletOwnershipError(
            "A signed-in customer is required."
        )
    return user


def _normalize_dedupe_key(dedupe_key):
    key = (dedupe_key or "").strip()
    if not key:
        raise WalletError("A deduplication key is required.")
    if len(key) > 160:
        raise WalletError("The deduplication key is too long.")
    return key


def _validate_kind(kind):
    if kind not in WalletTransaction.Kind.values:
        raise WalletInvalidKindError("Unknown wallet transaction kind.")
    return kind


def get_wallet(user):
    """Return the customer's wallet or ``None`` without creating one."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return Wallet.objects.filter(user=user).first()


def get_or_create_wallet(user):
    """Return the customer's wallet, creating it exactly once if needed."""
    user = _assert_customer(user)

    try:
        wallet, _created = Wallet.objects.get_or_create(user=user)
    except IntegrityError:
        # A concurrent request created the wallet first.
        wallet = Wallet.objects.get(user=user)

    return wallet


def _lock_wallet(wallet):
    locked = (
        Wallet.objects.select_for_update()
        .filter(pk=wallet.pk)
        .first()
    )
    if locked is None:
        raise WalletNotFoundError("Wallet not found.")
    return locked


def _validate_references(wallet, *, order=None, order_item=None, topup=None):
    if order is not None and order.user_id != wallet.user_id:
        raise WalletOwnershipError(
            "The order does not belong to this wallet's customer."
        )

    if order_item is not None:
        if order_item.order.user_id != wallet.user_id:
            raise WalletOwnershipError(
                "The order item does not belong to this wallet's customer."
            )
        if order is not None and order_item.order_id != order.pk:
            raise WalletOwnershipError(
                "The order item does not belong to the referenced order."
            )

    if topup is not None:
        if (
            topup.wallet_id != wallet.pk
            or topup.user_id != wallet.user_id
        ):
            raise WalletOwnershipError(
                "The top-up does not belong to this wallet."
            )


def _reference_ids(*, order=None, order_item=None, topup=None):
    return (
        order.pk if order is not None else None,
        order_item.pk if order_item is not None else None,
        topup.pk if topup is not None else None,
    )


def _resolve_existing_transaction(
    dedupe_key,
    *,
    wallet,
    amount,
    kind,
    order=None,
    order_item=None,
    topup=None,
):
    """Return a matching existing transaction or raise on a conflict."""
    existing = (
        WalletTransaction.objects
        .filter(dedupe_key=dedupe_key)
        .first()
    )
    if existing is None:
        return None

    order_id, order_item_id, topup_id = _reference_ids(
        order=order,
        order_item=order_item,
        topup=topup,
    )

    if (
        existing.wallet_id == wallet.pk
        and _money(existing.amount) == _money(amount)
        and existing.kind == kind
        and existing.order_id == order_id
        and existing.order_item_id == order_item_id
        and existing.topup_id == topup_id
    ):
        return existing

    raise WalletDuplicateTransactionError(
        "This wallet operation was already processed with different details."
    )


def _create_transaction(
    *,
    wallet,
    amount,
    kind,
    balance_after,
    dedupe_key,
    description="",
    order=None,
    order_item=None,
    topup=None,
):
    try:
        with transaction.atomic():
            return WalletTransaction.objects.create(
                wallet=wallet,
                user_id=wallet.user_id,
                kind=kind,
                amount=amount,
                balance_after=balance_after,
                status=WalletTransaction.Status.COMPLETED,
                order=order,
                order_item=order_item,
                topup=topup,
                dedupe_key=dedupe_key,
                description=(description or "").strip(),
            )
    except IntegrityError as exc:
        raise WalletDuplicateTransactionError(
            "This wallet operation has already been processed."
        ) from exc


@transaction.atomic
def credit_wallet(
    *,
    wallet,
    amount,
    kind,
    dedupe_key,
    description="",
    order=None,
    order_item=None,
    topup=None,
):
    """Credit a wallet exactly once for the given deduplication key."""
    amount = normalize_amount(amount)
    dedupe_key = _normalize_dedupe_key(dedupe_key)
    kind = _validate_kind(kind)

    wallet = _lock_wallet(wallet)

    existing = _resolve_existing_transaction(
        dedupe_key,
        wallet=wallet,
        amount=amount,
        kind=kind,
        order=order,
        order_item=order_item,
        topup=topup,
    )
    if existing is not None:
        return existing

    _validate_references(
        wallet,
        order=order,
        order_item=order_item,
        topup=topup,
    )

    new_balance = _money(wallet.balance + amount)
    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])

    return _create_transaction(
        wallet=wallet,
        amount=amount,
        kind=kind,
        balance_after=new_balance,
        dedupe_key=dedupe_key,
        description=description,
        order=order,
        order_item=order_item,
        topup=topup,
    )


@transaction.atomic
def debit_wallet(
    *,
    wallet,
    amount,
    kind,
    dedupe_key,
    description="",
    order=None,
    order_item=None,
    topup=None,
):
    """Debit a wallet exactly once, never allowing a negative balance."""
    amount = normalize_amount(amount)
    dedupe_key = _normalize_dedupe_key(dedupe_key)
    kind = _validate_kind(kind)

    wallet = _lock_wallet(wallet)

    existing = _resolve_existing_transaction(
        dedupe_key,
        wallet=wallet,
        amount=amount,
        kind=kind,
        order=order,
        order_item=order_item,
        topup=topup,
    )
    if existing is not None:
        return existing

    _validate_references(
        wallet,
        order=order,
        order_item=order_item,
        topup=topup,
    )

    if wallet.balance < amount:
        raise WalletInsufficientBalanceError(
            "Your wallet balance is not enough for this payment."
        )

    new_balance = _money(wallet.balance - amount)
    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])

    return _create_transaction(
        wallet=wallet,
        amount=-amount,
        kind=kind,
        balance_after=new_balance,
        dedupe_key=dedupe_key,
        description=description,
        order=order,
        order_item=order_item,
        topup=topup,
    )


def ledger_balance(wallet):
    """Return the wallet's balance calculated from its completed ledger."""
    total = (
        WalletTransaction.objects
        .filter(
            wallet=wallet,
            status=WalletTransaction.Status.COMPLETED,
        )
        .aggregate(total=Sum("amount"))["total"]
    )
    return _money(total or ZERO)


def wallet_is_consistent(wallet):
    return _money(wallet.balance) == ledger_balance(wallet)


@transaction.atomic
def reconcile_wallet(wallet, *, repair=False):
    """Compare the cached balance with the ledger.

    With ``repair=True`` the cached balance is corrected under a row lock.
    Ledger rows are never modified; financial corrections must use
    compensating transactions.
    """
    if repair:
        wallet = _lock_wallet(wallet)

    expected = ledger_balance(wallet)
    cached = _money(wallet.balance)
    consistent = cached == expected

    if repair and not consistent:
        wallet.balance = expected
        wallet.save(update_fields=["balance", "updated_at"])
        cached = expected
        consistent = True

    return WalletReconciliation(
        wallet_id=wallet.pk,
        cached_balance=cached,
        ledger_balance=expected,
        is_consistent=consistent,
    )
