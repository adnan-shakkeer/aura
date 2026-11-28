import logging
import re
import secrets
from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from user_panel.orders.models import Order
from user_panel.wallet.models import WalletTransaction
from user_panel.wallet.services import credit_wallet, get_or_create_wallet

from .exceptions import (
    InvalidReferralCodeError,
    ReferralCodeGenerationError,
    ReferralError,
)
from .models import Referral, ReferralCode


logger = logging.getLogger(__name__)

REFERRAL_CODE_PREFIX = "AURA"
REFERRAL_CODE_LENGTH = 8
REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_MAX_ATTEMPTS = 5
REFERRAL_CODE_CONSTRAINT = "referral_code_unique"

REFERRER_REWARD_AMOUNT = Decimal("500.00")
REFERRED_REWARD_AMOUNT = Decimal("200.00")

REWARD_STATUS_REWARDED = "REWARDED"
REWARD_STATUS_SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ReferralRewardResult:
    status: str
    referral_id: int | None = None
    order_id: int | None = None
    message: str = ""


def normalize_referral_code(value):
    return (value or "").strip().upper()


def _generate_referral_code():
    suffix = "".join(
        secrets.choice(REFERRAL_CODE_ALPHABET)
        for _ in range(REFERRAL_CODE_LENGTH)
    )
    return f"{REFERRAL_CODE_PREFIX}{suffix}"


