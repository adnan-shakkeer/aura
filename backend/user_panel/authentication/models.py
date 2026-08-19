from django.db import models

class SignupOTP(models.Model):
    """
    Store OTP information for email verification during signup.
    """

    email = models.EmailField()

    code = models.CharField(max_length=6)

    created_at = models.DateTimeField(auto_now_add=True)

    expires_at = models.DateTimeField()

    is_verified = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Signup OTP for {self.email}"


