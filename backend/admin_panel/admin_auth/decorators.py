from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

def admin_required(view_func):
    """
    Custom decorator to ensure the user is authenticated 
    and holds superuser/staff permissions.
    """
    @wraps(view_func)
    def _wrapper_view(request,*args,**kwargs):
        # --- BEFORE: Security Gatekeeper Checks ---
        if not request.user.is_authenticated:
            messages.warning(request, "Please log in to access the admin portal.")
            return redirect("login")

        if not (request.user.is_superuser or request.user.is_staff):
            messages.error(request,"Unauthorized access. Admin privileges required.")
            return redirect("login")
        
        # --- CALL ORIGINAL FUNCTION: If security checks pass ---
        return view_func(request, *args,**kwargs)

    return _wrapper_view


