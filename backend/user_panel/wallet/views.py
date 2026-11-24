"""Customer wallet top-up views.

Only authenticated customers may use these endpoints. Every lookup is scoped
to the requesting user; top-ups are never exposed to other customers or to the
admin panel.
"""

import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from user_panel.payments.exceptions import (
    PaymentConfigurationError,
    PaymentError,
    PaymentVerificationError,
)

from .exceptions import (
    WalletError,
    WalletTopUpAmountError,
    WalletTopUpNotFoundError,
    WalletTopUpStateError,
    WalletTopUpVerificationError,
)
from .models import WalletTopUp, WalletTransaction
from .services import ZERO, get_wallet
from .topups import (
    STATUS_CAPTURED,
    TOPUP_MAX_AMOUNT,
    TOPUP_MIN_AMOUNT,
    initiate_topup,
    reconcile_topup,
    verify_topup,
)


def _wallet_transaction_description(tx):
    """Return a customer-friendly description for a wallet transaction."""
    if (tx.description or "").strip():
        return tx.description
    kind = tx.kind
    if kind == WalletTransaction.Kind.TOPUP:
        return "Wallet Top-up"
    if kind == WalletTransaction.Kind.REFUND_ORDER:
        return "Order Cancellation Refund"
    if kind == WalletTransaction.Kind.REFUND_RETURN:
        return "Return Refund"
    if kind == WalletTransaction.Kind.PURCHASE:
        return "Order Payment"
    return (kind or "Transaction").title()


def _wallet_transaction_reference(tx):
    """Return the most relevant order or top-up reference for display."""
    if tx.order:
        return tx.order.order_number
    if tx.topup:
        return tx.topup.receipt
    return ""


def _build_transaction_rows(transactions):
    """Annotate transactions with customer-facing display data."""
    rows = []
    for tx in transactions:
        if tx.amount > ZERO:
            sign = "+"
            amount_class = "text-[#2F9E6B]"
        else:
            sign = "−"
            amount_class = "text-[#B3261E]"
        rows.append(
            {
                "tx": tx,
                "sign": sign,
                "amount_class": amount_class,
                "amount": abs(tx.amount),
                "description": _wallet_transaction_description(tx),
                "reference": _wallet_transaction_reference(tx),
            }
        )
    return rows


def _json_body(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@login_required
@never_cache
@require_GET
def wallet_page(request):
    """Customer Wallet Management page: balance and transaction history.

    Read-only presentation of the customer's existing wallet data. The wallet
    balance is read from the authoritative ``Wallet`` record; transactions are
    read from the append-only ``WalletTransaction`` ledger, scoped to the
    authenticated customer and paginated at the database level.
    """
    wallet = get_wallet(request.user)
    balance = wallet.balance if wallet is not None else ZERO

    transactions_qs = (
        WalletTransaction.objects
        .filter(user=request.user)
        .select_related("order", "topup", "order_item")
        .order_by("-created_at", "-id")
    )

    paginator = Paginator(transactions_qs, 10)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "wallet/wallet.html",
        {
            "wallet": wallet,
            "balance": balance,
            "page_obj": page_obj,
            "transactions": _build_transaction_rows(page_obj.object_list),
            "active_nav": "wallet",
            "wallet_url": reverse("wallet:wallet"),
            "topup_url": reverse("wallet:topup_page"),
        },
    )


def _status_url(topup_id):
    return reverse("wallet:topup_status", args=[topup_id])


@login_required
@ensure_csrf_cookie
@never_cache
@require_GET
def topup_page(request):
    return_url = request.GET.get("next", "").strip()
    if (
        return_url
        and return_url.startswith("/")
        and not return_url.startswith("//")
        and url_has_allowed_host_and_scheme(
            return_url, allowed_hosts=None, require_https=False
        )
    ):
        request.session["wallet_return_url"] = return_url
    return render(
        request,
        "wallet/topup.html",
        {
            "razorpay_enabled": bool(settings.RAZORPAY_ENABLED),
            "topup_min_amount": int(TOPUP_MIN_AMOUNT),
            "topup_max_amount": int(TOPUP_MAX_AMOUNT),
        },
    )


@login_required
@require_POST
def topup_dismiss(request, pk):
    """Record a customer-initiated Razorpay modal dismissal as a FAILED top-up.

    A dismissal is not proof of a provider-confirmed failure, but AURA treats
    it as terminal from its own perspective: the attempt is closed locally so
    the customer can start a fresh top-up attempt. No wallet credit is issued
    and no WalletTransaction is created.
    """
    topup = get_object_or_404(
        WalletTopUp,
        pk=pk,
        user=request.user,
    )

    if topup.status in (
        WalletTopUp.Status.CAPTURED,
        WalletTopUp.Status.FAILED,
        WalletTopUp.Status.EXPIRED,
    ):
        # Already terminal: idempotent, preserve existing status.
        return JsonResponse(
            {
                "success": True,
                "redirect_url": _status_url(topup.pk),
            }
        )

    with transaction.atomic():
        locked = (
            WalletTopUp.objects
            .select_for_update()
            .filter(
                pk=topup.pk,
                status__in=(
                    WalletTopUp.Status.CREATED,
                    WalletTopUp.Status.PENDING,
                ),
            )
            .first()
        )
        if locked is None:
            return JsonResponse(
                {
                    "success": True,
                    "redirect_url": _status_url(topup.pk),
                }
            )
        WalletTopUp.objects.filter(
            pk=topup.pk,
            status=locked.status,
        ).update(status=WalletTopUp.Status.FAILED)

    return JsonResponse(
        {
            "success": True,
            "redirect_url": _status_url(topup.pk),
        }
    )


