from django.urls import path

from . import views

app_name = "user_brands"

urlpatterns = [
    path("", views.brand_list, name="brand_list"),
]
