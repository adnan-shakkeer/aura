import re
from PIL import Image, UnidentifiedImageError

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages

from .models import UserProfile


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


