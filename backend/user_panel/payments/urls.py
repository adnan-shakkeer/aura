from django.urls import path

from . import views


app_name = "payments"

urlpatterns = [
    path(
        "razorpay/create/",
        views.create_cart_payment,
        name="create_cart_payment",
    ),
    path(
        "razorpay/direct/create/",
        views.create_direct_payment,
        name="create_direct_payment",
    ),
    path(
        "razorpay/verify/",
        views.verify_payment,
        name="verify_payment",
    ),
    path(
        "razorpay/retry/<str:order_number>/",
        views.retry_payment,
        name="retry_payment",
    ),
    path(
        "razorpay/failed/<str:order_number>/",
        views.payment_failed,
        name="payment_failed",
    ),
]
