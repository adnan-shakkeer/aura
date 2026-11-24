"""Customer Razorpay endpoints."""

import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from user_panel.checkout.views import (
    COUPON_SESSION_KEY,
    DIRECT_COUPON_SESSION_KEY,
    DIRECT_INTENT_SESSION_KEY,
    DIRECT_REVIEW_ADDRESS_SESSION_KEY,
    DIRECT_REVIEW_SESSION_KEY,
    DIRECT_REVIEW_SUBMISSION_SESSION_KEY,
    REVIEW_ADDRESS_SESSION_KEY,
    REVIEW_SESSION_KEY,
    REVIEW_SUBMISSION_SESSION_KEY,
)
from user_panel.orders.exceptions import OrderServiceError
from user_panel.orders.models import Order
from user_panel.orders.services import (
    SOURCE_CART,
    SOURCE_DIRECT,
    STATUS_REVIEW_REQUIRED,
    place_direct_order,
    place_order,
)

from . import services
from .exceptions import (
    PaymentConfigurationError,
    PaymentError,
    PaymentGatewayError,
    PaymentNotAllowedError,
    PaymentVerificationError,
)
from .models import PaymentAttempt


logger = logging.getLogger(__name__)

PAYMENT_ORIGIN_CHECKOUT = "checkout"
PAYMENT_ORIGIN_ORDER = "order"
PAYMENT_ORIGINS = {PAYMENT_ORIGIN_CHECKOUT, PAYMENT_ORIGIN_ORDER}


def _normalize_origin(value, *, default=PAYMENT_ORIGIN_CHECKOUT):
    """Return a validated navigation origin.

    The origin only decides which page a customer returns to. It never
    influences payment state, and orders are always scoped to the signed-in
    user, so a tampered value cannot expose another order.
    """
    origin = (value or "").strip().lower()
    return origin if origin in PAYMENT_ORIGINS else default


def _json_error(message, *, status=400, **extra):
    payload = {"success": False, "message": message}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def _clear_checkout_sessions(request, order):
    if order.checkout_source == Order.CheckoutSource.DIRECT:
        for key in (
            DIRECT_REVIEW_SESSION_KEY,
            DIRECT_REVIEW_ADDRESS_SESSION_KEY,
            DIRECT_REVIEW_SUBMISSION_SESSION_KEY,
            DIRECT_INTENT_SESSION_KEY,
            DIRECT_COUPON_SESSION_KEY,
        ):
            request.session.pop(key, None)
        return

    for key in (
        REVIEW_SESSION_KEY,
        REVIEW_ADDRESS_SESSION_KEY,
        REVIEW_SUBMISSION_SESSION_KEY,
        COUPON_SESSION_KEY,
    ):
        request.session.pop(key, None)


def _review_redirect_url(source, review):
    if source == SOURCE_DIRECT:
        url = reverse("checkout:buy_now_review")
    else:
        url = reverse("checkout:checkout_review")

    if review is not None:
        return f"{url}?address={review.address_id}"
    return url


def _failure_url(order, origin=PAYMENT_ORIGIN_CHECKOUT):
    url = reverse(
        "payments:payment_failed",
        args=[order.order_number],
    )
    return f"{url}?origin={_normalize_origin(origin)}"


