from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils.cache import add_never_cache_headers


class BlockedUserMiddleware:
    """
    Middleware that checks if a logged-in user has been blocked (is_active=False).
    If blocked during an active session, it logs them out, displays a toast message,
    and redirects them back to the user login page.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not request.user.is_active:
            # Clear the session
            logout(request)

            # Pass error message to user login page
            messages.error(request, "Your account has been suspended. Please reach out to AURA support for assistance.")

            # Redirect to user login view
            return redirect('user_auth:login')

        response = self.get_response(request)
        return response


class NeverCacheMiddleware:
    """
    Global middleware that stamps every HTTP response with cache-prevention headers:
        Cache-Control: max-age=0, no-cache, no-store, must-revalidate, private
        Expires: Thu, 01 Jan 1970 00:00:00 GMT
        Pragma: no-cache

    This is the primary server-side defence against the Back-button cache attack,
    where a logged-out user or blocked user can click Back and view a stale page
    snapshot without the browser making a new request to the server.

    Applies to ALL responses (user portal, admin panel, and auth pages) so that
    no protected page is ever served from the browser's disk or memory cache.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        add_never_cache_headers(response)
        return response