"""Shop and catalog product queries for the user-side.

This module encapsulates the reusable "eligible product" queryset.
It performs User-side visibility filtering only.
"""

from django.db.models import Exists, Min, OuterRef, Q, Subquery

from admin_panel.products.models import Product, ProductVariant, VariantImage


def get_available_products():
    """Return eligible products for the User-side catalog.

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
