from http import HTTPStatus

from django.contrib import messages
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify
from django.views import View

from .models import Category


class CategoryListView(View):
    template_name = "admin_panel/categories/category_list.html"
    paginate_by = 5  # Number of categories per page

    def get(self, request):
        # 1. Base Queryset (Exclude soft-deleted categories)
        queryset = Category.objects.filter(is_deleted=False)

        # Annotate total active product count for each category
        queryset = queryset.annotate(
            products_count=Count("brands__products", filter=Q(brands__products__is_deleted=False))
        )

        # 2. Search Query Handling (Restricted to Category Name)
        search_query = request.GET.get("search", "").strip()
        if search_query:
            queryset = queryset.filter(name__icontains=search_query)

        # 3. Status Filtering Handling
        status_filter = request.GET.get("status", "all").strip().lower()
        if status_filter == "active":
            queryset = queryset.filter(is_listed=True)
        elif status_filter == "inactive":
            queryset = queryset.filter(is_listed=False)

        # 4. Sorting Handling
        sort_by = request.GET.get("sort", "newest").strip().lower()
        sort_options = {
            "newest": "-created_at",
            "oldest": "created_at",
            "a-z": "name",
            "z-a": "-name",
        }
        order_field = sort_options.get(sort_by, "-created_at")
        queryset = queryset.order_by(order_field)

        # 5. Pagination Handling
        paginator = Paginator(queryset, self.paginate_by)
        page_number = request.GET.get("page", 1)

        try:
            categories = paginator.page(page_number)
        except PageNotAnInteger:
            categories = paginator.page(1)
        except EmptyPage:
            categories = paginator.page(paginator.num_pages)

        # 6. Context Payload
        context = {
            "categories": categories,
            "search_query": search_query,
            "status_filter": status_filter,
            "sort_by": sort_by,
            "total_count": paginator.count,
        }

        return render(request, self.template_name, context)


class CategoryCreateView(View):
    def post(self, request):
        name = request.POST.get("name", "").strip()
        offer_input = request.POST.get("offer_percentage", "0").strip()
        is_listed_input = request.POST.get("is_listed")

        field_errors = {}

        # 1. Validate Category Name
        if not name:
            field_errors["name"] = "Category name is required."
        elif Category.objects.filter(name__iexact=name, is_deleted=False).exists():
            field_errors["name"] = f"Category '{name}' already exists."

        # 2. Validate Offer Percentage
        offer_percentage = 0
        if offer_input:
            try:
                offer_percentage = int(offer_input)
                if offer_percentage < 0 or offer_percentage > 99:
                    field_errors["offer_percentage"] = "Offer must be between 0% and 99%."
            except ValueError:
                field_errors["offer_percentage"] = "Please enter a valid whole number."

        # Validation failed: Return 400 Bad Request
        if field_errors:
            return JsonResponse(
                {"success": False, "errors": field_errors},
                status=HTTPStatus.BAD_REQUEST  # Clean constant instead of 400
            )

        # 3. Handle Status
        is_listed = is_listed_input in ["on", "true", "True", True]

        # 4. Generate Unique Slug
        base_slug = slugify(name)
        slug = base_slug
        counter = 1
        while Category.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        # 5. Save Instance
        Category.objects.create(
            name=name,
            slug=slug,
            offer_percentage=offer_percentage,
            is_listed=is_listed,
        )

        # Attach success message for next page render (displayed as toast)
        messages.success(request, f"Category '{name}' created successfully!")

        return JsonResponse(
            {"success": True, "message": f"Category '{name}' created successfully!"},
            status=HTTPStatus.CREATED  
        )


class CategoryUpdateView(View):
    """
    Handles updating an existing category via POST using AJAX.
    URL pattern expects: /admin/categories//update/
    """

    def post(self, request, category_id):
        # 1. Fetch Target Category
        category = get_object_or_404(Category, id=category_id, is_deleted=False)

        name = request.POST.get("name", "").strip()
        offer_input = request.POST.get("offer_percentage", "0").strip()
        is_listed_input = request.POST.get("is_listed")

        field_errors = {}

        # 2. Validate Category Name
        if not name:
            field_errors["name"] = "Category name is required."
        elif (
            Category.objects.filter(name__iexact=name, is_deleted=False)
            .exclude(id=category.id)
            .exists()
        ):
            # Exclude current category ID so editing without changing name doesn't trigger duplicate error
            field_errors["name"] = f"Another category with the name '{name}' already exists."

        # 3. Validate Offer Percentage
        offer_percentage = 0
        if offer_input:
            try:
                offer_percentage = int(offer_input)
                if offer_percentage < 0 or offer_percentage > 99:
                    field_errors["offer_percentage"] = "Offer must be between 0% and 99%."
            except ValueError:
                field_errors["offer_percentage"] = "Please enter a valid whole number."

        # Return HTTP 400 Bad Request if validation fails (Modal stays open)
        if field_errors:
            return JsonResponse(
                {"success": False, "errors": field_errors},
                status=HTTPStatus.BAD_REQUEST,
            )

        # 4. Handle Status
        is_listed = is_listed_input in ["on", "true", "True", True]

        # 5. Handle Slug Update (Only recalculate if name has changed)
        if name.lower() != category.name.lower():
            base_slug = slugify(name)
            slug = base_slug
            counter = 1
            while Category.objects.filter(slug=slug).exclude(id=category.id).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            category.slug = slug

        # 6. Update Category Instance
        category.name = name
        category.offer_percentage = offer_percentage
        category.is_listed = is_listed
        category.save()

        # Attach success message for next page render
        messages.success(request, f"Category '{name}' updated successfully!")

        return JsonResponse(
            {"success": True, "message": f"Category '{name}' updated successfully!"},
            status=HTTPStatus.OK,
        )
    

class CategoryDeleteView(View):
    def post(self, request, category_id):
        category = get_object_or_404(
            Category,
            id=category_id,
            is_deleted=False,
        )

        if category.brands.filter(is_deleted=False).exists():
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        f"Cannot delete '{category.name}' — "
                        "it still has active brands assigned."
                    ),
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        category_name = category.name
        category.is_deleted = True
        category.save()

        messages.success(
            request,
            f"Category '{category_name}' deleted successfully!",
        )

        return JsonResponse(
            {
                "success": True,
                "message": f"Category '{category_name}' deleted successfully!",
            },
            status=HTTPStatus.OK,
        )

