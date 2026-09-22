from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.account.adapter import DefaultAccountAdapter
from django.contrib import messages
from django.urls import reverse
from django.http import HttpResponseRedirect
from user_panel.user_profile.models import UserProfile


class AURAAccountAdapter(DefaultAccountAdapter):
    def respond_user_inactive(self, request, user):
        messages.error(request, "Your account has been suspended. Please reach out to AURA support for assistance.")
        return HttpResponseRedirect(reverse("user_auth:login"))

class AURASocialAccountAdapter(DefaultSocialAccountAdapter):

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)

        full_name = f"{user.first_name} {user.last_name}".strip()

        if not full_name:
            full_name = user.email.split("@")[0]

        UserProfile.objects.get_or_create(
            user=user,
            defaults={
                "full_name": full_name,
            },
        )

        return user