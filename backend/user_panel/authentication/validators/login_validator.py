"""
Validation functions for user login.
"""

import re

def validate_email(email):
    """
    Validate the user's email during login.
    """

    email = email.strip().lower()

    if not email:
        return "Email address is required."

    email_pattern = r"^[\w\.-]+@[\w\.-]+\.\w{2,}$"

    if not re.fullmatch(email_pattern,email):
        return "Enter a valid email address."

    return None

def validate_password(password):
    """
    Validate the user's password during login.
    """

    if not password:
        return "Password is required."

    return None