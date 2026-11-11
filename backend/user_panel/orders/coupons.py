"""Coupon management services.

Provides reusable coupon validation, discount calculation, usage tracking,
and available-coupon discovery for both Cart Checkout and Buy Now flows.

All usage-limit enforcement runs inside database transactions with row
locking to prevent race conditions. Historical snapshots (coupon_code and
discount_amount) are taken at consumption time so later coupon edits,
deactivation, expiry or soft deletion never change placed orders.
"""

from decimal import Decimal

from django.db.models import Count
from django.db import transaction
from django.utils import timezone

from admin_panel.coupons.models import Coupon
from user_panel.pricing import services as pricing

from .exceptions import (
    CouponDeletedError,
    CouponError,
    CouponExpiredError,
    CouponInactiveError,
    CouponMinimumPurchaseError,
    CouponNotFoundError,
    CouponNotYetValidError,
    CouponPerUserLimitExceededError,
    CouponUsageLimitExceededError,
)
from .models import CouponUsage


ZERO = Decimal("0.00")

USAGES_LIMITING_STATUSES = [
    CouponUsage.Status.RESERVED,
    CouponUsage.Status.CONSUMED,
]


def normalize_coupon_code(value):
    """Trim whitespace and normalize a coupon code to uppercase."""
    return (value or "").strip().upper()


def get_coupon_by_code(code):
    """Return a non-deleted Coupon by case-insensitive code, or None."""
    normalized = normalize_coupon_code(code)
    if not normalized:
        return None
    return (
        Coupon.objects.filter(code__iexact=normalized, is_deleted=False)
        .first()
    )


def _check_usage_limits(coupon, user):
    """Enforce overall and per-customer usage limits with row locking.

    Locking the coupon row serializes concurrent final-use attempts so the
    limit can never be exceeded by a race between two placements.
    """
    with transaction.atomic():
        coupon = (
            Coupon.objects.select_for_update()
            .filter(pk=coupon.pk)
            .first()
        )

        if coupon is None:
            raise CouponNotFoundError("This coupon code is not valid.")

        # Count active (RESERVED + CONSUMED) usages for overall limit.
        active_usages = CouponUsage.objects.filter(
            coupon=coupon,
            status__in=USAGES_LIMITING_STATUSES,
        ).count()

        if (
            coupon.total_usage_limit is not None
            and active_usages >= coupon.total_usage_limit
        ):
            raise CouponUsageLimitExceededError(
                "This coupon has reached its usage limit."
            )

        # Per-customer limit counts only this user's active usages. A NULL
        # per-user limit means unlimited, so it is deliberately not enforced.
        if (
            user is not None
            and user.is_authenticated
            and coupon.per_user_limit is not None
        ):
            per_user_usages = CouponUsage.objects.filter(
                coupon=coupon,
                user=user,
                status__in=USAGES_LIMITING_STATUSES,
            ).count()

            if per_user_usages >= coupon.per_user_limit:
                raise CouponPerUserLimitExceededError(
                    "You have already used this coupon."
                )


def validate_coupon(coupon, user, eligible_merchandise, moment=None):
    """Validate a coupon for a given customer and checkout amount.

    Returns the validated ``Coupon`` instance. Raises a ``CouponError``
    subclass on any validation failure.
    """
    moment = moment or timezone.now()

    if coupon is None:
        raise CouponNotFoundError("This coupon code is not valid.")

    if coupon.is_deleted:
        raise CouponDeletedError("This coupon is no longer available.")

    if not coupon.is_active:
        raise CouponInactiveError("This coupon has been deactivated.")

    if not coupon.is_valid_at(moment):
        if moment < coupon.valid_from:
            raise CouponNotYetValidError("This coupon is not yet valid.")
        raise CouponExpiredError("This coupon has expired.")

    if eligible_merchandise < coupon.minimum_purchase:
        raise CouponMinimumPurchaseError(
            f"Minimum order amount of "
            f"\u20b9{coupon.minimum_purchase:,.2f} is required for "
            f"this coupon."
        )

    _check_usage_limits(coupon, user)

    return coupon


def calculate_coupon_discount(coupon, eligible_merchandise):
    """Calculate the coupon discount amount for eligible merchandise.

    Returns a Decimal discount amount. Returns ZERO if the coupon is not
    applicable or eligible merchandise is zero.
    """
    if coupon is None:
        return ZERO

    if coupon.is_deleted or not coupon.is_active:
        return ZERO

    moment = timezone.now()
    if not coupon.is_valid_at(moment):
        return ZERO

    if eligible_merchandise < coupon.minimum_purchase:
        return ZERO

    return pricing.calculate_coupon_discount(
        discount_type=coupon.discount_type,
        discount_value=coupon.discount_value,
        max_discount_amount=coupon.max_discount_amount,
        eligible_merchandise=eligible_merchandise,
    )


@transaction.atomic
def consume_coupon(coupon, order, user, discount_amount, moment=None):
    """Create a CONSUMED CouponUsage record for a placed order.

    Must be called inside the order placement transaction. The coupon row
    is locked and usage limits are rechecked before consumption. If the
    surrounding order transaction rolls back, the usage record is rolled
    back with it.
    """
    moment = moment or timezone.now()

    # Re-lock and revalidate within this transaction.
    coupon = (
        Coupon.objects.select_for_update()
        .filter(pk=coupon.pk)
        .first()
    )
    if coupon is None or coupon.is_deleted:
        raise CouponNotFoundError("This coupon is no longer available.")
    if not coupon.is_active:
        raise CouponInactiveError("This coupon has been deactivated.")

    _check_usage_limits(coupon, user)

    usage = CouponUsage.objects.create(
        coupon=coupon,
        order=order,
        user=user,
        coupon_code=coupon.code,
        status=CouponUsage.Status.CONSUMED,
        discount_amount=discount_amount,
        consumed_at=moment,
    )
    return usage


