from django.urls import path

from . import views


app_name = "orders"

urlpatterns = [
    path(
        "",
        views.my_orders,
        name="my_orders",
    ),
    path(
        "confirmation/<str:order_number>/",
        views.order_confirmation,
        name="order_confirmation",
    ),
    path(
        "<str:order_number>/cancel/",
        views.cancel_order_view,
        name="cancel_order",
    ),
    path(
        "<str:order_number>/return/",
        views.request_return_order_view,
        name="return_order",
    ),
    path(
        "<str:order_number>/invoice/",
        views.download_invoice,
        name="download_invoice",
    ),
    path(
        "<str:order_number>/",
        views.order_detail,
        name="order_detail",
    ),
    path(
        "<str:order_number>/items/<int:item_id>/cancel/",
        views.cancel_order_item_view,
        name="cancel_item",
    ),
    path(
        "<str:order_number>/items/<int:item_id>/return/",
        views.request_return_item_view,
        name="return_item",
    ),
]
