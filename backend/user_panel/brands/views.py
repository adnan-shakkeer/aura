"""Customer-facing brand views for AURA.

Brand Listing — renders every eligible brand from the existing
Admin-managed catalogue. Eligibility lives in ``services``.

Brand Detail — hero plus the brand's eligible products, reusing the
Shop product eligibility, search, concentration filtering, sorting
and pagination conventions.
"""


from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render

from .services import get_visible_brands

from user_panel.shop.services import (
    CONCENTRATION_OPTIONS,
    SHOP_PAGE_SIZE,
    SORT_NEWEST,
    SORT_OPTIONS,
    VALID_SORTS,
    clean_concentration_values,
    clean_price_range,
    get_shop_products,
)


def brand_list(request):
    brands = get_visible_brands()

    return render(
        request,
        "brands/brand_list.html",
        {
            "brands": brands,
        },
    )


def brand_detail(request, slug):
    # 1. Get the requested brand.
    # Only brands eligible for public display are accessible.
    brand = get_object_or_404(
        get_visible_brands(),
        slug=slug,
    )

    # 2. Read query parameters.
    search_query = request.GET.get("q", "").strip()

    concentrations = clean_concentration_values(
        request.GET.getlist("concentration")
    )

    min_price, max_price = clean_price_range(
        request.GET.get("min_price"),
        request.GET.get("max_price"),
    )

    requested_sort = request.GET.get("sort") or SORT_NEWEST

    # 3. Validate the selected sorting option.
    # Featured is no longer a supported sorting option.
    sort_by = (
        requested_sort
        if requested_sort in VALID_SORTS
        else SORT_NEWEST
    )

    # 4. Reuse the Shop service for product eligibility,
    # searching, concentration and price filtering, and sorting.
    # Brand isolation stays in the URL path via ``brand_slugs``: a price
    # filter can never surface another brand's products.
    products = get_shop_products(
        q=search_query,
        brand_slugs=(brand.slug,),
        concentrations=concentrations,
        sort=sort_by,
        min_price=min_price,
        max_price=max_price,
    )

    # 5. Paginate the filtered product queryset.
    paginator = Paginator(products, SHOP_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    # 6. Querystring without ``page`` so pagination/sort links preserve
    # the active search, filter and sort parameters (same convention
    # as the Shop listing). The brand restriction lives in the URL
    # path itself, so no brand parameter is needed here.
    params = request.GET.copy()
    params.pop("page", None)
    base_query = params.urlencode()

    # 7. Prepare the template context.
    context = {
        "brand": brand,
        "products": page_obj.object_list,
        "page_obj": page_obj,
        "search_query": search_query,
        "current_q": search_query,
        "concentrations": concentrations,
        "current_concentrations": concentrations,
        "concentration_options": CONCENTRATION_OPTIONS,
        "concentration_choices": CONCENTRATION_OPTIONS,
        "current_sort": sort_by,
        "sort_options": SORT_OPTIONS,
        "current_min_price": "" if min_price is None else str(min_price),
        "current_max_price": "" if max_price is None else str(max_price),
        "base_query": base_query,
        "total_count": paginator.count,
        "has_active_filters": bool(
            search_query
            or concentrations
            or min_price is not None
            or max_price is not None
        ),
    }

    return render(
        request,
        "brands/brand_detail.html",
        context,
    )
