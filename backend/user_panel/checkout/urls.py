from django.urls import path

from . import views


app_name = "checkout"

urlpatterns = [
    path("", views.checkout_review, name="checkout_review"),
    path("place-order/", views.place_order_submit, name="place_order"),
    path("buy-now/", views.buy_now_start, name="buy_now_start"),
    path(
        "buy-now/review/",
        views.buy_now_review,
        name="buy_now_review",
    ),
    path(
        "buy-now/place-order/",
        views.buy_now_place_order,
        name="buy_now_place",
    ),
]
