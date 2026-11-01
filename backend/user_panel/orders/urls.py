from django.urls import path

from . import views


app_name = "orders"

urlpatterns = [
    path(
        "confirmation/<str:order_number>/",
        views.order_confirmation,
        name="order_confirmation",
    ),
]
