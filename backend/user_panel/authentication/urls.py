from django.urls import path
from . import views

app_name = "user_auth"

urlpatterns = [
    path("signup/otp/",views.signup_otp, name = "signup_otp"),

    path("signup/",views.signup, name = "signup"),

    path("signup/otp/resend",views.resend_signup_otp, name = "resend_signup_otp"),

    path("login/",views.login, name = "login"),

    path("logout/",views.logout, name = "logout"),

    path("forgot-password/",views.forgot_password, name = "forgot_password"),

    path("forgot-password/otp/",views.forgot_password_otp, name = "forgot_password_otp"),

    path("forgot-password/otp/resend/",views.resend_password_reset_otp, name = "resend_password_reset_otp"),

    path("reset-password/",views.reset_password, name = "reset_password")

]