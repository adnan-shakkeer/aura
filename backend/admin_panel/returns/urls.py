from django.urls import path

from . import views

app_name = "admin_returns"

urlpatterns = [
    path("", views.return_list, name="list"),
    path("<int:return_item_id>/approve/", views.approve_return_view, name="approve"),
    path("<int:return_item_id>/reject/", views.reject_return_view, name="reject"),
]

