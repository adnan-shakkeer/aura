from django.urls import path

from . import views

app_name = "users"

urlpatterns = [
    path('',views.users_list, name = "index"),
    
    path("<int:user_id>/toggle-status/",views.toggle_user_status, name = "toggle_status"),
]