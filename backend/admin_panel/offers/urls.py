from django.urls import path

from . import views


app_name = "offers"

urlpatterns = [
    path("", views.offer_list, name="list"),
    path("create/", views.offer_create, name="create"),
    path("edit/<int:offer_id>/", views.offer_edit, name="edit"),
    path("toggle/<int:offer_id>/", views.offer_toggle, name="toggle"),
    path("delete/<int:offer_id>/", views.offer_delete, name="delete"),
]
