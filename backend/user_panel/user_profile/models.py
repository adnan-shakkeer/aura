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