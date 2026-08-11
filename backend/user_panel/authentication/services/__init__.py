from django.shortcuts import render

def signup(request):
    """
    Display and process the user signup page.
    """
    if request.method == "POST":

        signup_data = {
            "full_name": request.POST.get("full_name", "").strip(),
            "email": request.POST.get("email", "").strip().lower(),
            "password": request.POST.get("password", ""),
            "confirm_password": request.POST.get("confirm_password", ""),
            "terms": request.POST.get("terms"),
        }

        print(signup_data)

    return render(request,"authentication/signup.html")

