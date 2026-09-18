import random
import re
from datetime import timedelta

from PIL import Image, UnidentifiedImageError

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone


from http import HTTPStatus
from django.contrib.auth import update_session_auth_hash
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password



from .models import UserProfile, EmailChangeOTP

from user_panel.authentication.validators.signup_validator import (
    validate_email,
)

from allauth.account.models import EmailAddress

from django.http import JsonResponse
from django.db import DatabaseError
from smtplib import SMTPException


@login_required
def profile(request):

    profile = get_object_or_404(
        UserProfile,
        user=request.user
    )

    context = {
        "profile" : profile,
        "user" : request.user,
    }

    return render(
        request,
        "user_profile/profile.html",
        context
    )

@login_required
def send_email_change_otp(request):
    """
    Send an OTP to the new email address for email change verification.
    """
    if request.method != "POST":
        return JsonResponse(
            {
                "success": False,
                "message": "Invalid request method.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Google-only accounts cannot change their email.
    if not request.user.has_usable_password():
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Email changes are not available "
                    "for Google-only accounts."
                ),
            },
            status=HTTPStatus.FORBIDDEN,
        )

    new_email = request.POST.get(
        "new_email",
        "",
    ).strip().lower()

    # Validate email format.
    email_error = validate_email(new_email)

    if email_error:
        return JsonResponse(
            {
                "success": False,
                "message": email_error,
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Prevent changing to the current email.
    if new_email == request.user.email.lower():

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "The new email address must be different "
                    "from your current email."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Prevent using an email already registered by another account.
    if User.objects.filter(
        email__iexact=new_email,
    ).exclude(
        pk=request.user.pk,
    ).exists():
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "An account with this email already exists."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Generate a 6-digit OTP.
    otp = str(
        random.randint(
            100000,
            999999,
        )
    )

    expires_at = (
        timezone.now() + timedelta(minutes=5)
    )

    try:
        # Remove any previous OTP for this user.
        EmailChangeOTP.objects.filter(
            user=request.user,
        ).delete()


        # Store the new OTP.
        EmailChangeOTP.objects.create(
            user=request.user,
            new_email=new_email,
            code=otp,
            expires_at=expires_at,
        )

        email_sent = send_mail(
            subject="AURA | Email Change Verification",
            message=(
                f"Your AURA email change verification code is: {otp}\n\n"
                "This code will expire in 5 minutes."
            ),
            from_email=None,
            recipient_list=[new_email],
        )

        # send_mail() returns 0 when no email was sent.
        if email_sent == 0:

            EmailChangeOTP.objects.filter(
                user=request.user,
            ).delete()

            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "We couldn't send the verification code. "
                        "Please try again."
                    ),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    except SMTPException:

        EmailChangeOTP.objects.filter(
            user=request.user,
        ).delete()

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "We couldn't send the verification code. "
                    "Please try again."
                ),
            },
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    except DatabaseError:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "We couldn't process your request. "
                    "Please try again."
                ),
            },
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "A verification code has been sent "
                "to your new email address."
            ),
        },
        status=200,
    )

@login_required
def resend_email_change_otp(request):
    """
    Generate and send a new OTP for the pending email change.
    """

    if request.method != "POST":
        return JsonResponse(
            {
                "success": False,
                "message": "Invalid request method.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Google-only accounts cannot change their email.
    if not request.user.has_usable_password():
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Email changes are not available "
                    "for Google-only accounts."
                ),
            },
            status=HTTPStatus.FORBIDDEN,
        )

    # Get the existing pending email change OTP.
    otp_record = (
        EmailChangeOTP.objects.filter(
            user=request.user,
        ).first()
    )

    if not otp_record:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "No pending email change was found. "
                    "Please enter your new email address again."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    new_otp = str(random.randint(100000, 999999))
    expires_at = timezone.now() + timedelta(minutes=5)

    otp_record.code = new_otp
    otp_record.expires_at = expires_at

    try:

        otp_record.save(
            update_fields=[
                "code",
                "expires_at",
            ]
        )

        email_sent = send_mail(
            subject="Your AURA Email Change Verification Code",
            message=(
                f"Your new AURA verification code is: {new_otp}\n\n"
                "This code will expire in 5 minutes."
            ),
            from_email=None,
            recipient_list=[otp_record.new_email],
            fail_silently=False,
        )

        if email_sent == 0:
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "We couldn't send the verification code. "
                        "Please try again."
                    ),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    except SMTPException:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "We couldn't send the verification code. "
                    "Please try again."
                ),
            },
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    except DatabaseError:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "We couldn't process your request. "
                    "Please try again."
                ),
            },
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "A new verification code has been sent "
                "to your new email address."
            ),
        },
        status=200,
    )


