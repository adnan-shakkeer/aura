"""Homepage product queries for the user-side AURA home page.

This module encapsulates the reusable "eligible product" queryset so the
home view stays thin. It performs User-side visibility filtering only;
it never modifies or deletes products.
"""

from django.db.models import Exists, Min, OuterRef, Q, Subquery

from admin_panel.products.models import Product, ProductVariant, VariantImage


def get_available_products():
    """Return eligible products for the User-side homepage.

    A product is eligible only when::

        Product: is_deleted=False, is_listed=True
        Brand: is_deleted=False, is_listed=True
        Category: is_deleted=False, is_listed=True
        At least one ProductVariant: is_deleted=False, is_listed=True

    Stock is intentionally NOT part of the filter for Phase 1.

    Annotations per product:

    - ``starting_price``: minimum price among eligible variants.
    - ``representative_size``: size of the earliest eligible variant
      (deterministic ``created_at, id`` ordering).
    - ``primary_image``: image path of the first image
      (``display_order, created_at`` ordering) belonging to eligible
      variants, with variants ordered by ``created_at, id`` so the
      image and the representative size come from the same
      deterministic variant-selection rule.
    """
    available_variants = ProductVariant.objects.filter(
        product=OuterRef("pk"),
        is_deleted=False,
        is_listed=True,
    )

    primary_image_qs = (
        VariantImage.objects.filter(
            variant__product=OuterRef("pk"),
            variant__is_deleted=False,
            variant__is_listed=True,
        )
        .order_by(
            "variant__created_at",
            "variant__id",
            "display_order",
            "created_at",
        )
        .values("image")[:1]
    )

    representative_size_qs = (
        ProductVariant.objects.filter(
            product=OuterRef("pk"),
            is_deleted=False,
            is_listed=True,
        )
        .order_by("created_at", "id")
        .values("size")[:1]
    )

    return (
        Product.objects.filter(
            is_deleted=False,
            is_listed=True,
            brand__is_deleted=False,
            brand__is_listed=True,
            brand__category__is_deleted=False,
            brand__category__is_listed=True,
        )
        .filter(Exists(available_variants))
        .select_related("brand", "brand__category")
        .annotate(
            starting_price=Min(
                "variants__price",
                filter=Q(
                    variants__is_deleted=False,
                    variants__is_listed=True,
                ),
            ),
            representative_size=Subquery(representative_size_qs),
            primary_image=Subquery(primary_image_qs),
        )
        .order_by("-created_at")
    )


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
