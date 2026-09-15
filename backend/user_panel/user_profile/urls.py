from django.urls import path

from . import views

urlpatterns = [

    path("",views.profile, name = "profile"),

    path("edit/",views.edit_profile, name = "edit_profile"),

    path("change-email/send-otp/",views.send_email_change_otp, name = "send_email_change_otp"),

    path("change-email/resend-otp/",views.resend_email_change_otp, name = "resend_email_change_otp"),

    path("change-email/verify-otp/",views.verify_email_change_otp, name = "verify_email_change_otp"),

]
