from django.urls import path

from . import views

app_name = "admin_orders"

urlpatterns = [
    path("", views.order_list, name="list"),
    path("<str:order_number>/", views.order_detail, name="detail"),
    path("<str:order_number>/status/", views.update_order_status, name="update_status"),
]

