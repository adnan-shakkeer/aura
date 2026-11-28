"""Wallet ledger models.

``Wallet.balance`` is a cached balance. ``WalletTransaction`` is the
append-only financial source of truth: every balance change is written as a
signed ledger row, so the cached balance can always be verified against the
sum of completed transactions.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models


ZERO = Decimal("0.00")


class Wallet(models.Model):
    """One wallet per authenticated customer."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallet",
    )

    balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Wallet"
        verbose_name_plural = "Wallets"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(balance__gte=0),
                name="wallet_balance_gte_0",
            ),
        ]

    def __str__(self):
        return f"Wallet #{self.pk} (user {self.user_id})"


class WalletTopUp(models.Model):
    """A Razorpay wallet top-up, kept separate from order payments."""

    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        PENDING = "PENDING", "Pending"
        CAPTURED = "CAPTURED", "Captured"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="topups",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet_topups",
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    provider_order_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
    )

    provider_payment_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.CREATED,
    )

    receipt = models.CharField(
        max_length=64,
        unique=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Wallet top-up"
        verbose_name_plural = "Wallet top-ups"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["user", "created_at"],
                name="wallet_topup_user_created",
            ),
            models.Index(
                fields=["status", "created_at"],
                name="wallet_topup_status_created",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=Decimal("1")),
                name="wallet_topup_amount_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__lte=Decimal("100000")),
                name="wallet_topup_amount_lte_100000",
            ),
        ]

    def __str__(self):
        return f"Top-up {self.receipt} ({self.status})"


class WalletTransaction(models.Model):
    """Append-only wallet ledger entry.

    Credits are stored as positive amounts and debits as negative amounts.
    Rows are never edited or deleted; corrections must use compensating
    transactions.
    """

    class Kind(models.TextChoices):
        TOPUP = "TOPUP", "Top-up"
        REFUND_ORDER = "REFUND_ORDER", "Order refund"
        REFUND_RETURN = "REFUND_RETURN", "Return refund"
        PURCHASE = "PURCHASE", "Purchase"
        REFERRAL = "REFERRAL", "Referral reward"

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="transactions",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet_transactions",
    )

    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    balance_after = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COMPLETED,
    )

    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wallet_transactions",
    )

    order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wallet_transactions",
    )

    topup = models.ForeignKey(
        WalletTopUp,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
    )

    dedupe_key = models.CharField(
        max_length=160,
        unique=True,
    )

    description = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    description_detail = models.CharField(
        max_length=512,
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Wallet transaction"
        verbose_name_plural = "Wallet transactions"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["wallet", "created_at"],
                name="wallet_tx_wallet_created",
            ),
            models.Index(
                fields=["user", "created_at"],
                name="wallet_tx_user_created",
            ),
            models.Index(
                fields=["order"],
                name="wallet_tx_order",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(amount=0),
                name="wallet_tx_amount_nonzero",
            ),
            models.CheckConstraint(
                condition=models.Q(balance_after__gte=0),
                name="wallet_tx_balance_gte_0",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.amount} (wallet {self.wallet_id})"

    def clean(self):
        super().clean()
        if self.wallet_id and self.user_id and self.wallet.user_id != self.user_id:
            from django.core.exceptions import ValidationError

            raise ValidationError(
                "A wallet transaction must belong to the wallet's owner."
            )
