"""Customer-facing shop views for AURA.

Shop Listing — database-driven search, category/brand filters, sorting
and pagination over the eligible product queryset.

Product Details — slug-based lookup that renders listed products normally
and renders unlisted (but not deleted) products in an UNAVAILABLE state.
A missing, soft-deleted, or brand/category-ineligible product returns
HTTP 404.
"""

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render, get_object_or_404

from .services import (
    CONCENTRATION_OPTIONS,
    SHOP_PAGE_SIZE,
    SORT_NEWEST,
    SORT_OPTIONS,
    VALID_SORTS,
    clean_concentration_values,
    clean_price_range,
    get_available_products,
    get_product_details_lookup,
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
        min_price       Minimum eligible-variant price (Decimal, optional)
        max_price       Maximum eligible-variant price (Decimal, optional)
        sort            one of name_asc | price_low | price_high | newest
        page            pagination page number

    Unknown ``sort``/``concentration`` values are ignored safely.
    Absent, malformed, negative, or non-finite price bounds are treated
    as absent; a minimum above the maximum ignores both boundaries.
    Invalid/out-of-range ``page`` values resolve to a valid page via
    ``Paginator.get_page()`` — never a 500.
    """
    search_query = (request.GET.get("q") or "").strip()
    category_slugs = [s for s in request.GET.getlist("category") if s]
    brand_slugs = [s for s in request.GET.getlist("brand") if s]
    concentrations = clean_concentration_values(request.GET.getlist("concentration"))
    min_price, max_price = clean_price_range(
        request.GET.get("min_price"),
        request.GET.get("max_price"),
    )
    sort = request.GET.get("sort") or SORT_NEWEST
    if sort not in VALID_SORTS:
        sort = SORT_NEWEST

    products = get_shop_products(
        q=search_query,
        category_slugs=category_slugs,
        brand_slugs=brand_slugs,
        concentrations=concentrations,
        sort=sort,
        min_price=min_price,
        max_price=max_price,
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
        "current_min_price": "" if min_price is None else str(min_price),
        "current_max_price": "" if max_price is None else str(max_price),
        "current_sort": sort,
        "sort_options": SORT_OPTIONS,
        "base_query": base_query,
        "total_count": paginator.count,
        "has_active_filters": bool(
            search_query
            or category_slugs
            or brand_slugs
            or concentrations
            or min_price is not None
            or max_price is not None
        ),
    }
    return render(request, "shop/shop_list.html", context)


def product_details(request, slug):
    """Display the Product Details page for a single product.

    Catalogue visibility and direct-details accessibility are separate:
    an unlisted (``is_listed=False``) product renders its details page in
    an UNAVAILABLE state instead of returning 404, while a missing or
    soft-deleted product — or one whose brand/category is unlisted or
    soft-deleted — still raises 404.

    A listed product without any eligible variants keeps the existing
    behaviour and raises 404.

    The selected variant is determined by the ``variant`` query-string
    parameter (SKU).  If absent or invalid, the earliest eligible variant
    is used as the default (deterministic ``created_at, id`` ordering,
    consistent with the representative-variant convention).

    Gallery images belong to the *selected* variant and are ordered by
    ``display_order, created_at`` as per the VariantImage model Meta.

    Related products come from the same Brand category, exclude the current
    product, enforce full eligibility, and are capped at 4.
    """
    # Direct-details lookup: 404 for a missing/soft-deleted product or an
    # unlisted/soft-deleted brand or category. An unlisted product itself
    # renders instead of raising 404 here.
    product = get_object_or_404(get_product_details_lookup(), slug=slug)

    # Listed, non-deleted variants only — unchanged selection behaviour.
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

    # Preserve existing behaviour: a listed product without eligible
    # variants is inaccessible (previously enforced by the shared
    # eligibility queryset).
    if product.is_listed and selected_variant is None:
        raise Http404("Product unavailable.")

    # Server-side purchase-availability flag (display only). An unlisted
    # product is always unavailable, regardless of variant stock or listing
    # status. CartService revalidates every cart mutation, so this flag
    # never grants purchasability.
    is_unavailable = not product.is_listed

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
        "is_unavailable": is_unavailable,
    }
    return render(request, "shop/product_details.html", context)
