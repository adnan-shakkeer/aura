from django.http import HttpResponse
from admin_panel.admin_auth.decorators import admin_required

@admin_required
def index(request):
    """
    Temporary placeholder for Admin Dashboard overview.
    """
    return HttpResponse("Admin Dashboard Placeholder")
# Create your views here.
