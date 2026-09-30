from django.urls import path

from . import views


app_name = "brands"

urlpatterns = [
    path("", views.BrandListView.as_view(), name="list"),
    path("add/", views.BrandCreateView.as_view(), name="add"),
    path("edit/<int:brand_id>/", views.BrandUpdateView.as_view(), name="edit"),
    path("delete/<int:brand_id>/", views.BrandDeleteView.as_view(), name="delete"),
]