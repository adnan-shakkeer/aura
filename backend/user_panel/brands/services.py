"""User-side brand catalogue queries.

Read-only access over the existing Admin-managed ``Brand`` model
(``admin_panel.brands``). No new model or image field is introduced here.
"""

from django.db.models import Exists, OuterRef

from admin_panel.brands.models import Brand
from admin_panel.products.models import Product, ProductVariant


def get_visible_brands():
    """Return brands eligible for the public catalogue.

    A brand is eligible only when::

        Brand: is_deleted=False, is_listed=True
        Category: is_deleted=False, is_listed=True
        At least one Product: is_deleted=False, is_listed=True
        with at least one ProductVariant: is_deleted=False, is_listed=True

    Stock is intentionally NOT part of the filter, consistent with the
    Shop eligibility rules (visibility and purchasability are separate).

    Ordering is alphabetical by name for a stable editorial grid.
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
    return (
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
