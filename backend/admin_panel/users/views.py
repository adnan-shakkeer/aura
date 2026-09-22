import json
from http import HTTPStatus
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.contrib.auth import get_user_model

from admin_panel.admin_auth.decorators import admin_required

User = get_user_model()

@admin_required
@never_cache
def users_list(request):
    """
    Renders Customer Management table with search, status filter, pagination,
    and default descending sort by join date (created_at).
    """
    search_query = request.GET.get("search","").strip()
    status_filter = request.GET.get("status","all").strip()

    # Exclude staff and superusers from customer list
    users_qs = User.objects.filter(is_staff=False, is_superuser=False).order_by("-date_joined")

    # Apply search filter across name, username, and email
    if search_query:
        users_qs=users_qs.filter(
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query) 
        )

    # Apply status filter
    if status_filter == "active":
        users_qs=users_qs.filter(is_active=True)
    elif status_filter == "blocked":
        users_qs=users_qs.filter(is_active=False)

    # Pagination (10 per page)
    paginator = Paginator(users_qs,10)
    page_number = request.GET.get("page",1)

    try:
        users_page = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        users_page = paginator.page(1)

    context = {
        'users': users_page,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_count': paginator.count,
    }

    return render(request,"users/index.html",context)

@admin_required
@require_POST
def toggle_user_status(request, user_id):
    """
    AJAX POST endpoint to block or unblock a customer account.
    Setting is_active=False instantly restricts access across both
    manual credentials and Google SSO logins.
    """
    user_to_toggle = get_object_or_404(User, id=user_id, is_staff=False, is_superuser=False)

    # Toggle active status
    user_to_toggle.is_active = not user_to_toggle.is_active
    user_to_toggle.save()

    status_label = "unblocked" if user_to_toggle.is_active else "blocked"

    return JsonResponse(
        {
            "success": True,
            "is_active": user_to_toggle.is_active,
            "message": f"User {user_to_toggle.get_full_name() or user_to_toggle.username} has been successfully {status_label}."
        },
        status=HTTPStatus.OK
    )
