"""Customer-facing checkout review and order-placement views.

The review view renders the authoritative order review produced by
``orders.services.review_checkout`` and lets the customer pick one of their
saved delivery addresses. The placement view submits the reviewed cart through
``orders.services.place_order``.

The signed review token is kept in the server-side session only. It is never
placed in a URL, rendered into the page, or written to client-side storage.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from admin_panel.products.models import ProductVariant
from user_panel.address.models import Address
from user_panel.orders.services import (
    REASON_ADDRESS_CHANGED,
    REASON_CART_CHANGED,
    REASON_PRICE_CHANGED,
    STATUS_DUPLICATE,
    STATUS_REVIEW_REQUIRED,
    EmptyCartError,
    ExpiredReviewTokenError,
    InsufficientStockError,
    InvalidAddressError,
    InvalidQuantityError,
    InvalidReviewTokenError,
    OrderServiceError,
    VariantUnavailableError,
    place_order,
    review_checkout,
)


REVIEW_SESSION_KEY = "checkout_review_token"
REVIEW_ADDRESS_SESSION_KEY = "checkout_review_address_id"
REVIEW_SUBMISSION_SESSION_KEY = "checkout_review_submission_id"

_REVIEW_SESSION_KEYS = (
    REVIEW_SESSION_KEY,
    REVIEW_ADDRESS_SESSION_KEY,
    REVIEW_SUBMISSION_SESSION_KEY,
)

_REVIEW_REQUIRED_MESSAGES = {
    REASON_PRICE_CHANGED: (
        "Prices have been updated. Please review your order again."
    ),
    REASON_CART_CHANGED: (
        "Your bag has changed. Please review your order again."
    ),
    REASON_ADDRESS_CHANGED: (
        "Your delivery address has changed. Please review your order again."
    ),
}


def _owned_address(user, raw_address_id):
    """Return a live address owned by ``user`` or ``None``."""
    if raw_address_id in (None, "") or isinstance(raw_address_id, bool):
        return None

    try:
        address_pk = int(raw_address_id)
    except (TypeError, ValueError):
        return None

    if address_pk < 1:
        return None

    return Address.objects.filter(
        pk=address_pk,
        user=user,
        is_deleted=False,
    ).first()


def _default_address(addresses):
    for address in addresses:
        if address.is_default:
            return address
    return addresses[0] if addresses else None


def _line_variant_details(review):
    """Map variant id to its catalogue image and fragrance concentration."""
    variant_ids = [line.variant_id for line in review.lines]
    image_by_variant = {}
    concentration_by_variant = {}

    if not variant_ids:
        return image_by_variant, concentration_by_variant

    variants = ProductVariant.objects.filter(
        pk__in=variant_ids
    ).select_related("product").prefetch_related("images")

    for variant in variants:
        images = list(variant.images.all())
        image_by_variant[variant.pk] = images[0] if images else None

        product = getattr(variant, "product", None)
        if product and getattr(product, "fragrance_concentration", None):
            concentration_by_variant[variant.pk] = (
                product.get_fragrance_concentration_display()
            )
        else:
            concentration_by_variant[variant.pk] = ""

    return image_by_variant, concentration_by_variant


def _line_images(review):
    """Map variant id to its first catalogue image for the item list."""
    images, _ = _line_variant_details(review)
    return images


def _clear_review_session(request):
    for key in _REVIEW_SESSION_KEYS:
        request.session.pop(key, None)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def checkout_review(request):
    user = request.user

    addresses = list(
        Address.objects.filter(
            user=user,
            is_deleted=False,
        ).order_by("-is_default", "-created_at")
    )

    address_issue = ""
    selected_address = None

    if request.method == "POST":
        selected_address = _owned_address(user, request.POST.get("address_id"))
        if selected_address is None:
            address_issue = "Please choose a valid delivery address."
        else:
            return redirect(
                f"{reverse('checkout:checkout_review')}"
                f"?address={selected_address.pk}"
            )
    else:
        raw_address_id = request.GET.get("address")
        if raw_address_id:
            selected_address = _owned_address(user, raw_address_id)
            if selected_address is None:
                address_issue = (
                    "The selected delivery address is no longer available. "
                    "Please choose another address."
                )

    if selected_address is None:
        selected_address = _default_address(addresses)

    context = {
        "addresses": addresses,
        "selected_address": selected_address,
        "address_issue": address_issue,
        "no_addresses": not addresses,
        "next_url": request.get_full_path(),
        "review": None,
        "line_rows": [],
        "total_quantity": 0,
        "empty_cart": False,
        "cart_issue": "",
        "cart_issue_is_address": False,
        "address_type_choices": Address.ADDRESS_TYPE_CHOICES,
    }

    if selected_address is None:
        _clear_review_session(request)
        return render(request, "checkout/checkout.html", context)

    try:
        review = review_checkout(user, selected_address.pk)
    except EmptyCartError:
        _clear_review_session(request)
        context["empty_cart"] = True
        return render(request, "checkout/checkout.html", context)
    except (VariantUnavailableError, InsufficientStockError, InvalidQuantityError) as exc:
        _clear_review_session(request)
        context["cart_issue"] = str(exc)
        return render(request, "checkout/checkout.html", context)
    except InvalidAddressError as exc:
        _clear_review_session(request)
        context["address_issue"] = str(exc)
        context["cart_issue_is_address"] = True
        return render(request, "checkout/checkout.html", context)
    except OrderServiceError:
        _clear_review_session(request)
        context["cart_issue"] = (
            "We could not review your bag right now. Please try again."
        )
        return render(request, "checkout/checkout.html", context)

    request.session[REVIEW_SESSION_KEY] = review.token
    request.session[REVIEW_ADDRESS_SESSION_KEY] = selected_address.pk
    request.session[REVIEW_SUBMISSION_SESSION_KEY] = review.submission_id

    image_by_variant, concentration_by_variant = _line_variant_details(review)

    context["review"] = review
    context["line_rows"] = [
        {
            "line": line,
            "image": image_by_variant.get(line.variant_id),
            "concentration": concentration_by_variant.get(line.variant_id, ""),
        }
        for line in review.lines
    ]
    context["total_quantity"] = sum(
        line.quantity for line in review.lines
    )

    return render(request, "checkout/checkout.html", context)


@login_required
@never_cache
@require_POST
def place_order_submit(request):
    """Place the reviewed cart as a COD order.

    The signed review token and selected address are read exclusively from the
    server-side session. On success or duplicate we deliberately keep the
    review state so a retried request can recover the same order through the
    service's submission-id idempotency even if the first HTTP response was
    lost.
    """
    review_token = request.session.get(REVIEW_SESSION_KEY)
    address_id = request.session.get(REVIEW_ADDRESS_SESSION_KEY)

    if not review_token or not address_id:
        messages.error(
            request,
            "Please review your order before placing it.",
        )
        return redirect("checkout:checkout_review")

    try:
        result = place_order(request.user, address_id, review_token)
    except InvalidReviewTokenError:
        _clear_review_session(request)
        messages.error(
            request,
            "Your checkout review is no longer valid. "
            "Please review your bag and try again.",
        )
        return redirect("checkout:checkout_review")
    except ExpiredReviewTokenError:
        _clear_review_session(request)
        messages.error(
            request,
            "Your checkout session expired. "
            "Please review your bag and try again.",
        )
        return redirect("checkout:checkout_review")
    except EmptyCartError:
        _clear_review_session(request)
        messages.error(request, "Your bag is empty.")
        return redirect("checkout:checkout_review")
    except InvalidAddressError as exc:
        _clear_review_session(request)
        messages.error(request, str(exc))
        return redirect("checkout:checkout_review")
    except (
        VariantUnavailableError,
        InsufficientStockError,
        InvalidQuantityError,
    ) as exc:
        _clear_review_session(request)
        messages.error(request, str(exc))
        return redirect("checkout:checkout_review")
    except OrderServiceError:
        _clear_review_session(request)
        messages.error(
            request,
            "We could not place your order. Please try again.",
        )
        return redirect("checkout:checkout_review")

    if result.status == STATUS_REVIEW_REQUIRED:
        review = result.review
        if review is not None:
            request.session[REVIEW_SESSION_KEY] = review.token
            request.session[REVIEW_ADDRESS_SESSION_KEY] = review.address_id
            request.session[REVIEW_SUBMISSION_SESSION_KEY] = (
                review.submission_id
            )

        messages.warning(
            request,
            _REVIEW_REQUIRED_MESSAGES.get(
                result.reason,
                "Please review your order again.",
            ),
        )

        checkout_url = reverse("checkout:checkout_review")
        if review is not None:
            checkout_url = f"{checkout_url}?address={review.address_id}"
        return redirect(checkout_url)

    order = result.order
    if order is None:
        _clear_review_session(request)
        messages.error(
            request,
            "We could not place your order. Please try again.",
        )
        return redirect("checkout:checkout_review")

    if result.status == STATUS_DUPLICATE:
        if result.recovered:
            messages.info(
                request,
                "Your order was already placed. "
                "Showing your existing confirmation.",
            )
        else:
            messages.info(
                request,
                "This order has already been placed.",
            )

    return redirect(
        "orders:order_confirmation",
        order_number=order.order_number,
    )
