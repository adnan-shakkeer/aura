from django.urls import path

from . import views


app_name = "checkout"

urlpatterns = [
    path("", views.checkout_review, name="checkout_review"),
    path("apply-coupon/", views.apply_coupon, name="apply_coupon"),
    path("remove-coupon/", views.remove_coupon, name="remove_coupon"),
    path(
        "available-coupons/",
        views.available_coupons,
        name="available_coupons",
    ),
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
