"""Customer-facing shop views for AURA.

Shop Listing — database-driven search, category/brand filters, sorting
and pagination over the eligible product queryset.

Product Details — slug-based lookup against the eligible product queryset.
An ineligible (unlisted/deleted) product returns HTTP 404.
"""

from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404

from .services import (
    CONCENTRATION_OPTIONS,
    SHOP_PAGE_SIZE,
    SORT_NEWEST,
    SORT_OPTIONS,
    VALID_SORTS,
    clean_concentration_values,
    get_available_products,
    get_shop_brands,
    get_shop_categories,
    get_shop_products,
)


def shop_list(request):
    """Display the Shop listing page.

    GET parameters (all optional)::

        q               free-text search (product, brand, category names)
        category        Category slug filter (repeatable for multiple)
        brand           Brand slug filter (repeatable for multiple)
        concentration   Stored fragrance-concentration value (repeatable)
        sort            one of name_asc | price_low | price_high | newest
        page            pagination page number

    Unknown ``sort``/``concentration`` values are ignored safely.
    Invalid/out-of-range ``page`` values resolve to a valid page via
    ``Paginator.get_page()`` — never a 500.
    """
    search_query = (request.GET.get("q") or "").strip()
    category_slugs = [s for s in request.GET.getlist("category") if s]
    brand_slugs = [s for s in request.GET.getlist("brand") if s]
    concentrations = clean_concentration_values(request.GET.getlist("concentration"))
    sort = request.GET.get("sort") or SORT_NEWEST
    if sort not in VALID_SORTS:
        sort = SORT_NEWEST

    products = get_shop_products(
        q=search_query,
        category_slugs=category_slugs,
        brand_slugs=brand_slugs,
        concentrations=concentrations,
        sort=sort,
    )

    paginator = Paginator(products, SHOP_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Querystring without ``page`` so pagination/sort links preserve the
    # active search, filter and sort parameters.
    params = request.GET.copy()
    params.pop("page", None)
    base_query = params.urlencode()

    context = {
        "page_obj": page_obj,
        "products": page_obj.object_list,
        "categories": get_shop_categories(),
        "brands": get_shop_brands(category_slugs=category_slugs),
        "concentration_choices": CONCENTRATION_OPTIONS,
        "current_q": search_query,
        "current_categories": category_slugs,
        "current_brands": brand_slugs,
        "current_concentrations": concentrations,
        "current_category": category_slugs[0] if category_slugs else "",
        "current_brand": brand_slugs[0] if brand_slugs else "",
        "current_sort": sort,
        "sort_options": SORT_OPTIONS,
        "base_query": base_query,
        "total_count": paginator.count,
        "has_active_filters": bool(
            search_query or category_slugs or brand_slugs or concentrations
        ),
    }
    return render(request, "shop/shop_list.html", context)


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
