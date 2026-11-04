"""Customer-facing checkout review and order-placement views.

Cart checkout and direct Buy Now checkout share the same address-selection and
rendering helpers but use separate signed review tokens and session keys. Direct
checkout never reads, locks or clears the customer's cart.

The signed review token is kept in the server-side session only. It is never
placed in a URL, rendered into the page, or written to client-side storage.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from admin_panel.products.models import ProductVariant
from user_panel.address.models import Address
from user_panel.orders.services import (
    REASON_ADDRESS_CHANGED,
    REASON_CART_CHANGED,
    REASON_PRICE_CHANGED,
    REASON_SELECTION_CHANGED,
    REVIEW_TOKEN_MAX_AGE,
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
    place_direct_order,
    place_order,
    review_checkout,
    review_direct_checkout,
    validate_direct_purchase,
)


REVIEW_SESSION_KEY = "checkout_review_token"
REVIEW_ADDRESS_SESSION_KEY = "checkout_review_address_id"
REVIEW_SUBMISSION_SESSION_KEY = "checkout_review_submission_id"

DIRECT_INTENT_SESSION_KEY = "checkout_direct_intent"
DIRECT_REVIEW_SESSION_KEY = "checkout_direct_review_token"
DIRECT_REVIEW_ADDRESS_SESSION_KEY = "checkout_direct_review_address_id"
DIRECT_REVIEW_SUBMISSION_SESSION_KEY = (
    "checkout_direct_review_submission_id"
)

_REVIEW_SESSION_KEYS = (
    REVIEW_SESSION_KEY,
    REVIEW_ADDRESS_SESSION_KEY,
    REVIEW_SUBMISSION_SESSION_KEY,
)

_DIRECT_REVIEW_SESSION_KEYS = (
    DIRECT_REVIEW_SESSION_KEY,
    DIRECT_REVIEW_ADDRESS_SESSION_KEY,
    DIRECT_REVIEW_SUBMISSION_SESSION_KEY,
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

_DIRECT_REVIEW_REQUIRED_MESSAGES = {
    REASON_PRICE_CHANGED: (
        "Prices have been updated. Please review your Buy Now selection again."
    ),
    REASON_SELECTION_CHANGED: (
        "Your Buy Now selection has changed. "
        "Please review it again before placing your order."
    ),
    REASON_ADDRESS_CHANGED: (
        "Your delivery address has changed. "
        "Please review your Buy Now selection again."
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


def _clear_direct_review_session(request):
    for key in _DIRECT_REVIEW_SESSION_KEYS:
        request.session.pop(key, None)


def _clear_direct_intent(request):
    request.session.pop(DIRECT_INTENT_SESSION_KEY, None)


def _clear_direct_session(request):
    _clear_direct_review_session(request)
    _clear_direct_intent(request)


def _get_direct_intent(request):
    """Return the current Buy Now intent or ``None`` when missing/expired."""
    intent = request.session.get(DIRECT_INTENT_SESSION_KEY)

    if not isinstance(intent, dict):
        return None

    variant_id = intent.get("variant_id")
    quantity = intent.get("quantity", 1)
    issued_at = intent.get("iat")
    product_slug = intent.get("product_slug", "")

    if (
        not isinstance(variant_id, int)
        or isinstance(variant_id, bool)
        or variant_id < 1
        or not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity < 1
        or not isinstance(issued_at, (int, float))
        or isinstance(issued_at, bool)
        or not isinstance(product_slug, str)
    ):
        _clear_direct_session(request)
        return None

    if timezone.now().timestamp() - issued_at > REVIEW_TOKEN_MAX_AGE:
        _clear_direct_session(request)
        return None

    return {
        "variant_id": variant_id,
        "quantity": quantity,
        "product_slug": product_slug,
    }


def _product_or_shop_url(product_slug):
    if product_slug:
        return reverse("shop:product_details", args=[product_slug])
    return reverse("shop:shop_list")


def _direct_back_url(intent):
    return _product_or_shop_url((intent or {}).get("product_slug"))


def _select_checkout_address(request, user, *, review_url_name):
    """Resolve the address selection shared by cart and direct checkout."""
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
            return (
                addresses,
                selected_address,
                address_issue,
                f"{reverse(review_url_name)}?address={selected_address.pk}",
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

    return addresses, selected_address, address_issue, None


def _base_checkout_context(
    request,
    addresses,
    selected_address,
    address_issue,
    *,
    review_url_name,
    place_url_name,
    checkout_mode,
    back_url,
    back_label,
    issue_url,
    issue_label,
):
    review_url = reverse(review_url_name)

    return {
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
        "checkout_mode": checkout_mode,
        "address_form_action": review_url,
        "place_order_action": reverse(place_url_name),
        "checkout_review_url": review_url,
        "back_url": back_url,
        "back_label": back_label,
        "issue_url": issue_url,
        "issue_label": issue_label,
    }


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def checkout_review(request):
    user = request.user
    _clear_direct_session(request)

    addresses, selected_address, address_issue, redirect_url = (
        _select_checkout_address(
            request,
            user,
            review_url_name="checkout:checkout_review",
        )
    )

    if redirect_url:
        return redirect(redirect_url)

    context = _base_checkout_context(
        request,
        addresses,
        selected_address,
        address_issue,
        review_url_name="checkout:checkout_review",
        place_url_name="checkout:place_order",
        checkout_mode="cart",
        back_url=reverse("cart:cart_view"),
        back_label="Back to Shopping Bag",
        issue_url=reverse("cart:cart_view"),
        issue_label="Review Shopping Bag",
    )

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


@never_cache
@require_POST
def buy_now_start(request):
    """Validate a Buy Now selection and start a direct checkout intent."""
    _clear_direct_session(request)

    variant_id = request.POST.get("variant_id")
    fallback_url = _product_or_shop_url(
        request.POST.get("product_slug", "").strip()
    )

    try:
        variant = validate_direct_purchase(variant_id, 1)
    except OrderServiceError as exc:
        messages.error(request, str(exc))
        return redirect(fallback_url)

    request.session[DIRECT_INTENT_SESSION_KEY] = {
        "variant_id": variant.pk,
        "quantity": 1,
        "product_slug": variant.product.slug,
        "iat": int(timezone.now().timestamp()),
    }

    review_url = reverse("checkout:buy_now_review")

    if not request.user.is_authenticated:
        login_url = reverse("user_auth:login")
        return redirect(f"{login_url}?{urlencode({'next': review_url})}")

    return redirect(review_url)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def buy_now_review(request):
    """Review one selected variant without reading or changing the cart."""
    user = request.user
    intent = _get_direct_intent(request)

    if intent is None:
        messages.error(
            request,
            "Your Buy Now selection has expired. "
            "Please choose a product again.",
        )
        return redirect("shop:shop_list")

    back_url = _direct_back_url(intent)
    back_label = (
        "Back to Product"
        if intent.get("product_slug")
        else "Back to Shop"
    )

    addresses, selected_address, address_issue, redirect_url = (
        _select_checkout_address(
            request,
            user,
            review_url_name="checkout:buy_now_review",
        )
    )

    if redirect_url:
        return redirect(redirect_url)

    context = _base_checkout_context(
        request,
        addresses,
        selected_address,
        address_issue,
        review_url_name="checkout:buy_now_review",
        place_url_name="checkout:buy_now_place",
        checkout_mode="direct",
        back_url=back_url,
        back_label=back_label,
        issue_url=back_url,
        issue_label=back_label,
    )

    if selected_address is None:
        _clear_direct_review_session(request)
        return render(request, "checkout/checkout.html", context)

    try:
        review = review_direct_checkout(
            user,
            intent["variant_id"],
            intent["quantity"],
            selected_address.pk,
        )
    except (VariantUnavailableError, InsufficientStockError, InvalidQuantityError) as exc:
        _clear_direct_review_session(request)
        context["cart_issue"] = str(exc)
        return render(request, "checkout/checkout.html", context)
    except InvalidAddressError as exc:
        _clear_direct_review_session(request)
        context["address_issue"] = str(exc)
        context["cart_issue_is_address"] = True
        return render(request, "checkout/checkout.html", context)
    except OrderServiceError:
        _clear_direct_review_session(request)
        context["cart_issue"] = (
            "We could not review your Buy Now selection right now. "
            "Please try again."
        )
        return render(request, "checkout/checkout.html", context)

    request.session[DIRECT_REVIEW_SESSION_KEY] = review.token
    request.session[DIRECT_REVIEW_ADDRESS_SESSION_KEY] = selected_address.pk
    request.session[DIRECT_REVIEW_SUBMISSION_SESSION_KEY] = (
        review.submission_id
    )

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
def buy_now_place_order(request):
    """Place the reviewed Buy Now selection as a COD order."""
    review_token = request.session.get(DIRECT_REVIEW_SESSION_KEY)
    address_id = request.session.get(DIRECT_REVIEW_ADDRESS_SESSION_KEY)

    if not review_token or not address_id:
        messages.error(
            request,
            "Please review your Buy Now selection before placing it.",
        )
        return redirect("checkout:buy_now_review")

    try:
        result = place_direct_order(request.user, address_id, review_token)
    except InvalidReviewTokenError:
        _clear_direct_review_session(request)
        messages.error(
            request,
            "Your Buy Now review is no longer valid. "
            "Please review your selection and try again.",
        )
        return redirect("checkout:buy_now_review")
    except ExpiredReviewTokenError:
        _clear_direct_review_session(request)
        messages.error(
            request,
            "Your Buy Now session expired. "
            "Please review your selection and try again.",
        )
        return redirect("checkout:buy_now_review")
    except InvalidAddressError as exc:
        _clear_direct_review_session(request)
        messages.error(request, str(exc))
        return redirect("checkout:buy_now_review")
    except (
        VariantUnavailableError,
        InsufficientStockError,
        InvalidQuantityError,
    ) as exc:
        _clear_direct_review_session(request)
        messages.error(request, str(exc))
        return redirect("checkout:buy_now_review")
    except OrderServiceError:
        _clear_direct_review_session(request)
        messages.error(
            request,
            "We could not place your Buy Now order. Please try again.",
        )
        return redirect("checkout:buy_now_review")

    if result.status == STATUS_REVIEW_REQUIRED:
        review = result.review
        if review is not None:
            request.session[DIRECT_REVIEW_SESSION_KEY] = review.token
            request.session[DIRECT_REVIEW_ADDRESS_SESSION_KEY] = (
                review.address_id
            )
            request.session[DIRECT_REVIEW_SUBMISSION_SESSION_KEY] = (
                review.submission_id
            )

        messages.warning(
            request,
            _DIRECT_REVIEW_REQUIRED_MESSAGES.get(
                result.reason,
                "Please review your Buy Now selection again.",
            ),
        )

        review_url = reverse("checkout:buy_now_review")
        if review is not None:
            review_url = f"{review_url}?address={review.address_id}"
        return redirect(review_url)

    order = result.order
    if order is None:
        _clear_direct_review_session(request)
        messages.error(
            request,
            "We could not place your Buy Now order. Please try again.",
        )
        return redirect("checkout:buy_now_review")

    _clear_direct_intent(request)

    if result.status == STATUS_DUPLICATE:
        if result.recovered:
            messages.info(
                request,
                "Your Buy Now order was already placed. "
                "Showing your existing confirmation.",
            )
        else:
            messages.info(
                request,
                "This Buy Now order has already been placed.",
            )

    return redirect(
        "orders:order_confirmation",
        order_number=order.order_number,
    )


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
