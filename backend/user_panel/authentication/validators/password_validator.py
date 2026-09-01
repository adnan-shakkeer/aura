"""
Validation functions for password reset.
"""

import re


def validate_new_password(password):
    """
    Validate the new password during password reset.
    """

    if not password:
        return "Password is required."

    if len(password) < 8:
        return "Password must contain at least 8 characters."

    if not re.search(r"[A-Z]",password):
        return "Password must contain at least one uppercase letter."

    if not re.search(r"[a-z]",password):
        return "Password must contain at least one lowercase letter."

    if not re.search(r"\d",password):
        return "Password must contain at least one number."

    if not re.search(r"[!@#$%^&*(),.?':{}|<>]",password):
        return "Password must contain at least one special character."

    return None

def validate_confirm_password(password, confirm_password):
    """
    Validate the confirmation password.
    """

    if not confirm_password:
        return "Please confirm your password."

    if password != confirm_password:
        return "Passwords do not match."

    return None