from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from http import HTTPStatus

from .services import get_featured_products, get_new_arrivals



def home(request):
    """

        Display the AURA home page.

    Featured Perfumes: newest 4 eligible products.
    New Arrivals: next 4 eligible products (offset 4).
    Best Sellers: intentionally not passed (static fallback until
    Order/OrderItem sales architecture exists).
    """

    return render(
        request,
        "home/home.html",
        {
            "featured_products": get_featured_products(),
            "new_arrivals": get_new_arrivals(),
        },
    )




# Create your views here.
