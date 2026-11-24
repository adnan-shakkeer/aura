from django.urls import path

from . import views


app_name = "wallet"

urlpatterns = [
    path("", views.wallet_page, name="wallet"),
    path("topup/", views.topup_page, name="topup_page"),
    path("topup/create/", views.topup_create, name="topup_create"),
    path("topup/verify/", views.topup_verify, name="topup_verify"),
    path(
        "topup/<int:pk>/status/",
        views.topup_status,
        name="topup_status",
    ),
    path(
        "topup/<int:pk>/dismiss/",
        views.topup_dismiss,
        name="topup_dismiss",
    ),
]
