"""Customer-facing brand views for AURA.

Brand Listing — renders every eligible brand from the existing
Admin-managed catalogue. Eligibility lives in ``services``.
"""

from django.shortcuts import render

from .services import get_visible_brands


def brand_list(request):
    """Display the public Brand Listing page."""
    brands = get_visible_brands()
    return render(request, "brands/brand_list.html", {"brands": brands})