def _create_payment(request, *, source):
    if source == SOURCE_DIRECT:
        review_token = request.session.get(DIRECT_REVIEW_SESSION_KEY)
        address_id = request.session.get(DIRECT_REVIEW_ADDRESS_SESSION_KEY)
        place = place_direct_order
        review_url_name = "checkout:buy_now_review"
    else:
        review_token = request.session.get(REVIEW_SESSION_KEY)
        address_id = request.session.get(REVIEW_ADDRESS_SESSION_KEY)
        place = place_order
        review_url_name = "checkout:checkout_review"

    if not review_token or not address_id:
        return _json_error(
            "Please review your order before starting payment.",
            status=409,
            redirect_url=reverse(review_url_name),
        )

    try:
        result = place(
            request.user,
            address_id,
            review_token,
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.PENDING,
            checkout_source=(
                Order.CheckoutSource.DIRECT
                if source == SOURCE_DIRECT
                else Order.CheckoutSource.CART
            ),
        )
    except OrderServiceError as exc:
        return _json_error(str(exc), status=409)
    except Exception:
        logger.exception("Unexpected Razorpay order placement failure")
        return _json_error(
            "We could not place your order. Please try again.",
            status=500,
        )

    if result.status == STATUS_REVIEW_REQUIRED:
        return _json_error(
            "Your checkout changed. Please review your order again.",
            status=409,
            review_required=True,
            redirect_url=_review_redirect_url(source, result.review),
        )

    order = result.order
    if order is None:
        return _json_error(
            "We could not place your order. Please try again.",
            status=500,
        )

    if order.payment_method != Order.PaymentMethod.RAZORPAY:
        return _json_error(
            "This order was already placed with Cash on Delivery.",
            status=409,
            redirect_url=reverse(
                "orders:order_detail",
                args=[order.order_number],
            ),
        )

    if order.payment_status == Order.PaymentStatus.PAID:
        return JsonResponse(
            {
                "success": True,
                "already_paid": True,
                "redirect_url": reverse(
                    "orders:order_confirmation",
                    args=[order.order_number],
                ),
            }
        )

    try:
        _attempt, checkout_config = services.prepare_checkout(order)
    except PaymentConfigurationError as exc:
        return _json_error(str(exc), status=503)
    except PaymentNotAllowedError as exc:
        return _json_error(
            str(exc),
            status=409,
            redirect_url=_failure_url(order),
        )
    except PaymentGatewayError as exc:
        return _json_error(
            str(exc),
            status=502,
            retryable=True,
            redirect_url=_failure_url(order),
        )
    except PaymentError as exc:
        return _json_error(str(exc), status=400)

    return JsonResponse(
        {
            "success": True,
            "order_number": order.order_number,
            "checkout": checkout_config,
            "failure_url": _failure_url(order, PAYMENT_ORIGIN_CHECKOUT),
            "origin": PAYMENT_ORIGIN_CHECKOUT,
        }
    )


@login_required
@never_cache
@require_POST
def create_cart_payment(request):
    return _create_payment(request, source=SOURCE_CART)


@login_required
@never_cache
@require_POST
def create_direct_payment(request):
    return _create_payment(request, source=SOURCE_DIRECT)