def verify_email_change_otp(request):
    """
    Verify the OTP and change the user's email address.
    """

    if request.method != "POST":
        return JsonResponse(
            {
                "success": False,
                "message": "Invalid request method.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Google-only accounts cannot change their email.
    if not request.user.has_usable_password():
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Email changes are not available "
                    "for Google-only accounts."
                ),
            },
            status=HTTPStatus.FORBIDDEN,
        )

    otp_code = request.POST.get(
        "otp",
        ""
    ).strip()

    if not otp_code:
        return JsonResponse(
            {
                "success": False,
                "message": "Please enter the verification code.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Get the latest OTP for this user.
    otp_record = (
        EmailChangeOTP.objects.filter(
            user=request.user,
        ).first()
    )

    if not otp_record:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "No active verification code was found. "
                    "Please request a new code."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Check whether the OTP has expired.
    if timezone.now() >= otp_record.expires_at:
        otp_record.delete()

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "This verification code has expired. "
                    "Please request a new code."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    if otp_code != otp_record.code:
        return JsonResponse(
            {
                "success" : False,
                "message": "Invalid verification code.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    new_email = otp_record.new_email


    # Make sure the new email has not been registered
    # by another account while the OTP was active.
    if User.objects.filter(
        email__iexact=new_email,
    ).exclude(
        pk=request.user.pk,
    ).exists():

        otp_record.delete()

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "An account with this email already exists."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    old_email = request.user.email

    try:

        # Update Django User email.
        request.user.email = new_email
        request.user.save(
            update_fields=["email"],
        )

        # Synchronize allauth EmailAddress.
        EmailAddress.objects.filter(
            user=request.user,
        ).update(
            email=new_email,
            primary=True,
            verified=True,
        )

        # Delete the used OTP.
        otp_record.delete()

    except Exception:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "We couldn't change your email address. "
                    "Please try again."
                ),
            },
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Your email address has been changed successfully."
            ),
            "email": new_email,
        },
        status=200,
    )

