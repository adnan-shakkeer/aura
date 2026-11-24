from decimal import Decimal

from django.db import models

from user_panel.orders.models import Order


class PaymentAttempt(models.Model):
    """One server-created Razorpay order and its payment outcome."""

    class Provider(models.TextChoices):
        RAZORPAY = "RAZORPAY", "Razorpay"

    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        PENDING = "PENDING", "Pending"
        AUTHORIZED = "AUTHORIZED", "Authorized"
        CAPTURED = "CAPTURED", "Captured"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    class CapturedVia(models.TextChoices):
        CHECKOUT = "CHECKOUT", "Checkout verification"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="payment_attempts",
    )

    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.RAZORPAY,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.CREATED,
    )

    amount_paise = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="INR")

    provider_order_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )

    provider_payment_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )

    provider_signature = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    receipt = models.CharField(max_length=64)
    expires_at = models.DateTimeField()

    failure_code = models.CharField(max_length=80, blank=True, default="")
    failure_description = models.TextField(blank=True, default="")

    raw_order_response = models.JSONField(default=dict, blank=True)
    raw_payment_response = models.JSONField(default=dict, blank=True)

    captured_via = models.CharField(
        max_length=20,
        choices=CapturedVia.choices,
        blank=True,
        default="",
    )
    captured_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Payment attempt"
        verbose_name_plural = "Payment attempts"
        indexes = [
            models.Index(
                fields=["order", "status"],
                name="pay_att_order_status",
            ),
            models.Index(
                fields=["provider_order_id"],
                name="pay_att_provider_order",
            ),
            models.Index(
                fields=["status", "expires_at"],
                name="pay_att_status_expiry",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gte=1),
                name="pay_att_amt_pos",
            ),
            models.CheckConstraint(
                condition=models.Q(currency="INR"),
                name="pay_att_cur_inr",
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(
                    status__in=["CREATED", "PENDING", "AUTHORIZED"]
                ),
                name="pay_att_unique_active",
            ),
        ]

    @property
    def amount(self):
        return (Decimal(self.amount_paise) / Decimal("100")).quantize(
            Decimal("0.01")
        )

    def __str__(self):
        return (
            f"{self.order.order_number} payment "
            f"({self.status})"
        )


class Refund(models.Model):
    """A manual refund obligation tracked inside AURA.

    Refunds are performed in the Razorpay Dashboard. AURA records the
    obligation, then an administrator marks it completed with the provider's
    manual reference after the money has actually been refunded.
    """

    class Kind(models.TextChoices):
        CANCELLATION = "CANCELLATION", "Cancellation"
        RETURN = "RETURN", "Return"
        LATE_PAYMENT = "LATE_PAYMENT", "Late payment"
        DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT", "Duplicate payment"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending manual refund"
        COMPLETED = "COMPLETED", "Refund completed"
        CANCELLED = "CANCELLED", "Cancelled"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="refunds",
    )

    payment_attempt = models.ForeignKey(
        PaymentAttempt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refunds",
    )

    kind = models.CharField(max_length=24, choices=Kind.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    amount_paise = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    reason = models.TextField(blank=True, default="")
    dedupe_key = models.CharField(max_length=160, unique=True)
    manual_reference = models.CharField(max_length=128, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Refund"
        verbose_name_plural = "Refunds"
        indexes = [
            models.Index(
                fields=["order", "status"],
                name="pay_ref_order_status",
            ),
            models.Index(
                fields=["status", "created_at"],
                name="pay_ref_status_created",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gte=1),
                name="pay_ref_amt_pos",
            ),
            models.CheckConstraint(
                condition=models.Q(currency="INR"),
                name="pay_ref_cur_inr",
            ),
        ]

    @property
    def amount(self):
        return (Decimal(self.amount_paise) / Decimal("100")).quantize(
            Decimal("0.01")
        )

    def __str__(self):
        return f"{self.order.order_number} refund {self.amount_paise}p ({self.status})"
