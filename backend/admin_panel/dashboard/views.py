from django.http import HttpResponse
from admin_panel.admin_auth.decorators import admin_required
from django.shortcuts import render, redirect

@admin_required
def index(request):
    """
    Temporary placeholder for Admin Dashboard overview.
    """
    return render(request,"dashboard/index.html")
# Create your views here.
