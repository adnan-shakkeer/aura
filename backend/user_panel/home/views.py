from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from http import HTTPStatus



def home(request):
    """
    Display the AURA home page.
    """

    return render(
        request,
        "home/home.html",
    )




# Create your views here.
