from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from user_panel.user_profile.models import UserProfile


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