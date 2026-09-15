from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    """
    Store additional profile information for a user.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    full_name = models.CharField(max_length=100)

    profile_image = models.ImageField(
        upload_to="profile_images/",
        blank=True,
        null=True,
    )

    mobile_number = models.CharField(
        max_length=15,
        blank=True,
    )

    def __str__(self):
        return self.full_name


class EmailChangeOTP(models.Model):
    """
    Store OTP information for changing a user's email address.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
    )

    new_email = models.EmailField()

    code = models.CharField(max_length=6)

    created_at = models.DateTimeField(auto_now_add=True)

    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Email Change OTP for {self.user.email}"