from django.urls import path

from . import views


app_name = "products"


urlpatterns = [
    # Product
    path(
        "",
        views.ProductListView.as_view(),
        name="list",
    ),
    path(
        "add/",
        views.ProductCreateView.as_view(),
        name="add",
    ),
    path(
        "edit/<int:product_id>/",
        views.ProductUpdateView.as_view(),
        name="edit",
    ),
    path(
        "delete/<int:product_id>/",
        views.ProductDeleteView.as_view(),
        name="delete",
    ),

    # Variant Management
    path(
        "<int:product_id>/variants/",
        views.VariantManagementView.as_view(),
        name="variants",
    ),
    path(
        "<int:product_id>/variants/add/",
        views.VariantCreateView.as_view(),
        name="variant_add",
    ),
    path(
        "variants/<int:variant_id>/edit/",
        views.VariantUpdateView.as_view(),
        name="variant_edit",
    ),
    path(
        "variants/<int:variant_id>/delete/",
        views.VariantDeleteView.as_view(),
        name="variant_delete",
    ),
]