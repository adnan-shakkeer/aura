"""Shop and catalog product queries for the user-side.

This module encapsulates the reusable "eligible product" queryset.
It performs User-side visibility filtering only.
"""

from django.db.models import Exists, Min, OuterRef, Q, Subquery
from django.db.models.functions import Lower

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant, VariantImage

# Supported ``?sort=`` values for the Shop listing page.
SORT_NAME_ASC = "name_asc"
SORT_PRICE_LOW = "price_low"
SORT_PRICE_HIGH = "price_high"
SORT_NEWEST = "newest"

VALID_SORTS = (
    SORT_NAME_ASC,
    SORT_PRICE_LOW,
    SORT_PRICE_HIGH,
    SORT_NEWEST,
)

#: (value, label) pairs consumed directly by the listing template.
SORT_OPTIONS = (
    (SORT_NAME_ASC, "Name: A to Z"),
    (SORT_PRICE_LOW, "Price: Low to High"),
    (SORT_PRICE_HIGH, "Price: High to Low"),
    (SORT_NEWEST, "Newest First"),
)

#: Number of products per listing page.
SHOP_PAGE_SIZE = 12

#: Stored ``fragrance_concentration`` values usable as listing filters.
#: Derived from the Product model choices — no new field is introduced.
VALID_CONCENTRATIONS = tuple(
    value for value, _label in Product.FragranceConcentration.choices
)

#: (stored value, display label) pairs for the filter drawer, in the
#: model's canonical choice order.
CONCENTRATION_OPTIONS = tuple(Product.FragranceConcentration.choices)


def clean_concentration_values(values):
    """Return the valid stored concentration values, in choice order."""
    wanted = {v for v in (values or ()) if v in VALID_CONCENTRATIONS}
    return [value for value in VALID_CONCENTRATIONS if value in wanted]


def get_available_products():
    """Return eligible products for the User-side catalog.

    A product is eligible only when::

        Product: is_deleted=False, is_listed=True
        Brand: is_deleted=False, is_listed=True
        Category: is_deleted=False, is_listed=True
        At least one ProductVariant: is_deleted=False, is_listed=True

    Stock is intentionally NOT part of the filter: a product whose
    representative variant has zero stock stays visible with an
    "Out of Stock" indicator. Visibility and purchasability are separate.

    Annotations per product:

    - ``starting_price``: minimum price among eligible variants.
    - ``representative_size``: size of the earliest eligible variant
      (deterministic ``created_at, id`` ordering).
    - ``representative_stock``: stock of the earliest eligible variant
      (same deterministic ordering as ``representative_size``), used for
      availability messaging.
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

    representative_variant_qs = (
        ProductVariant.objects.filter(
            product=OuterRef("pk"),
            is_deleted=False,
            is_listed=True,
        )
        .order_by("created_at", "id")
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
            representative_size=Subquery(
                representative_variant_qs.values("size")[:1]
            ),
            representative_stock=Subquery(
                representative_variant_qs.values("stock")[:1]
            ),
            primary_image=Subquery(primary_image_qs),
        )
        .order_by("-created_at", "-id")
    )


def get_shop_products(
    *,
    q="",
    category_slugs=(),
    brand_slugs=(),
    concentrations=(),
    sort=SORT_NEWEST,
):
    """Return the eligible listing queryset with search, filters and sorting.

    ``category_slugs``, ``brand_slugs`` and ``concentrations`` are
    iterables of strings (typically from ``request.GET.getlist()``), so
    multiple drawer checkboxes combine instead of overwriting each other.
    Unknown ``sort`` values fall back to the deterministic default order.
    Filtering and sorting stay database-driven; no Python-side filtering.
    """
    queryset = get_available_products()

    search = (q or "").strip()
    if search:
        # FK traversals cannot duplicate rows, so no distinct() is needed.
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(brand__name__icontains=search)
            | Q(brand__category__name__icontains=search)
        )

    category_slugs = [s for s in (category_slugs or ()) if s]
    if category_slugs:
        queryset = queryset.filter(brand__category__slug__in=category_slugs)

    brand_slugs = [s for s in (brand_slugs or ()) if s]
    if brand_slugs:
        queryset = queryset.filter(brand__slug__in=brand_slugs)

    concentrations = clean_concentration_values(concentrations)
    if concentrations:
        queryset = queryset.filter(fragrance_concentration__in=concentrations)

    if sort == SORT_NAME_ASC:
        queryset = queryset.order_by(Lower("name"), "id")
    elif sort == SORT_PRICE_LOW:
        queryset = queryset.order_by("starting_price", "id")
    elif sort == SORT_PRICE_HIGH:
        queryset = queryset.order_by("-starting_price", "-id")
    elif sort == SORT_NEWEST:
        queryset = queryset.order_by("-created_at", "-id")
    else:
        # Any unknown value: deterministic default (newest first).
        queryset = queryset.order_by("-created_at", "-id")

    return queryset


def get_shop_categories():
    """Categories usable as listing filters.

    Only listed, non-deleted categories that own at least one fully
    eligible product (listed product + listed brand + eligible variant).
    """
    eligible_product = Product.objects.filter(
        brand__category=OuterRef("pk"),
        is_deleted=False,
        is_listed=True,
        brand__is_deleted=False,
        brand__is_listed=True,
    ).filter(
        Exists(
            ProductVariant.objects.filter(
                product=OuterRef("pk"),
                is_deleted=False,
                is_listed=True,
            )
        )
    )
    return (
        Category.objects.filter(
            is_deleted=False,
            is_listed=True,
        )
        .filter(Exists(eligible_product))
        .order_by("name")
    )


def get_shop_brands(*, category_slugs=()):
    """Brands usable as listing filters, optionally scoped to categories.

    Only listed, non-deleted brands (in listed, non-deleted categories)
    that own at least one fully eligible product.
    """
    eligible_product = Product.objects.filter(
        brand=OuterRef("pk"),
        is_deleted=False,
        is_listed=True,
    ).filter(
        Exists(
            ProductVariant.objects.filter(
                product=OuterRef("pk"),
                is_deleted=False,
                is_listed=True,
            )
        )
    )
    queryset = (
        Brand.objects.filter(
            is_deleted=False,
            is_listed=True,
            category__is_deleted=False,
            category__is_listed=True,
        )
        .filter(Exists(eligible_product))
        .select_related("category")
        .order_by("name")
    )
    category_slugs = [s for s in (category_slugs or ()) if s]
    if category_slugs:
        queryset = queryset.filter(category__slug__in=category_slugs)
    return queryset
