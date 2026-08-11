from django.shortcuts import render
from django.contrib import messages
from .validators.signup_validator import (
    validate_full_name,
    validate_email,
    validate_password,
    validate_confirm_password,
    validate_terms,
)     


def signup(request):
    """
    Display and process the user signup page.
    """

    context = {
        "signup_data": {},
        "errors": {},
    }

    if request.method == "POST":

        signup_data = {
            "full_name": request.POST.get("full_name", "").strip(),
            "email": request.POST.get("email", "").strip().lower(),
            "password": request.POST.get("password", ""),
            "confirm_password": request.POST.get("confirm_password", ""),
            "terms": request.POST.get("terms"),
        }
        errors = {}

        full_name_error = validate_full_name(signup_data["full_name"])
        if full_name_error:
            errors["full_name"] = full_name_error

        email_error = validate_email(signup_data["email"])
        if email_error:
            errors["email"] = email_error

        password_error = validate_password(signup_data["password"])
        if password_error:
            errors["password"] = password_error

        confirm_password_error = validate_confirm_password(
            signup_data["password"],
            signup_data["confirm_password"],
        )

        if confirm_password_error:
            errors["confirm_password"] = confirm_password_error

        terms_error = validate_terms(signup_data["terms"])
        if terms_error:
            errors["terms"] = terms_error

        if errors:
            
            context = {
            "signup_data": signup_data,
            "errors": errors,
            }

            return render(
                request,
                "authentication/signup.html",
                context,
            )

        
    return render(request, "authentication/signup.html",context)


    


