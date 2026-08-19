from django.db import models
from django.contrib.auth.models import User

    
class UserProfile(models.Model):
    """
    Store additional profile information for a user.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    full_name = models.CharField(max_length=100)

    def __str__(self):
        return self.full_name

    

