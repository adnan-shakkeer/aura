from django.urls import path
from . import views

app_name = "categories"

urlpatterns = [
    path("", views.CategoryListView.as_view(), name="list"),

    path("add/",views.CategoryCreateView.as_view(), name="add"),

    path("edit/<int:category_id>/", views.CategoryUpdateView.as_view(), name="edit"),

    path("delete/<int:category_id>/", views.CategoryDeleteView.as_view(), name="delete"),


]