@login_required
def change_password(request):
    """
    Change the authenticated user's password.
    """
    if request.method != "POST":
        return JsonResponse(
            {
                "success" : False,
                "message" : "Invalid request method.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Google-only accounts do not have a usable password.
    if not request.user.has_usable_password():
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Password changes are not available "
                    "for Google-only accounts."
                ),
            },
            status=HTTPStatus.FORBIDDEN,
        )

    current_password = request.POST.get(
        "current_password",
        "",
    )

    new_password = request.POST.get(
        "new_password",
        "",
    )

    confirm_password = request.POST.get(
        "confirm_password",
        "",
    )

    # Check required fields.
    if not current_password:
        return JsonResponse(
            {
                "success" : False,
                "message" : "Please enter your current password.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    if not new_password:
            return JsonResponse(
                {
                    "success" : False,
                    "message" : "Please enter a new password.",
                },
                status=HTTPStatus.BAD_REQUEST,
            )

    if not confirm_password:
            return JsonResponse(
                {
                    "success" : False,
                    "message" : "Please confirm your new password.",
                },
                status=HTTPStatus.BAD_REQUEST,
            )

    # Check whether both new passwords match.
    if new_password != confirm_password:
        return JsonResponse(
            {
                "success": False,
                "message" : (
                    "New password and confirmation password "
                    "do not match."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Verify the current password.
    if not request.user.check_password(current_password):
        return JsonResponse(
            {
                "success" : False,
                "message": "Your current password is incorrect.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Prevent reusing the current password.
    if request.user.check_password(new_password):
        return JsonResponse(
            {
                "success" : False,
                "message" : (
                    "Your new password must be different "
                    "from your current password."
                ),
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    # Apply Django's configured password validators.
    try:

        validate_password(
            new_password,
            request.user,
        )

    except ValidationError as error:
        return JsonResponse(
            {
                "success": False,
                # Use list(error.messages)[0] to safely extract the first error string
                "message": list(error.messages)[0],
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    try:

        # Django securely hashes the new password.
        request.user.set_password(new_password)

        request.user.save(
            update_fields=["password"],
        )

        # Keep the current user logged in after changing
        # the password.
        update_session_auth_hash(
            request,
            request.user,
        )

    except DatabaseError:
        return JsonResponse(
            {
                "success" : False,
                "message" : (
                    "We couldn't change your password. "
                    "Please try again."
                ),
            },
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Your password has been changed successfully."
            ),
        },
        status=HTTPStatus.OK,
    )



    











@login_required
def edit_profile(request):



    profile = get_object_or_404(
        UserProfile,
        user=request.user,
    )

    if request.method == "POST":

        # Get submitted values
        full_name = request.POST.get("full_name", "").strip()
        mobile_number = request.POST.get("mobile_number", "").strip()
        profile_image = request.FILES.get("profile_image")
        remove_profile_image = request.POST.get("remove_profile_image")

        errors = {}


        # Full name validation

        if not full_name:

            errors["full_name"] = "Full name is required."

        elif len(full_name) > 100:

            errors["full_name"] = (
                "Full name must not exceed 100 characters."
            )

        elif not re.fullmatch(r"[A-Za-z ]+", full_name):

            errors["full_name"] = (
                "Full name can contain only letters and spaces."
            )


        # Mobile number validation

        if mobile_number:

            if not re.fullmatch(
                r"[6-9][0-9]{9}",
                mobile_number,
            ):

                errors["mobile_number"] = (
                    "Enter a valid 10-digit mobile number."
                )


        # Profile image validation

        if profile_image:

            if profile_image.size > 2 * 1024 * 1024:

                errors["profile_image"] = (
                    "Profile image must be smaller than 2 MB."
                )

            else:

                try:
                    image = Image.open(profile_image)
                    image_format = image.format
                    image.verify()

                except (UnidentifiedImageError, OSError):

                    errors["profile_image"] = (
                        "Please upload a valid image file."
                    )

                else:

                    if image_format not in ["JPEG", "PNG"]:

                        errors["profile_image"] = (
                            "Profile image must be JPG, JPEG, or PNG."
                        )

                    profile_image.seek(0)


        # If there are errors

        if errors:

            request.session["profile_edit_errors"] = errors

            request.session["profile_edit_values"] = {
                "full_name": full_name,
                "mobile_number": mobile_number,
            }

            return redirect("edit_profile")


        # Save profile information

        profile.full_name = full_name
        profile.mobile_number = mobile_number


        # Remove existing image

        if remove_profile_image == "1" and not profile_image:

            if profile.profile_image:

                profile.profile_image.delete(save=False)
                profile.profile_image = None

            profile.save()


        # Replace existing image with new image

        elif profile_image:

            old_image = profile.profile_image

            profile.profile_image = profile_image

            if old_image:
                profile.save()
                old_image.delete(save=False)
            else:
                profile.save()

        else:

            profile.save()


        messages.success(
            request,
            "Profile updated successfully.",
        )

        return redirect("profile")


    # GET request

    errors = request.session.pop(
        "profile_edit_errors",
        {},
    )

    values = request.session.pop(
        "profile_edit_values",
        {},
    )

    context = {
        "profile": profile,
        "user": request.user,
        "errors": errors,
        "full_name": values.get(
            "full_name",
            profile.full_name,
        ),
        "mobile_number": values.get(
            "mobile_number",
            profile.mobile_number,
        ),
    }

    return render(
        request,
        "user_profile/edit_profile.html",
        context,
    )
