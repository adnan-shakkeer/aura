"""Views for the user-side wishlist.

The page view is authenticated and server-rendered. The JSON list endpoint
remains available under ``/wishlist/items/``. Mutations are POST-only and
authenticated, matching the cart conventions.
"""

from http import HTTPStatus

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .services import (
    WishlistServiceError,
    add_to_wishlist,
    get_wishlist_count,
    get_wishlist_items,
    remove_from_wishlist,
    remove_wishlist_item,
)


def _serialize_item(item):
    """Return a JSON-safe representation of a wishlist entry."""
    variant = item.variant

    if variant is not None:
        product = variant.product
        brand = product.brand
        product_name = product.name
        brand_name = brand.name
        variant_size = variant.size
        price = str(variant.price)
        product_slug = product.slug
    else:
        product_name = item.product_name
        brand_name = item.brand_name
        variant_size = item.variant_size
        price = None
        product_slug = None

    return {
        "id": item.id,
        "variant_id": item.variant_id,
        "product_name": product_name,
        "brand_name": brand_name,
        "variant_size": variant_size,
        "price": price,
        "product_slug": product_slug,
        "added_at": item.added_at.isoformat(),
        "is_available": item.is_available,
        "is_out_of_stock": item.is_out_of_stock,
        "is_unavailable": item.is_unavailable,
    }


@login_required
@require_GET
def wishlist_page(request):
    """Render the authenticated customer's wishlist page."""
    items = get_wishlist_items(request.user)

    return render(
        request,
        "wishlist/wishlist.html",
        {
            "wishlist_items": items,
        },
    )


@login_required
@require_GET
def wishlist_view(request):
    """Return the authenticated customer's wishlist as JSON."""
    items = get_wishlist_items(request.user)

    return JsonResponse(
        {
            "success": True,
            "count": len(items),
            "items": [_serialize_item(item) for item in items],
        },
        status=HTTPStatus.OK,
    )


@login_required
@require_POST
def add_to_wishlist_view(request):
    """Add a product variant to the authenticated customer's wishlist."""
    variant_id = request.POST.get("variant_id")

    if variant_id in (None, ""):
        return JsonResponse(
            {
                "success": False,
                "message": "Please select a product variant.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        item, created = add_to_wishlist(
            user=request.user,
            variant_id=variant_id,
        )
    except WishlistServiceError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Added to your wishlist."
                if created
                else "This variant is already in your wishlist."
            ),
            "item_id": item.id,
            "variant_id": item.variant_id,
            "created": created,
            "wishlisted": True,
            "wishlist_count": get_wishlist_count(request.user),
        },
        status=HTTPStatus.OK,
    )


@login_required
@require_POST
def remove_from_wishlist_view(request):
    """Remove a product variant from the authenticated customer's wishlist."""
    variant_id = request.POST.get("variant_id")

    if variant_id in (None, ""):
        return JsonResponse(
            {
                "success": False,
                "message": "Please select a product variant.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        removed = remove_from_wishlist(
            user=request.user,
            variant_id=variant_id,
        )
    except WishlistServiceError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Removed from your wishlist."
                if removed
                else "This variant is not in your wishlist."
            ),
            "variant_id": int(variant_id),
            "removed": removed,
            "wishlisted": False,
            "wishlist_count": get_wishlist_count(request.user),
        },
        status=HTTPStatus.OK,
    )


@login_required
@require_POST
def remove_wishlist_item_view(request):
    """Remove one of the authenticated customer's wishlist entries by ID."""
    item_id = request.POST.get("item_id")

    if item_id in (None, ""):
        return JsonResponse(
            {
                "success": False,
                "message": "Please select a wishlist item.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        removed = remove_wishlist_item(
            user=request.user,
            item_id=item_id,
        )
    except WishlistServiceError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Removed from your wishlist."
                if removed
                else "This wishlist item was not found."
            ),
            "item_id": int(item_id),
            "removed": removed,
            "wishlist_count": get_wishlist_count(request.user),
        },
        status=HTTPStatus.OK,
    )
