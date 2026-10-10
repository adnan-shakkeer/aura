from django.urls import path
from . import views

app_name = "shop"

urlpatterns = [
    path("", views.shop_list, name="shop_list"),
    path("<slug:slug>/", views.product_details, name="product_details"),
]
