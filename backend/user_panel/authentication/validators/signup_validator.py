"""
Validation functions for user signup.
"""

import re

def validate_full_name(full_name):
    """
    Validate the user's full name.
    """

    full_name = full_name.strip()

    if not full_name:
        return "Full name is required."

    if len(full_name) < 3:
        return "Full name must contain at least 3 characters."

    if not re.fullmatch(r"[A-Za-z ]+",full_name):
        return "Full name should contain only letters and spaces."

    return None

def validate_email(email):
    """
    Validate the user's email.
    """

    email = email.strip().lower()

    if not email:
        return "Email address is required."

    email_pattern = r"^[\w\.-]+@[\w\.-]+\.\w{2,}$"

    if not re.fullmatch(email_pattern, email):
        return "Enter a valid email address."

    return None

def validate_password(password):
    """
    Validate the user's password.
    """

    if not password:
        return "Password is required."

    if len(password) < 8:
        return "Password must contain at least 8 characters."

    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter."

    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter."

    if not re.search(r"\d", password):
        return "Password must contain at least one number."

    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return "Password must contain at least one special character."

    return None

def validate_confirm_password(password, confirm_password):
    """
    Validate confirm password.
    """

    if not confirm_password:
        return "Please confirm your password."

    if password != confirm_password:
        return "Passwords do not match."

    return None

def validate_terms(terms):
    """
    Validate Terms & Conditions.
    """

    if not terms:
        return "Please accept the Terms & Conditions."

    return None


