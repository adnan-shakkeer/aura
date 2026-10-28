"""Wishlist operations for the authenticated customer.

Business rules live here; views only translate HTTP input/output and
``WishlistServiceError`` into JSON responses.
"""

from django.db import IntegrityError, transaction

from admin_panel.products.models import ProductVariant

from user_panel.shop.services import get_eligible_variants

from .models import WishlistItem


class WishlistServiceError(Exception):
    """Raised when a wishlist operation violates a business rule."""


def _require_active_user(user):
    """Ensure wishlist operations are performed by an active user."""
    if not user or not user.is_authenticated:
        raise WishlistServiceError("Please log in to use your wishlist.")

    if not user.is_active:
        raise WishlistServiceError("Your account is not active.")


def _coerce_positive_id(raw, *, message):
    """Return a positive integer id or raise a service error."""
    if isinstance(raw, bool):
        raise WishlistServiceError(message)

    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise WishlistServiceError(message) from None

    if value < 1:
        raise WishlistServiceError(message)

    return value


def _coerce_variant_id(raw):
    """Return a positive integer variant id or raise a service error."""
    return _coerce_positive_id(
        raw,
        message="Please provide a valid product variant.",
    )


def _coerce_item_id(raw):
    """Return a positive integer wishlist item id or raise a service error."""
    return _coerce_positive_id(
        raw,
        message="Please provide a valid wishlist item.",
    )


def _get_eligible_variant(variant_id, *, lock=False):
    """Return a variant visible in the catalogue, or raise a service error.

    Eligibility is delegated to the canonical Shop helper, so Product,
    Brand and Category visibility rules are never re-implemented here.
    Stock is not part of eligibility.

    ``lock=True`` locks the variant row and requires an active transaction.
    It is used when adding so a concurrent hard delete cannot remove the
    variant between eligibility validation and snapshot population.
    """
    queryset = get_eligible_variants()

    if lock:
        queryset = queryset.select_for_update()

    variant = queryset.filter(pk=variant_id).first()

    if variant is None:
        raise WishlistServiceError("This product variant is unavailable.")

    return variant


def _delete_user_wishlist_rows(user, **lookup):
    """Delete matching rows owned by ``user`` and report whether any existed."""
    deleted_count, _details = WishlistItem.objects.filter(
        user=user,
        **lookup,
    ).delete()

    return deleted_count > 0


def get_wishlist_count(user):
    """Return the number of entries in the user's wishlist."""
    _require_active_user(user)
    return WishlistItem.objects.filter(user=user).count()


@transaction.atomic
def add_to_wishlist(user, variant_id):
    """Add an eligible variant to the user's wishlist.

    Repeated requests are idempotent: the existing row is returned with
    ``created=False``. Deleted/unlisted variants (or an ineligible
    product/brand/category hierarchy) are rejected. Zero-stock variants
    are allowed; availability is reported separately when reading.

    The variant row is locked for the duration of the transaction so a
    concurrent hard delete cannot remove it between eligibility validation
    and snapshot population. If the deletion wins the race anyway, the
    operation raises ``WishlistServiceError`` instead of leaking
    ``ProductVariant.DoesNotExist`` or creating an orphaned entry.
    """
    _require_active_user(user)
    value = _coerce_variant_id(variant_id)
    variant = _get_eligible_variant(value, lock=True)

    try:
        item, created = WishlistItem.objects.get_or_create(
            user=user,
            variant=variant,
        )
    except ProductVariant.DoesNotExist:
        raise WishlistServiceError(
            "This product variant is unavailable."
        ) from None
    except IntegrityError:
        # A concurrent hard delete can also surface as a foreign-key
        # integrity error. Only translate it when the variant is gone;
        # unrelated integrity errors are allowed to propagate.
        if not get_eligible_variants().filter(pk=value).exists():
            raise WishlistServiceError(
                "This product variant is unavailable."
            ) from None
        raise

    return item, created


def remove_from_wishlist(user, variant_id):
    """Remove a variant from the user's wishlist.

    Returns ``True`` when a row was removed and ``False`` when the variant
    was not in the user's wishlist (including another user's entry).
    Removal is scoped to ``user`` and is idempotent.
    """
    _require_active_user(user)
    value = _coerce_variant_id(variant_id)
    return _delete_user_wishlist_rows(user, variant_id=value)


def remove_wishlist_item(user, item_id):
    """Remove one of the user's wishlist entries by its primary key.

    This works for entries whose variant has been hard-deleted
    (``variant_id`` is ``NULL``). Another user's entry and a nonexistent
    entry both return ``False`` without revealing which case occurred.
    """
    _require_active_user(user)
    value = _coerce_item_id(item_id)
    return _delete_user_wishlist_rows(user, pk=value)


def get_wishlist_items(user):
    """Return the user's wishlist entries with availability flags.

    Entries whose variant or catalogue hierarchy is no longer available
    are preserved and flagged. Hard-deleted variants (``variant is None``)
    rely on the stored identity snapshots.
    """
    _require_active_user(user)

    items = list(
        WishlistItem.objects.filter(user=user)
        .select_related("variant__product__brand__category")
        .prefetch_related("variant__images")
        .order_by("-added_at", "-id")
    )

    variant_ids = [
        item.variant_id
        for item in items
        if item.variant_id is not None
    ]

    eligible_ids = (
        set(
            get_eligible_variants()
            .filter(pk__in=variant_ids)
            .values_list("pk", flat=True)
        )
        if variant_ids
        else set()
    )

    for item in items:
        variant = item.variant
        is_catalogue_eligible = (
            variant is not None and variant.pk in eligible_ids
        )
        is_in_stock = variant is not None and variant.stock > 0

        item.is_catalogue_eligible = is_catalogue_eligible
        item.is_in_stock = is_in_stock
        item.is_available = is_catalogue_eligible and is_in_stock
        item.is_out_of_stock = is_catalogue_eligible and not is_in_stock
        item.is_unavailable = not is_catalogue_eligible

    return items


def get_wishlisted_variant_ids(user, variant_ids):
    """Return the supplied variant ids that are in the user's wishlist.

    Invalid ids are ignored. Anonymous or inactive users receive an empty
    set so public product cards can call this without special-casing.
    """
    normalized = set()

    for raw in variant_ids or ():
        if isinstance(raw, bool):
            continue

        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue

        if value > 0:
            normalized.add(value)

    if not normalized:
        return set()

    if not user or not user.is_authenticated or not user.is_active:
        return set()

    return set(
        WishlistItem.objects.filter(
            user=user,
            variant_id__in=normalized,
        ).values_list("variant_id", flat=True)
    )
