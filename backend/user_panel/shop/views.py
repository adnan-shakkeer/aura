"""Customer-facing shop views for AURA.

Product Details — slug-based lookup against the eligible product queryset.
An ineligible (unlisted/deleted) product returns HTTP 404.
"""

from django.shortcuts import render, get_object_or_404

from .services import get_available_products


def product_details(request, slug):
    """Display the Product Details page for a single eligible product.

    Eligibility is enforced by scoping the lookup to
    ``get_available_products()``.  An ineligible slug (product, brand, or
    category unlisted/deleted, or no eligible variants) raises 404.

    The selected variant is determined by the ``variant`` query-string
    parameter (SKU).  If absent or invalid, the earliest eligible variant
    is used as the default (deterministic ``created_at, id`` ordering,
    consistent with the representative-variant convention).

    Gallery images belong to the *selected* variant and are ordered by
    ``display_order, created_at`` as per the VariantImage model Meta.

    Related products come from the same Brand category, exclude the current
    product, enforce full eligibility, and are capped at 4.
    """
    # Enforce eligibility: 404 if product is unlisted, deleted,
    # brand/category unlisted/deleted, or has no eligible variants.
    product = get_object_or_404(get_available_products(), slug=slug)

    # All eligible variants for this product, earliest-first.
    variants = product.variants.filter(
        is_deleted=False,
        is_listed=True,
    ).order_by("created_at", "id")

    # Variant selection: prefer explicit SKU query param, fall back to first.
    selected_sku = request.GET.get("variant")
    selected_variant = None
    if selected_sku:
        selected_variant = variants.filter(sku=selected_sku).first()

    if not selected_variant:
        selected_variant = variants.first()

    # Images belong exclusively to the selected variant.
    if selected_variant:
        images = selected_variant.images.all().order_by("display_order", "created_at")
    else:
        images = []

    # Related products: same category, exclude current, full eligibility, ≤4.
    related_products = (
        get_available_products()
        .filter(brand__category=product.brand.category)
        .exclude(id=product.id)[:4]
    )

    context = {
        "product": product,
        "variants": variants,
        "selected_variant": selected_variant,
        "images": images,
        "related_products": related_products,
    }
    return render(request, "shop/product_details.html", context)
