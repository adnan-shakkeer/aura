
"""User-side cart views.

These views handle HTTP requests for displaying the cart, adding variants,
updating quantities, removing items, and validating checkout eligibility.

Business rules and database mutations are delegated to cart.services.
"""

from http import HTTPStatus

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from user_panel.pricing.services import (
    price_variant,
    summarize,
    unit_price_after_offer,
)

from .services import (
    CartServiceError,
    add_variant_to_cart,
    get_cart_for_user,
    remove_cart_item,
    update_cart_item_quantity,
    validate_cart_for_checkout,
)


@login_required
def cart_view(request):
    """Display the authenticated customer's cart and its items."""
    cart = get_cart_for_user(request.user)

    items = list(
        cart.items.select_related(
            "variant__product__brand__category"
        ).order_by("added_at", "id")
    )

    line_pricings = []

    # Display flags only. The service layer rechecks eligibility
    # before allowing a cart mutation.
    for item in items:
        variant = item.variant
        product = variant.product
        brand = product.brand
        category = brand.category

        line_pricing = price_variant(variant, item.quantity)
        line_pricings.append(line_pricing)

        item.line_total = line_pricing.line_total
        item.unit_price = line_pricing.unit_price
        item.offer_percentage = line_pricing.offer_percentage
        item.offer_discount = line_pricing.offer_discount
        item.payable_total = line_pricing.paid_amount
        item.effective_unit_price = unit_price_after_offer(variant)

        item.is_available = (
            not variant.is_deleted
            and variant.is_listed
            and not product.is_deleted
            and product.is_listed
            and not brand.is_deleted
            and brand.is_listed
            and not category.is_deleted
            and category.is_listed
            and variant.stock > 0
        )

        item.is_out_of_stock = variant.stock <= 0

    totals = summarize(line_pricings)
    total_quantity = sum(item.quantity for item in items)

    context = {
        "cart": cart,
        "items": items,
        "subtotal": totals["subtotal"],
        "offer_discount_total": totals["offer_discount_total"],
        "estimated_total": totals["total"],
        "total_quantity": total_quantity,
    }

    return render(request, "cart/cart.html", context)


@login_required
@require_POST
def add_to_cart_view(request):
    """Add a product variant to the authenticated customer's cart."""
    variant_id = request.POST.get("variant_id")
    quantity = request.POST.get("quantity", "1")

    if not variant_id:
        return JsonResponse(
            {
                "success": False,
                "message": "Please select a product variant.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        item = add_variant_to_cart(
            user=request.user,
            variant_id=variant_id,
            quantity=quantity,
        )
    except CartServiceError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    cart = item.cart

    return JsonResponse(
        {
            "success": True,
            "message": "Item added to your bag.",
            "item_id": item.id,
            "variant_id": item.variant_id,
            "quantity": item.quantity,
            "cart_item_count": cart.items.count(),
            "total_quantity": (
                cart.items.aggregate(total=Sum("quantity"))["total"] or 0
            ),
        },
        status=HTTPStatus.OK,
    )


@login_required
@require_POST
def update_cart_item_view(request, item_id):
    """Update a cart item's quantity when its variant is eligible."""
    quantity = request.POST.get("quantity")

    if quantity is None:
        return JsonResponse(
            {
                "success": False,
                "message": "Please provide a quantity.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        item = update_cart_item_quantity(
            user=request.user,
            item_id=item_id,
            quantity=quantity,
        )
    except CartServiceError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    cart = item.cart

    cart_items = list(
        cart.items.select_related(
            "variant__product__brand__category"
        )
    )
    line_pricings = []
    updated_pricing = None

    for cart_item in cart_items:
        line_pricing = price_variant(
            cart_item.variant,
            cart_item.quantity,
        )
        line_pricings.append(line_pricing)

        if cart_item.id == item.id:
            updated_pricing = line_pricing

    totals = summarize(line_pricings)

    return JsonResponse(
        {
            "success": True,
            "message": "Cart quantity updated.",
            "item_id": item.id,
            "quantity": item.quantity,
            "line_total": str(updated_pricing.line_total),
            "payable_total": str(updated_pricing.paid_amount),
            "offer_percentage": str(updated_pricing.offer_percentage),
            "offer_discount": str(updated_pricing.offer_discount),
            "subtotal": str(totals["subtotal"]),
            "offer_discount_total": str(
                totals["offer_discount_total"]
            ),
            "total": str(totals["total"]),
            "total_quantity": (
                cart.items.aggregate(total=Sum("quantity"))["total"] or 0
            ),
        },
        status=HTTPStatus.OK,
    )


@login_required
@require_POST
def remove_cart_item_view(request, item_id):
    """Remove an item from the authenticated customer's cart."""
    try:
        remove_cart_item(
            user=request.user,
            item_id=item_id,
        )
    except CartServiceError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    cart = get_cart_for_user(request.user)

    return JsonResponse(
        {
            "success": True,
            "message": "Item removed from your bag.",
            "cart_item_count": cart.items.count(),
            "total_quantity": (
                cart.items.aggregate(total=Sum("quantity"))["total"] or 0
            ),
        },
        status=HTTPStatus.OK,
    )


@login_required
@require_POST
def validate_checkout_view(request):
    """Check cart eligibility and stock before proceeding to checkout."""
    try:
        problems = validate_cart_for_checkout(request.user)
    except CartServiceError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    return JsonResponse(
        {
            "success": not problems,
            "message": (
                "Your cart is ready for checkout."
                if not problems
                else "Please resolve the following cart issues."
            ),
            "problems": problems,
        },
        status=HTTPStatus.OK,
    )
