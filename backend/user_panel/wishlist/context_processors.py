"""Shared wishlist context for user-facing templates.

Provides the header wishlist badge count on every page that uses
``RequestContext``. The count is the total number of wishlist entries
belonging to the current user, including unavailable and orphaned entries.

Anonymous and inactive users receive 0 without a database query.
"""

from .services import get_wishlist_count


def wishlist_count(request):
    """Return ``{"wishlist_count": <total entries>}`` for the header badge."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_active:
        return {"wishlist_count": 0}

    try:
        total = get_wishlist_count(user)
    except Exception:
        return {"wishlist_count": 0}

    return {"wishlist_count": total}
