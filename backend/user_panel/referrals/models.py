from django.conf import settings
from django.db import models


class ReferralCode(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="referral_code",
    )
    code = models.CharField(max_length=16, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Referral code"
        verbose_name_plural = "Referral codes"
        constraints = [
            models.UniqueConstraint(
                fields=["code"],
                name="referral_code_unique",
            ),
        ]

    def __str__(self):
        return f"{self.code} (user {self.user_id})"


class Referral(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        REWARDED = "REWARDED", "Rewarded"

    referrer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="referrals_made",
    )
    referred_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="referral_received",
    )
    code = models.CharField(max_length=16)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    qualifying_order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referrals_qualified",
    )
    rewarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Referral"
        verbose_name_plural = "Referrals"
        indexes = [
            models.Index(
                fields=["status", "created_at"],
                name="referral_status_created",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(referrer=models.F("referred_user")),
                name="referral_no_self_referral",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="REWARDED",
                        rewarded_at__isnull=False,
                    )
                    | models.Q(
                        status="PENDING",
                        rewarded_at__isnull=True,
                    )
                ),
                name="referral_rewarded_state_consistent",
            ),
            models.UniqueConstraint(
                fields=["qualifying_order"],
                condition=models.Q(qualifying_order__isnull=False),
                name="referral_qualifying_order_uniq",
            ),
        ]

    def __str__(self):
        return f"Referral #{self.pk} ({self.status})"
