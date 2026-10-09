"""Homepage product queries for the user-side AURA home page.

get_available_products() is the canonical source of truth for
customer-facing product eligibility. It lives in user_panel.shop.services
and is imported here so existing callers remain unchanged.
"""

from user_panel.shop.services import get_available_products  # noqa: F401


def get_featured_products(limit=4):
    """Newest ``limit`` eligible products for Featured Perfumes."""
    return get_available_products()[:limit]


def get_new_arrivals(offset=4, limit=4):
    """Next ``limit`` eligible products after the featured slice.

    Temporary deterministic merchandising rule for Phase 1 so Featured
    and New Arrivals never show identical products. No merchandising
    model field is introduced.
    """
    return get_available_products()[offset : offset + limit]