def _integrity_constraint_name(exc):
    cause = getattr(exc, "__cause__", None)
    diag = getattr(cause, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return name

    match = re.search(r'constraint "([^"]+)"', str(exc))
    return match.group(1) if match else ""


def get_or_create_referral_code(user):
    if user is None or not getattr(user, "is_authenticated", False):
        raise ReferralError("A signed-in customer is required.")

    existing = ReferralCode.objects.filter(user=user).first()
    if existing is not None:
        return existing

    for _attempt in range(REFERRAL_CODE_MAX_ATTEMPTS):
        code = _generate_referral_code()
        try:
            with transaction.atomic():
                return ReferralCode.objects.create(user=user, code=code)
        except IntegrityError as exc:
            if _integrity_constraint_name(exc) != REFERRAL_CODE_CONSTRAINT:
                logger.exception(
                    "Could not create a referral code for user %s.",
                    user.pk,
                )
                raise

            existing = ReferralCode.objects.filter(user=user).first()
            if existing is not None:
                return existing

    raise ReferralCodeGenerationError(
        "We could not generate a referral code. Please try again."
    )


def resolve_referral_code(value, *, referred_email=""):
    normalized = normalize_referral_code(value)
    if not normalized:
        return None

    referral_code = (
        ReferralCode.objects
        .select_related("user")
        .filter(code=normalized)
        .first()
    )
    if referral_code is None:
        raise InvalidReferralCodeError(
            "The referral code entered is not valid."
        )

    if (
        referred_email
        and referral_code.user.email.casefold()
        == referred_email.strip().casefold()
    ):
        raise InvalidReferralCodeError(
            "You cannot use your own referral code."
        )

    return referral_code


def create_referral_for_signup(referred_user, code):
    if not normalize_referral_code(code):
        return None

    referral_code = resolve_referral_code(
        code,
        referred_email=referred_user.email,
    )
    if referral_code.user_id == referred_user.pk:
        raise InvalidReferralCodeError(
            "You cannot use your own referral code."
        )

    try:
        return Referral.objects.create(
            referrer_id=referral_code.user_id,
            referred_user=referred_user,
            code=referral_code.code,
        )
    except IntegrityError as exc:
        raise ReferralError(
            "We could not apply this referral code. Please try again."
        ) from exc


def lock_pending_referral_for_user(user):
    if user is None or not getattr(user, "pk", None):
        return None

    return (
        Referral.objects
        .select_for_update()
        .filter(
            referred_user_id=user.pk,
            status=Referral.Status.PENDING,
        )
        .first()
    )


def _qualifying_timestamp(order):
    if order is None or order.user_id is None:
        return None
    if order.status == Order.OrderStatus.CANCELLED:
        return None

    if order.payment_method == Order.PaymentMethod.COD:
        return order.created_at

    if order.payment_method in (
        Order.PaymentMethod.WALLET,
        Order.PaymentMethod.RAZORPAY,
    ):
        if order.payment_status != Order.PaymentStatus.PAID:
            return None
        if order.payment_collected_at is None:
            logger.warning(
                "Order %s is PAID but has no payment_collected_at; "
                "excluding it from referral qualification.",
                order.pk,
            )
            return None
        return order.payment_collected_at

    return None


def order_qualifies_for_referral(order):
    return _qualifying_timestamp(order) is not None


def find_first_qualifying_order(user):
    if user is None or not getattr(user, "pk", None):
        return None

    orders = (
        Order.objects
        .filter(user=user)
        .exclude(status=Order.OrderStatus.CANCELLED)
        .filter(
            Q(payment_method=Order.PaymentMethod.COD)
            | Q(
                payment_method__in=(
                    Order.PaymentMethod.WALLET,
                    Order.PaymentMethod.RAZORPAY,
                ),
                payment_status=Order.PaymentStatus.PAID,
                payment_collected_at__isnull=False,
            )
        )
        .only(
            "id",
            "user_id",
            "payment_method",
            "payment_status",
            "status",
            "created_at",
            "payment_collected_at",
        )
    )

    first_order = None
    first_key = None
    for order in orders:
        timestamp = _qualifying_timestamp(order)
        if timestamp is None:
            continue

        key = (timestamp, order.pk)
        if first_key is None or key < first_key:
            first_key = key
            first_order = order

    return first_order


@transaction.atomic
def process_referral_reward(order):
    if order is None or order.user_id is None:
        return ReferralRewardResult(
            status=REWARD_STATUS_SKIPPED,
            message="Order has no customer.",
        )

    referral = (
        Referral.objects
        .select_for_update()
        .filter(referred_user_id=order.user_id)
        .first()
    )
    if referral is None:
        return ReferralRewardResult(
            status=REWARD_STATUS_SKIPPED,
            order_id=order.pk,
            message="Customer has no referral.",
        )

    if referral.status == Referral.Status.REWARDED:
        return ReferralRewardResult(
            status=REWARD_STATUS_REWARDED,
            referral_id=referral.pk,
            order_id=referral.qualifying_order_id,
            message="Referral was already rewarded.",
        )

    if not order_qualifies_for_referral(order):
        return ReferralRewardResult(
            status=REWARD_STATUS_SKIPPED,
            referral_id=referral.pk,
            order_id=order.pk,
            message="Order does not qualify for a referral reward.",
        )

    first_order = find_first_qualifying_order(referral.referred_user)
    if first_order is None or first_order.pk != order.pk:
        return ReferralRewardResult(
            status=REWARD_STATUS_SKIPPED,
            referral_id=referral.pk,
            order_id=order.pk,
            message="Order is not the customer's first qualifying order.",
        )

    referrer_wallet = get_or_create_wallet(referral.referrer)
    referred_wallet = get_or_create_wallet(referral.referred_user)

    credits = [
        (
            referred_wallet,
            REFERRED_REWARD_AMOUNT,
            f"referral:{referral.pk}:referred",
            "Referral Signup Reward",
            "Referral signup reward after first order",
        ),
        (
            referrer_wallet,
            REFERRER_REWARD_AMOUNT,
            f"referral:{referral.pk}:referrer",
            "Referral Reward",
            (
                "Referral reward for inviting "
                f"{referral.referred_user.email}"
            )[:512],
        ),
    ]

    for wallet, amount, dedupe_key, description, detail in sorted(
        credits,
        key=lambda credit: credit[0].pk,
    ):
        credit_wallet(
            wallet=wallet,
            amount=amount,
            kind=WalletTransaction.Kind.REFERRAL,
            dedupe_key=dedupe_key,
            description=description,
            description_detail=detail,
        )

    referral.status = Referral.Status.REWARDED
    referral.qualifying_order = order
    referral.rewarded_at = timezone.now()
    referral.save(
        update_fields=[
            "status",
            "qualifying_order",
            "rewarded_at",
        ]
    )

    return ReferralRewardResult(
        status=REWARD_STATUS_REWARDED,
        referral_id=referral.pk,
        order_id=order.pk,
        message="Referral rewards credited.",
    )


def _process_scheduled_referral_reward(order_id):
    try:
        order = Order.objects.get(pk=order_id)
        result = process_referral_reward(order)
        if result.status == REWARD_STATUS_SKIPPED:
            logger.info(
                "Referral reward skipped for order %s: %s",
                order_id,
                result.message,
            )
    except Order.DoesNotExist:
        logger.warning(
            "Referral reward skipped: order %s no longer exists.",
            order_id,
        )
    except Exception:
        logger.exception(
            "Referral reward processing failed for order %s.",
            order_id,
        )


def schedule_referral_reward(order):
    if not order_qualifies_for_referral(order):
        return

    order_id = order.pk
    transaction.on_commit(
        lambda: _process_scheduled_referral_reward(order_id)
    )
