from django.urls import path

from . import views


app_name = "coupons"

urlpatterns = [
    path("", views.coupon_list, name="list"),
    path("create/", views.coupon_create, name="create"),
    path("edit/<int:coupon_id>/", views.coupon_edit, name="edit"),
    path("toggle/<int:coupon_id>/", views.coupon_toggle, name="toggle"),
    path("delete/<int:coupon_id>/", views.coupon_delete, name="delete"),
]