@transaction.atomic
def release_coupon_usage(usage, reason=""):
    """Release a consumed CouponUsage (idempotent).

    Used when an order reaches CANCELLED. Releasing a RELEASED or EXPIRED
    usage is a no-op, so a usage can never be released more than once.
    """
    if usage is None:
        return None

    if usage.status in (
        CouponUsage.Status.RELEASED,
        CouponUsage.Status.EXPIRED,
    ):
        return usage

    if usage.status != CouponUsage.Status.CONSUMED:
        return usage

    usage.status = CouponUsage.Status.RELEASED
    usage.released_at = timezone.now()
    usage.release_reason = (reason or "").strip()
    usage.save(
        update_fields=["status", "released_at", "release_reason", "updated_at"]
    )
    return usage


def release_coupon_usage_for_order(order, reason=""):
    """Release the CouponUsage attached to ``order`` (idempotent).

    Returns the released usage or ``None`` when the order has no coupon
    usage. Only ever touches the usage record; order and order-item
    financial snapshots are never modified.
    """
    usage = CouponUsage.objects.filter(order=order).first()
    if usage is None:
        return None
    return release_coupon_usage(usage, reason=reason)


def _aggregate_usage_counts(coupons, user=None):
    """Return (total_active_by_coupon, user_active_by_coupon) count maps.

    Counts RESERVED + CONSUMED only. A single grouped query per map keeps
    coupon discovery free of N+1 queries.
    """
    coupon_ids = [coupon.pk for coupon in coupons]

    total_counts = {
        row["coupon_id"]: row["usage_count"]
        for row in CouponUsage.objects.filter(
            coupon_id__in=coupon_ids,
            status__in=USAGES_LIMITING_STATUSES,
        )
        .values("coupon_id")
        .annotate(usage_count=Count("id"))
    }

    user_counts = {}
    if user is not None and user.is_authenticated:
        user_counts = {
            row["coupon_id"]: row["usage_count"]
            for row in CouponUsage.objects.filter(
                coupon_id__in=coupon_ids,
                user=user,
                status__in=USAGES_LIMITING_STATUSES,
            )
            .values("coupon_id")
            .annotate(usage_count=Count("id"))
        }

    return total_counts, user_counts


def discover_available_coupons(user, eligible_merchandise, moment=None):
    """Return coupons available to a customer for the current checkout.

    Includes coupons whose minimum purchase is not yet met (with an
    explicit applicability state) so customers can see what they could
    unlock. Excludes inactive, soft-deleted, expired and exhausted
    coupons.

    Returns a list of dicts with coupon display data, applicability state
    and a single Best Deal marker. Does not expose customer-level or
    order-level usage history.
    """
    moment = moment or timezone.now()

    coupons = list(
        Coupon.objects.filter(
            is_deleted=False,
            is_active=True,
            valid_from__lte=moment,
            valid_until__gte=moment,
        )
        .order_by("-created_at", "-id")
    )

    total_counts, user_counts = _aggregate_usage_counts(coupons, user)

    results = []
    for coupon in coupons:
        if coupon.total_usage_limit is not None:
            if total_counts.get(coupon.pk, 0) >= coupon.total_usage_limit:
                continue

        if (
            user is not None
            and user.is_authenticated
            and coupon.per_user_limit is not None
        ):
            if user_counts.get(coupon.pk, 0) >= coupon.per_user_limit:
                continue

        discount_amount = pricing.calculate_coupon_discount(
            discount_type=coupon.discount_type,
            discount_value=coupon.discount_value,
            max_discount_amount=coupon.max_discount_amount,
            eligible_merchandise=eligible_merchandise,
        )

        applicable = eligible_merchandise >= coupon.minimum_purchase

        results.append(
            {
                "code": coupon.code,
                "discount_type": coupon.discount_type,
                "discount_value": coupon.discount_value,
                "max_discount_amount": coupon.max_discount_amount,
                "minimum_purchase": coupon.minimum_purchase,
                "discount_amount": discount_amount,
                "applicable": applicable,
                "best_deal": False,
            }
        )

    # Best Deal is only meaningful among coupons that are actually
    # applicable for this checkout amount. Below-minimum coupons are still
    # discoverable, but must never be marked as the best deal.
    best = calculate_best_deal(results, eligible_merchandise)
    if best is not None:
        best["best_deal"] = True

    return results


def calculate_best_deal(coupons_data, eligible_merchandise):
    """Return the coupon dict with the highest applicable discount.

    Only considers coupons whose ``applicable`` flag is True and whose
    discount amount is positive. Returns ``None`` when no reliable
    comparison can be established.
    """
    if eligible_merchandise is None or eligible_merchandise <= ZERO:
        return None

    best = None
    best_discount = ZERO

    for coupon_data in coupons_data:
        if not coupon_data.get("applicable", False):
            continue
        discount = coupon_data.get("discount_amount", ZERO)
        if not isinstance(discount, Decimal):
            discount = Decimal(str(discount))
        if discount > best_discount:
            best_discount = discount
            best = coupon_data

    return best
