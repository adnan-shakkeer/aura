import random
from datetime import timedelta

from django.shortcuts import redirect, render
from django.core.mail import send_mail
from django.contrib.auth.models import User
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.utils import timezone
from django.db import transaction

from .models import SignupOTP
from user_panel.user_profile.models import UserProfile

from .validators.signup_validator import (
    validate_full_name,
    validate_email,
    validate_password,
    validate_confirm_password,
    validate_terms,
)     

from .validators.login_validator import (
    validate_email as validate_login_email,
    validate_password as validate_login_password,
)

def generate_signup_otp(email):
    """
    Generate, store, and send a new signup OTP.
    """

    otp = str(random.randint(100000, 999999))

    expires_at = (
        timezone.now() + timedelta(minutes=5)
    )

    SignupOTP.objects.filter(
        email=email,
        is_verified=False,
    ).delete()

    SignupOTP.objects.create(
        email=email,
        code=otp,
        expires_at=expires_at,
    )

    send_mail(
        subject="AURA | Email Verification",
        message=(
            f"Your AURA verification code is: {otp}\n\n"
            "This code will expire in 5 minutes."
        ),
        from_email=None,
        recipient_list=[email],
    )

def signup_otp(request):
    """
    Display and process the signup OTP verification page.
    """

    signup_data = request.session.get("signup_data")

    if not signup_data:
        return redirect("signup")

    email = signup_data["email"]

    otp_record = SignupOTP.objects.filter(
        email=email,
        is_verified=False,
    ).first()

    error = None

    if request.method == "POST":

        entered_otp = request.POST.get(
            "otp",
            "",
        ).strip()

        if not entered_otp:

            error = "Please enter the OTP."

        elif not entered_otp.isdigit():

            error = "OTP must contain only numbers."

        elif len(entered_otp) != 6:

            error = "OTP must contain exactly 6 digits."

        else:

            otp_record = SignupOTP.objects.filter(
                email=email,
                is_verified=False,
            ).first()

            if not otp_record:

                error = "OTP not found. Please request a new OTP."

            elif timezone.now() > otp_record.expires_at:

                error = "This OTP has expired. Please request a new OTP."

            elif entered_otp != otp_record.code:

                error = "Invalid OTP. Please enter the correct OTP."

            else:

                if User.objects.filter(
                    email__iexact=signup_data["email"]
                ).exists():

                    error = "An account with this email already exists."

                else:

                    with transaction.atomic():

                        user = User.objects.create_user(
                            username=signup_data["email"],
                            email=signup_data["email"],
                            password=signup_data["password"],
                        )

                        UserProfile.objects.create(
                            user=user,
                            full_name=signup_data["full_name"],
                        )

                        otp_record.is_verified = True
                        otp_record.save()

                        otp_record.delete()

                    messages.success(
                        request,
                        "Your account has been created successfully. Please log in.",
                    )    

                    request.session.pop(
                        "signup_data",
                        None,
                    )

                    return redirect("login")

    context = {
        "email": email,
        "error": error,
        "otp_expires_at": otp_record.expires_at if otp_record else None,
    }

    return render(
        request,
        "authentication/signup_otp.html",
        context,
    )

def resend_signup_otp(request):
    """
    Generate and send a new OTP for signup email verification.
    """

    signup_data = request.session.get("signup_data")

    if not signup_data:
        return redirect("signup")

    email = signup_data["email"]

    generate_signup_otp(email)

    messages.success(
        request,
        "A new OTP has been sent to your email address.",
    )

    return redirect("signup_otp")





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

        if not errors:
            if User.objects.filter(
                email__iexact=signup_data["email"]
            ).exists():

                errors["email"] = "An account with this email already exists."

        if errors:

            context = {
                "signup_data": {
                    "full_name": signup_data["full_name"],
                    "email": signup_data["email"],
                    "terms": signup_data["terms"],
                },
                "errors": errors,
            }

            return render(
                request,
                "authentication/signup.html",
                context,
            )

        request.session["signup_data"] = {
            "full_name": signup_data["full_name"],
            "email": signup_data["email"],
            "password": signup_data["password"],
        }

        generate_signup_otp(
            signup_data["email"]
        )

        return redirect("signup_otp")

                
    return render(request, "authentication/signup.html",context)


def login(request):
    """
    Display the user login page.

    """

    context = {
        "login_data": {},
        "errors": {},
    }

    if request.method == "POST":

        login_data = {
            "email" : request.POST.get(
                "email",
                ""
            ).strip().lower(),

            "password" : request.POST.get(
                "password",
                "",
            )
        }

        errors = {}

        # =============================================
        # Validate Email
        # =============================================

        email_error = validate_login_email(
            login_data["email"]
        )

        if email_error:
            errors["email"] = email_error

        # =============================================
        # Validate Password
        # =============================================

        password_error = validate_login_password(
            login_data["password"]
        )

        if password_error:
            errors["password"] = password_error

        # =============================================
        # Authenticate User
        # =============================================

        if not errors:

            user = authenticate(
                request,
                username = login_data["email"],
                password = login_data["password"]
            )

            if user is None:

                errors["login"] = (
                    "Invalid email or password."
                )

            else:

                auth_login(
                    request,
                    user,
                )

                return redirect("home")

        # =============================================
        # Return Login Page With Errors
        # =============================================

        context = {
            "login_data": {
                "email": login_data["email"],
            },

            "errors": errors,
        }

        
    return render(
        request,
        "authentication/login.html",
        context,
    )

def logout(request):
    """
    Log out the currently authenticated user.
    """

    auth_logout(request)

    messages.success(
        request,
        "You have been logged out successfully.",
    )

    return redirect("login")