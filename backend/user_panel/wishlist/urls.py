from django.urls import path

from . import views

app_name = "wishlist"

urlpatterns = [
    path("", views.wishlist_page, name="wishlist_page"),
    path("items/", views.wishlist_view, name="wishlist_view"),
    path("add/", views.add_to_wishlist_view, name="add_to_wishlist"),
    path("remove/", views.remove_from_wishlist_view, name="remove_from_wishlist"),
    path(
        "remove-item/",
        views.remove_wishlist_item_view,
        name="remove_wishlist_item",
    ),
]
