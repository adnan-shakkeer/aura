from django.shortcuts import render
from http import HTTPStatus
from django.contrib.auth.decorators import login_required
from .models import Address

import re
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.db import DatabaseError


@login_required
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

    # Extract form data
    full_name = request.POST.get("full_name", "").strip()
    phone_number = request.POST.get("phone_number", "").strip()
    address_line1 = request.POST.get("address_line1", "").strip()
    address_line2 = request.POST.get("address_line2", "").strip()
    landmark = request.POST.get("landmark", "").strip()
    city = request.POST.get("city", "").strip()
    state = request.POST.get("state", "").strip()
    pincode = request.POST.get("pincode", "").strip()
    address_type = request.POST.get("address_type", "HOME").strip().upper()
    is_default = request.POST.get("is_default") in ["true", "True", "1", True,"on"]

    # Required field presence validation
    required_fields = [full_name, phone_number, address_line1, city, state, pincode]
    if not all(required_fields):
        return JsonResponse(
            {"success": False, "message": "Please fill in all required fields."},
            status=HTTPStatus.BAD_REQUEST,
        )

    # Phone number validation (10 to 15 digits)
    if not re.match(r"^\+?[0-9]{10,15}$", phone_number):
        return JsonResponse(
            {"success": False, "message": "Please enter a valid phone number (10–15 digits)."},
            status=HTTPStatus.BAD_REQUEST,
        )

    # Pincode validation (6-digit Indian postal code)
    if not re.match(r"^[1-9][0-9]{5}$", pincode):
        return JsonResponse(
            {"success": False, "message": "Please enter a valid 6-digit postal pincode."},
            status=HTTPStatus.BAD_REQUEST,
        )

    # Validate choices for address type
    valid_types = [choice[0] for choice in Address.ADDRESS_TYPE_CHOICES]
    if address_type not in valid_types:
        address_type = "HOME"

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

    full_name = request.POST.get("full_name", "").strip()
    phone_number = request.POST.get("phone_number", "").strip()
    address_line1 = request.POST.get("address_line1", "").strip()
    address_line2 = request.POST.get("address_line2", "").strip()
    landmark = request.POST.get("landmark", "").strip()
    city = request.POST.get("city", "").strip()
    state = request.POST.get("state", "").strip()
    pincode = request.POST.get("pincode", "").strip()
    address_type = request.POST.get("address_type", "HOME").strip().upper()
    is_default = request.POST.get("is_default") in ["true", "True", "1", True,"on"]

    required_fields = [full_name, phone_number, address_line1, city, state, pincode]
    if not all(required_fields):
        return JsonResponse(
            {"success": False, "message": "Please fill in all required fields."},
            status=HTTPStatus.BAD_REQUEST,
        )

    if not re.match(r"^\+?[0-9]{10,15}$", phone_number):
        return JsonResponse(
            {"success": False, "message": "Please enter a valid phone number (10–15 digits)."},
            status=HTTPStatus.BAD_REQUEST,
        )

    if not re.match(r"^[1-9][0-9]{5}$", pincode):
        return JsonResponse(
            {"success": False, "message": "Please enter a valid 6-digit postal pincode."},
            status=HTTPStatus.BAD_REQUEST,
        )

    valid_types = [choice[0] for choice in Address.ADDRESS_TYPE_CHOICES]
    if address_type in valid_types:
        address.address_type = address_type

    address.full_name = full_name
    address.phone_number = phone_number
    address.address_line1 = address_line1
    address.address_line2 = address_line2 or None
    address.landmark = landmark or None
    address.city = city
    address.state = state
    address.pincode = pincode
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

    

    





