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


class PanelAccessMiddleware:
    """
    Redirect authenticated users who manually visit the wrong panel.

    Scenario A — an administrator (``is_staff`` or ``is_superuser``) opens a
    User Panel page (``/``, ``/shop/``, ``/brands/``, ``/cart/``, ``/profile/``,
    ``/address/`` or the user auth pages): redirect to the Admin Dashboard
    (``dashboard:index``) with the message
    "Admin users cannot access the User Panel."

    Scenario B — a normal customer opens an Admin Panel page (any URL under
    ``/admin-panel/``): redirect to the User Panel Home page (``home``) with
    the message "You are not authorized to access the Admin Panel."

    The session is never touched, so both users stay logged in. The existing
    ``admin_required`` and ``login_required`` checks, the login/logout views,
    CSRF protection and the inactive-user handling in ``BlockedUserMiddleware``
    are all left intact — this middleware only adds an earlier, friendlier
    redirect for the wrong-panel case.
    """

    #: URL prefix for every custom Admin Panel route.
    ADMIN_PANEL_PREFIX = "/admin-panel/"

    #: Always allow the user logout route so anyone can end their session.
    USER_LOGOUT_PATH = "/logout/"

    #: Always allow the admin logout route so anyone can end their session.
    ADMIN_LOGOUT_PATH = "/admin-panel/auth/logout/"

    #: User Panel page prefixes covered by Scenario A. ``"/"`` (Home) is
    #: handled separately as an exact match so static/media-style paths are
    #: never caught by a bare ``startswith("/")`` check.
    USER_PANEL_PREFIXES = (
        "/shop",
        "/brands",
        "/cart",
        "/profile",
        "/address",
        "/login",
        "/signup",
        "/forgot-password",
        "/reset-password",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_user_panel_path(self, path):
        """Return True for User Panel pages (Scenario A scope)."""
        if path == "/":
            return True
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in self.USER_PANEL_PREFIXES
        )

    def __call__(self, request):
        user = getattr(request, "user", None)

        # Anonymous visitors keep the existing behaviour: public pages
        # render, protected pages fall through to ``login_required`` /
        # ``admin_required`` and their login redirects.
        if user is None or not user.is_authenticated:
            return self.get_response(request)

        # Suspended accounts keep the existing behaviour: they are handled
        # by ``BlockedUserMiddleware`` (which runs before this middleware),
        # so they are deliberately ignored here.
        if not user.is_active:
            return self.get_response(request)

        # Only safe page loads are redirected. POST/AJAX endpoints (cart
        # mutations, block toggles, logouts) return JSON or perform state
        # changes, where an HTML redirect would break the caller — they keep
        # the existing view-level behaviour instead.
        if request.method != "GET":
            return self.get_response(request)

        path = request.path

        # Logout must always be reachable so a session can always be ended;
        # redirecting away from it could trap a user in the wrong panel.
        if path == self.USER_LOGOUT_PATH or path.startswith(self.USER_LOGOUT_PATH):
            return self.get_response(request)
        if path == self.ADMIN_LOGOUT_PATH or path.startswith(self.ADMIN_LOGOUT_PATH):
            return self.get_response(request)

        is_admin = user.is_staff or user.is_superuser

        # Scenario A: administrator on a User Panel page.
        if is_admin and self._is_user_panel_path(path):
            messages.warning(request, "Admin users cannot access the User Panel.")
            return redirect("dashboard:index")

        # Scenario B: normal customer on an Admin Panel page.
        if not is_admin and path.startswith(self.ADMIN_PANEL_PREFIX):
            messages.warning(request, "You are not authorized to access the Admin Panel.")
            return redirect("home")

        return self.get_response(request)