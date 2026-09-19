from django.conf import settings
from django.db import models, transaction


class Address(models.Model):
    """
    Store user shipping and billing addresses with default selection support.
    """

    ADDRESS_TYPE_CHOICES = [
        ("HOME", "Home"),
        ("WORK", "Work"),
        ("OTHER", "Other"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="addresses",
    )
    full_name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15)
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, blank=True, null=True)
    landmark = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    pincode = models.CharField(max_length=10)
    address_type = models.CharField(
        max_length=10, choices=ADDRESS_TYPE_CHOICES, default="HOME"
    )
    is_default = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "-created_at"]
        verbose_name_plural = "Addresses"

    def __str__(self):
        return f"{self.full_name} - {self.city} ({self.address_type})"

    def save(self, *args, **kwargs):
        """
        Ensure only one address is marked as default per user atomically.
        """
        with transaction.atomic():
            if self.is_default:
                # Unset is_default on all other active addresses for this user
                Address.objects.filter(
                    user=self.user, is_default=True, is_deleted=False
                ).exclude(pk=self.pk).update(is_default=False)
            else:
                # If this is the user's first active address, force it to be default
                has_other_default = (
                    Address.objects.filter(
                        user=self.user, is_default=True, is_deleted=False
                    )
                    .exclude(pk=self.pk)
                    .exists()
                )
                if not has_other_default and not self.is_deleted:
                    self.is_default = True

            super().save(*args, **kwargs)