@login_required
@require_POST
def topup_create(request):
    payload = _json_body(request)
    if payload is None:
        return JsonResponse(
            {"success": False, "message": "Invalid request."},
            status=400,
        )

    retry_topup_id = payload.get("retry_topup_id")
    if retry_topup_id in (None, ""):
        retry_topup_id = None
    else:
        try:
            retry_topup_id = int(retry_topup_id)
        except (TypeError, ValueError):
            return JsonResponse(
                {
                    "success": False,
                    "message": "Invalid top-up reference.",
                },
                status=400,
            )

    try:
        initiation = initiate_topup(
            request.user,
            payload.get("amount"),
            retry_topup_id=retry_topup_id,
        )
    except WalletTopUpNotFoundError as exc:
        return JsonResponse(
            {"success": False, "message": str(exc)},
            status=404,
        )
    except WalletTopUpAmountError as exc:
        return JsonResponse(
            {"success": False, "message": str(exc)},
            status=400,
        )
    except WalletTopUpStateError as exc:
        response = {"success": False, "message": str(exc)}
        topup_id = getattr(exc, "topup_id", None)
        if topup_id:
            response["redirect_url"] = _status_url(topup_id)
        return JsonResponse(response, status=409)
    except WalletError as exc:
        return JsonResponse(
            {"success": False, "message": str(exc)},
            status=400,
        )
    except PaymentConfigurationError:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Online payment is temporarily unavailable. "
                    "Please try again later."
                ),
            },
            status=503,
        )
    except PaymentError:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "We could not start the wallet top-up. "
                    "Please try again."
                ),
            },
            status=502,
        )

    status_url = _status_url(initiation.topup.pk)

    if initiation.status == STATUS_CAPTURED:
        return JsonResponse(
            {
                "success": True,
                "already_paid": True,
                "redirect_url": status_url,
            }
        )

    return JsonResponse(
        {
            "success": True,
            "topup_id": initiation.topup.pk,
            "status_url": status_url,
            "checkout": initiation.checkout,
        }
    )


@login_required
@require_POST
def topup_verify(request):
    payload = _json_body(request)
    if payload is None:
        return JsonResponse(
            {"success": False, "message": "Invalid request."},
            status=400,
        )

    try:
        topup_id = int(payload.get("topup_id"))
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "message": "Invalid top-up reference."},
            status=400,
        )

    try:
        topup = verify_topup(
            request.user,
            topup_id,
            provider_order_id=(
                payload.get("razorpay_order_id") or ""
            ).strip(),
            payment_id=(payload.get("razorpay_payment_id") or "").strip(),
            signature=(payload.get("razorpay_signature") or "").strip(),
        )
    except WalletTopUpNotFoundError as exc:
        return JsonResponse(
            {"success": False, "message": str(exc)},
            status=404,
        )
    except WalletTopUpStateError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "pending": True,
                "redirect_url": reverse("wallet:wallet"),
            },
            status=409,
        )
    except (PaymentVerificationError, WalletTopUpVerificationError) as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "pending": True,
                "redirect_url": reverse("wallet:wallet"),
            },
            status=400,
        )
    except WalletError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "pending": True,
                "redirect_url": reverse("wallet:wallet"),
            },
            status=409,
        )
    except PaymentConfigurationError:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Online payment is temporarily unavailable. "
                    "Please check the status again shortly."
                ),
                "pending": True,
                "redirect_url": reverse("wallet:wallet"),
            },
            status=503,
        )
    except PaymentError:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "We could not confirm the payment right now. "
                    "Please check the status again in a moment."
                ),
                "pending": True,
                "redirect_url": reverse("wallet:wallet"),
            },
            status=502,
        )

    return JsonResponse(
        {
            "success": True,
            "redirect_url": _status_url(topup.pk),
        }
    )


@login_required
@never_cache
@require_GET
def topup_status(request, pk):
    topup = get_object_or_404(
        WalletTopUp,
        pk=pk,
        user=request.user,
    )
    result = reconcile_topup(topup)
    topup = result.topup

    context = {
        "topup": topup,
        "amount": topup.amount,
        "reference": topup.receipt,
        "payment_reference": topup.provider_payment_id,
        "captured_at": topup.captured_at,
        "status_url": _status_url(topup.pk),
        "topup_url": reverse("wallet:topup_page"),
        "razorpay_enabled": bool(settings.RAZORPAY_ENABLED),
    }

    if topup.status == WalletTopUp.Status.CAPTURED:
        context["return_url"] = request.session.pop("wallet_return_url", None)
        return render(request, "wallet/topup_success.html", context)

    if topup.status in (
        WalletTopUp.Status.FAILED,
        WalletTopUp.Status.EXPIRED,
    ):
        return render(request, "wallet/topup_failed.html", context)

    # Unresolved / uncertain states: redirect to Wallet Management.
    # No customer-facing pending page exists.
    return redirect("wallet:wallet")
