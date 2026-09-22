from django.shortcuts import render
from http import HTTPStatus
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import never_cache
from .models import Address

import re
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.db import DatabaseError


@login_required
@never_cache
def address_book(request):
    """
    Render the dedicated Address Book management page listing all active addresses.
    """
    addresses = Address.objects.filter(
        user=request.user,
        is_deleted=False,
    ).order_by("-is_default", "-created_at")

    context = {
        "addresses" : addresses,
        "active_nav" : "addresses",
    }

    return render(request,"address/address_book.html",context)

@login_required
def add_address(request):
    """
    API endpoint to create a new address via AJAX POST.
    """
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "message": "Invalid request method."},
            status=HTTPStatus.METHOD_NOT_ALLOWED,
        )

    # ── Extract & normalise form data ─────────────────────────────────────────
    # Strip outer whitespace first, then collapse any internal multiple spaces.
    full_name     = re.sub(r"\s+", " ", request.POST.get("full_name",     "").strip())
    phone_number  =                      request.POST.get("phone_number",  "").strip()
    address_line1 = re.sub(r"\s+", " ", request.POST.get("address_line1", "").strip())
    address_line2 = re.sub(r"\s+", " ", request.POST.get("address_line2", "").strip())
    landmark      = re.sub(r"\s+", " ", request.POST.get("landmark",      "").strip())
    city          = re.sub(r"\s+", " ", request.POST.get("city",          "").strip())
    state         = re.sub(r"\s+", " ", request.POST.get("state",         "").strip())
    pincode       =                      request.POST.get("pincode",       "").strip()
    address_type  =                      request.POST.get("address_type",  "HOME").strip().upper()
    is_default    = request.POST.get("is_default") in ["true", "True", "1", True, "on"]

    # ── Field validations ─────────────────────────────────────────────────────
    errors = {}

    if not full_name:
        errors["full_name"] = "Full name is required."
    elif not (3 <= len(full_name) <= 100):
        errors["full_name"] = "Full name must be between 3 and 100 characters."
    elif not re.fullmatch(r"[A-Za-z ]+", full_name):
        errors["full_name"] = "Full name can contain only letters and spaces."
    elif not (full_name[0].isalpha() and full_name[-1].isalpha()):
        errors["full_name"] = "Full name must start and end with a letter."
    elif re.search(r"['.\-]{2,}", full_name):
        errors["full_name"] = "Full name must not contain consecutive special characters."

    if not phone_number:
        errors["phone_number"] = "Phone number is required."
    elif not re.match(r"^[6-9][0-9]{9}$", phone_number):
        errors["phone_number"] = "Please enter a valid 10-digit Indian mobile number starting with 6, 7, 8, or 9."

    if not address_line1:
        errors["address_line1"] = "Address line 1 is required."
    elif not (5 <= len(address_line1) <= 40):
        errors["address_line1"] = "Address line 1 must be between 5 and 40 characters."
    elif not re.search(r"[A-Za-z0-9]", address_line1):
        errors["address_line1"] = "Address line 1 must contain at least one letter or number."

    if address_line2:
        if len(address_line2) > 40:
            errors["address_line2"] = "Address line 2 must not exceed 40 characters."
        elif not re.search(r"[A-Za-z0-9]", address_line2):
            errors["address_line2"] = "Address line 2 must contain at least one letter or number."

    if landmark:
        if not (3 <= len(landmark) <= 25):
            errors["landmark"] = "Landmark must be between 3 and 25 characters."

    if not city:
        errors["city"] = "City is required."
    elif not (2 <= len(city) <= 20):
        errors["city"] = "City must be between 2 and 20 characters."
    elif not re.match(r"^[A-Za-z]+(?:[ \-][A-Za-z]+)*$", city):
        errors["city"] = "City must contain only letters, spaces, or hyphens."

    if not state:
        errors["state"] = "State is required."
    elif not (2 <= len(state) <= 20):
        errors["state"] = "State must be between 2 and 20 characters."
    elif not re.match(r"^[A-Za-z]+(?:[ \-][A-Za-z]+)*$", state):
        errors["state"] = "State must contain only letters, spaces, or hyphens."

    if not pincode:
        errors["pincode"] = "Pincode is required."
    elif not re.match(r"^[1-9][0-9]{5}$", pincode):
        errors["pincode"] = "Please enter a valid 6-digit postal pincode."

    # ── Address type sanitisation ──────────────────────────────────────────────
    valid_types = [choice[0] for choice in Address.ADDRESS_TYPE_CHOICES]
    if address_type not in valid_types:
        address_type = "HOME"

    if errors:
        return JsonResponse({"success": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)

    try:

        address = Address.objects.create(
            user=request.user,
            full_name=full_name,
            phone_number=phone_number,
            address_line1=address_line1,
            address_line2=address_line2 or None,
            landmark=landmark or None,
            city=city,
            state=state,
            pincode=pincode,
            address_type=address_type,
            is_default=is_default,
        )

    except DatabaseError:
        return JsonResponse(
            {"success": False, "message": "Unable to save address right now. Please try again."},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JsonResponse(
        {"success": True, "message": "Address added successfully.", "id": address.id},
        status=HTTPStatus.CREATED,
    )

@login_required
def edit_address(request, address_id):
    """
    API endpoint to update an existing user address via AJAX POST.
    """
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "message": "Invalid request method."},
            status=HTTPStatus.METHOD_NOT_ALLOWED,
        )

    address = get_object_or_404(Address, id=address_id, user=request.user, is_deleted=False)

    # ── Extract & normalise form data ─────────────────────────────────────────
    full_name     = re.sub(r"\s+", " ", request.POST.get("full_name",     "").strip())
    phone_number  =                      request.POST.get("phone_number",  "").strip()
    address_line1 = re.sub(r"\s+", " ", request.POST.get("address_line1", "").strip())
    address_line2 = re.sub(r"\s+", " ", request.POST.get("address_line2", "").strip())
    landmark      = re.sub(r"\s+", " ", request.POST.get("landmark",      "").strip())
    city          = re.sub(r"\s+", " ", request.POST.get("city",          "").strip())
    state         = re.sub(r"\s+", " ", request.POST.get("state",         "").strip())
    pincode       =                      request.POST.get("pincode",       "").strip()
    address_type  =                      request.POST.get("address_type",  "HOME").strip().upper()
    is_default    = request.POST.get("is_default") in ["true", "True", "1", True, "on"]

    # ── Field validations ─────────────────────────────────────────────────────
    errors = {}

    if not full_name:
        errors["full_name"] = "Full name is required."
    elif not (3 <= len(full_name) <= 100):
        errors["full_name"] = "Full name must be between 3 and 100 characters."
    elif not re.fullmatch(r"[A-Za-z ]+", full_name):
        errors["full_name"] = "Full name can contain only letters and spaces."
    elif not (full_name[0].isalpha() and full_name[-1].isalpha()):
        errors["full_name"] = "Full name must start and end with a letter."
    elif re.search(r"['.\-]{2,}", full_name):
        errors["full_name"] = "Full name must not contain consecutive special characters."

    if not phone_number:
        errors["phone_number"] = "Phone number is required."
    elif not re.match(r"^[6-9][0-9]{9}$", phone_number):
        errors["phone_number"] = "Please enter a valid 10-digit Indian mobile number starting with 6, 7, 8, or 9."

    if not address_line1:
        errors["address_line1"] = "Address line 1 is required."
    elif not (5 <= len(address_line1) <= 40):
        errors["address_line1"] = "Address line 1 must be between 5 and 40 characters."
    elif not re.search(r"[A-Za-z0-9]", address_line1):
        errors["address_line1"] = "Address line 1 must contain at least one letter or number."

    if address_line2:
        if len(address_line2) > 40:
            errors["address_line2"] = "Address line 2 must not exceed 40 characters."
        elif not re.search(r"[A-Za-z0-9]", address_line2):
            errors["address_line2"] = "Address line 2 must contain at least one letter or number."

    if landmark:
        if not (3 <= len(landmark) <= 25):
            errors["landmark"] = "Landmark must be between 3 and 25 characters."

    if not city:
        errors["city"] = "City is required."
    elif not (2 <= len(city) <= 20):
        errors["city"] = "City must be between 2 and 20 characters."
    elif not re.match(r"^[A-Za-z]+(?:[ \-][A-Za-z]+)*$", city):
        errors["city"] = "City must contain only letters, spaces, or hyphens."

    if not state:
        errors["state"] = "State is required."
    elif not (2 <= len(state) <= 20):
        errors["state"] = "State must be between 2 and 20 characters."
    elif not re.match(r"^[A-Za-z]+(?:[ \-][A-Za-z]+)*$", state):
        errors["state"] = "State must contain only letters, spaces, or hyphens."

    if not pincode:
        errors["pincode"] = "Pincode is required."
    elif not re.match(r"^[1-9][0-9]{5}$", pincode):
        errors["pincode"] = "Please enter a valid 6-digit postal pincode."

    # ── Address type sanitisation ──────────────────────────────────────────────
    valid_types = [choice[0] for choice in Address.ADDRESS_TYPE_CHOICES]
    if address_type not in valid_types:
        address_type = "HOME"

    if errors:
        return JsonResponse({"success": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)

    address.full_name = full_name
    address.phone_number = phone_number
    address.address_line1 = address_line1
    address.address_line2 = address_line2 or None
    address.landmark = landmark or None
    address.city = city
    address.state = state
    address.pincode = pincode
    address.address_type = address_type
    address.is_default = is_default

    try:
        address.save()
    except DatabaseError:
        return JsonResponse(
            {"success": False, "message": "Unable to update address right now. Please try again."},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JsonResponse(
        {"success": True, "message": "Address updated successfully."},
        status=HTTPStatus.OK,
    )

@login_required
def delete_address(request, address_id):
    """
    API endpoint to soft-delete an address via AJAX POST.
    """
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "message": "Invalid request method."},
            status=HTTPStatus.METHOD_NOT_ALLOWED,
        )

    address = get_object_or_404(Address, id=address_id, user=request.user, is_deleted=False)
    was_default = address.is_default

    address.is_deleted=True
    address.is_default=False
    address.save()

    if was_default:
        remaining_address = Address.objects.filter(user=request.user,is_deleted=False).first()
        if remaining_address:
            remaining_address.is_default = True
            remaining_address.save()

    return JsonResponse(
        {"success": True, "message": "Address removed successfully."},
        status=HTTPStatus.OK,
    )

@login_required
def set_default_address(request, address_id):
    """
    API endpoint to explicitly mark an address as default via AJAX POST.
    """
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "message": "Invalid request method."},
            status=HTTPStatus.METHOD_NOT_ALLOWED,
        )

    address = get_object_or_404(Address, id=address_id, user=request.user, is_deleted=False)
    address.is_default = True
    address.save()

    return JsonResponse(
        {"success": True, "message": "Default address updated successfully."},
        status=HTTPStatus.OK,
    )

    

    