@login_required
@never_cache
@require_POST
def verify_payment(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return _json_error("Invalid payment response.", status=400)

    if not isinstance(payload, dict):
        return _json_error("Invalid payment response.", status=400)

    origin = _normalize_origin(
        payload.get("origin"),
        default=PAYMENT_ORIGIN_CHECKOUT,
    )

    provider_order_id = (payload.get("razorpay_order_id") or "").strip()
    payment_id = (payload.get("razorpay_payment_id") or "").strip()
    signature = (payload.get("razorpay_signature") or "").strip()

    if not provider_order_id or not payment_id or not signature:
        return _json_error(
            "The payment response is incomplete.",
            status=400,
        )

    attempt = (
        PaymentAttempt.objects.select_related("order")
        .filter(
            provider_order_id=provider_order_id,
            order__user=request.user,
        )
        .first()
    )
    if attempt is None:
        return _json_error(
            "We could not find this payment for your account.",
            status=404,
        )

    order = attempt.order

    try:
        services.confirm_checkout_payment(
            order,
            provider_order_id=provider_order_id,
            payment_id=payment_id,
            signature=signature,
        )
    except PaymentVerificationError as exc:
        return _json_error(
            str(exc),
            status=400,
            failure_url=_failure_url(order, origin),
        )
    except PaymentGatewayError as exc:
        return _json_error(
            str(exc),
            status=502,
            failure_url=_failure_url(order, origin),
            retryable=True,
        )
    except PaymentError as exc:
        return _json_error(
            str(exc),
            status=400,
            failure_url=_failure_url(order, origin),
        )

    _clear_checkout_sessions(request, order)

    return JsonResponse(
        {
            "success": True,
            "redirect_url": reverse(
                "orders:order_confirmation",
                args=[order.order_number],
            ),
        }
    )


def _paid_redirect(order):
    return JsonResponse(
        {
            "success": True,
            "already_paid": True,
            "redirect_url": reverse(
                "orders:order_confirmation",
                args=[order.order_number],
            ),
        }
    )


@login_required
@never_cache
@require_POST
def retry_payment(request, order_number):
    order = get_object_or_404(
        Order,
        order_number=order_number,
        user=request.user,
    )

    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    origin = _normalize_origin(
        payload.get("origin"),
        default=PAYMENT_ORIGIN_ORDER,
    )

    if order.payment_method != Order.PaymentMethod.RAZORPAY:
        return _json_error(
            "This order is not an online payment order.",
            status=400,
        )

    if order.payment_status == Order.PaymentStatus.PAID:
        return _paid_redirect(order)

    if services.captured_attempt_for_order(order) is not None:
        return _paid_redirect(order)

    if (
        order.status == Order.OrderStatus.PENDING
        and order.payment_status
        in (Order.PaymentStatus.PENDING, Order.PaymentStatus.FAILED)
        and not services.payment_window_is_open(order)
    ):
        services.expire_order_payment(order.pk)
        order.refresh_from_db()
        if order.payment_status == Order.PaymentStatus.PAID:
            return _paid_redirect(order)

    if not services.is_order_payable(order):
        return _json_error(
            "This payment window has closed. Please place a new order.",
            status=409,
            redirect_url=reverse(
                "orders:order_detail",
                args=[order.order_number],
            ),
        )

    # Reconcile the previous attempt before creating/reusing a checkout.
    # This prevents a fresh attempt while a provider payment may still be
    # authorized or in progress.
    try:
        retry_state, _previous_attempt = services.reconcile_retry_attempt(
            order
        )
    except PaymentError:
        logger.exception(
            "Retry reconciliation failed for order %s",
            order.order_number,
        )
        return _json_error(
            "We could not confirm your previous payment right now. "
            "Your order is still saved. Please try again in a moment.",
            status=503,
            retryable=True,
        )

    if retry_state == services.RETRY_STATE_CAPTURED:
        return _paid_redirect(order)

    if retry_state == services.RETRY_STATE_IN_PROGRESS:
        return _json_error(
            "Your previous payment is still being processed. "
            "Please wait a moment and try again.",
            status=409,
            pending=True,
        )

    if retry_state == services.RETRY_STATE_UNAVAILABLE:
        return _json_error(
            "We could not confirm your previous payment right now. "
            "Your order is still saved. Please try again in a moment.",
            status=503,
            retryable=True,
        )

    try:
        _attempt, checkout_config = services.prepare_checkout(order)
    except PaymentConfigurationError as exc:
        return _json_error(str(exc), status=503)
    except PaymentNotAllowedError as exc:
        return _json_error(str(exc), status=409)
    except PaymentGatewayError as exc:
        return _json_error(str(exc), status=502, retryable=True)
    except PaymentError as exc:
        return _json_error(str(exc), status=400)

    return JsonResponse(
        {
            "success": True,
            "order_number": order.order_number,
            "checkout": checkout_config,
            "failure_url": _failure_url(order, origin),
            "origin": origin,
        }
    )


@login_required
@never_cache
@ensure_csrf_cookie
@require_GET
def payment_failed(request, order_number):
    order = get_object_or_404(
        Order.objects.prefetch_related("payment_attempts", "refunds"),
        order_number=order_number,
        user=request.user,
    )

    if order.payment_status == Order.PaymentStatus.PAID:
        return redirect(
            "orders:order_confirmation",
            order_number=order.order_number,
        )

    if order.payment_method != Order.PaymentMethod.RAZORPAY:
        return redirect(
            "orders:order_detail",
            order_number=order.order_number,
        )

    origin = _normalize_origin(
        request.GET.get("origin"),
        default=PAYMENT_ORIGIN_ORDER,
    )

    can_retry = services.is_order_payable(order)

    if origin == PAYMENT_ORIGIN_CHECKOUT:
        if order.checkout_source == Order.CheckoutSource.DIRECT:
            back_url = reverse("checkout:buy_now_review")
        else:
            back_url = reverse("checkout:checkout_review")
        back_label = "Back to Checkout"
    else:
        back_url = reverse(
            "orders:order_detail",
            args=[order.order_number],
        )
        back_label = "Back to Order"

    context = {
        "order": order,
        "origin": origin,
        "can_retry": can_retry,
        "retry_url": reverse(
            "payments:retry_payment",
            args=[order.order_number],
        ),
        "back_url": back_url,
        "back_label": back_label,
        "razorpay_enabled": settings.RAZORPAY_ENABLED,
        "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        "latest_attempt": order.payment_attempts.first(),
        "refunds": list(order.refunds.all()[:5]),
    }
    return render(request, "payments/payment_failed.html", context)
