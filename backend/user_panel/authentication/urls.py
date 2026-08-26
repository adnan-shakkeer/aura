from django.urls import path
from . import views


urlpatterns = [
    path("signup/otp/",views.signup_otp, name = "signup_otp"),

    path("signup/",views.signup, name = "signup"),

    path("signup/otp/resend",views.resend_signup_otp, name = "resend_signup_otp"),

    path("login/",views.login, name = "login"),

    path("logout/",views.logout, name = "logout"),

]