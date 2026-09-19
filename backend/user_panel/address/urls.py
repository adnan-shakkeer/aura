from django.urls import path
from . import views

urlpatterns = [
    path("",views.address_book, name = "address_book"),
    path("add/",views.add_address, name = "add_address"),
    path("edit/<int:address_id>/",views.edit_address, name = "edit_address"),
    path("delete/<int:address_id>/",views.delete_address, name = "delete_address"),
    path("set-default/<int:address_id>/",views.set_default_address, name = "set_default_address"),
]