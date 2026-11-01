from django.urls import path

from . import views


app_name = "checkout"

urlpatterns = [
    path("", views.checkout_review, name="checkout_review"),
    path("place-order/", views.place_order_submit, name="place_order"),
]
