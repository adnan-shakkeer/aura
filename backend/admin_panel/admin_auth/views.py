from http import HTTPStatus
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.views.decorators.http import require_http_methods
from .decorators import admin_required


def login(request):
    """
    Renders the luxury admin login page and handles POST authentication requests.
    """
    if request.user.is_authenticated and (request.user.is_superuser or request.user.is_staff):
        return redirect("dashboard:index")

    if request.method == "POST":
        email_or_username = request.POST.get("username","").strip()
        password = request.POST.get("password","").strip()

        if not email_or_username or not password:
            return JsonResponse(
                {
                    "success" : False,
                    "message" : "Please enter both credentials."
                },
                status=HTTPStatus.BAD_REQUEST
            )

        user = authenticate(request, username=email_or_username, password=password)

        if user is not None:
            if user.is_superuser or user.is_staff:
                if not user.is_active:
                    return JsonResponse(
                        {
                            "success" : False,
                            "messages" : "This admin account has been deactivated."
                        },
                        status=HTTPStatus.FORBIDDEN,
                    )

                # Using aliased auth_login to prevent shadowing
                auth_login(request, user)
                return JsonResponse(
                    {
                        "success" : True,
                        "messages" : "Authentication successful. Redirecting..."
                    },
                    status=HTTPStatus.OK,
                )
            else:
                return JsonResponse(
                    {
                        "success" : False,
                        "message" : "Access denied. You do not have admin permissions."
                    },
                    status=HTTPStatus.FORBIDDEN,
                )

        else:
            return JsonResponse(
                {
                    "success" : False,
                    "message" : "Invalid email/username or password."
                },
                status=HTTPStatus.UNAUTHORIZED,
            )

    return render(request,"admin_auth/login.html")

@admin_required
@require_http_methods(["POST"])
def logout(request):
    """
    Flushes admin session and redirects to login.
    """
    auth_logout(request)

    return redirect("admin_auth:login")
    

