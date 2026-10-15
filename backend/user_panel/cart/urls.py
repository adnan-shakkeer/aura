
from django.urls import path

from . import views

app_name = "cart"

urlpatterns = [
    path("", views.cart_view, name="cart_view"),
    path("add/", views.add_to_cart_view, name="add_to_cart"),
    path(
        "update/<int:item_id>/",
        views.update_cart_item_view,
        name="update_cart_item",
    ),
    path(
        "remove/<int:item_id>/",
        views.remove_cart_item_view,
        name="remove_cart_item",
    ),
    path(
        "validate-checkout/",
        views.validate_checkout_view,
        name="validate_checkout",
    ),
]
