from datetime import datetime
from http import HTTPStatus

from django.contrib import messages
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify
from django.views import View

from PIL import Image

from admin_panel.categories.models import Category

from .models import Brand


class BrandListView(View):
    template_name = "admin_panel/brands/brand_list.html"
    paginate_by = 5

    def get(self, request):
        queryset = Brand.objects.filter(is_deleted=False)
        queryset = queryset.annotate(
            products_count=Count(
                "products",
                filter=Q(products__is_deleted=False),
            )
        )

        search_query = request.GET.get("search", "").strip()
        if search_query:
            queryset = queryset.filter(name__icontains=search_query)

        status_filter = request.GET.get("status", "all").strip().lower()
        if status_filter == "active":
            queryset = queryset.filter(is_listed=True)
        elif status_filter == "inactive":
            queryset = queryset.filter(is_listed=False)

        category_filter = request.GET.get("category", "").strip()
        if category_filter:
            queryset = queryset.filter(category_id=category_filter)

        sort_by = request.GET.get("sort", "newest").strip().lower()
        sort_options = {
            "newest": "-created_at",
            "oldest": "created_at",
            "a-z": "name",
            "z-a": "-name",
        }
        order_field = sort_options.get(sort_by, "-created_at")
        queryset = queryset.order_by(order_field)

        paginator = Paginator(queryset, self.paginate_by)
        page_number = request.GET.get("page", 1)

        try:
            brands = paginator.page(page_number)
        except PageNotAnInteger:
            brands = paginator.page(1)
        except EmptyPage:
            brands = paginator.page(paginator.num_pages)

        context = {
            "brands": brands,
            "search_query": search_query,
            "status_filter": status_filter,
            "category_filter": category_filter,
            "sort_by": sort_by,
            "total_count": paginator.count,
        }

        return render(request, self.template_name, context)

class BrandCreateView(View):
    template_name = "admin_panel/brands/brand_add.html"

    def get(self, request):
        categories = Category.objects.filter(
            is_deleted=False
        ).order_by("name")

        context = {
            "categories": categories,
        }

        return render(request, self.template_name, context)
    
    def post(self, request):
        name = request.POST.get("name", "").strip()
        category_id = request.POST.get("category", "").strip()
        description = request.POST.get("description", "").strip()
        listing_description = request.POST.get(
            "listing_description", ""
        ).strip()
        hero_description = request.POST.get(
            "hero_description", ""
        ).strip()

        listing_image = request.FILES.get("listing_image")
        hero_image = request.FILES.get("hero_image")

        field_errors = {}

        # Name validation
        if not name:
            field_errors["name"] = "Brand name is required."
        elif Brand.objects.filter(
            name__iexact=name,
            is_deleted=False,
        ).exists():
            field_errors["name"] = f"Brand '{name}' already exists."

        # Category validation
        category = None

        if not category_id:
            field_errors["category"] = "Category is required."
        elif not Category.objects.filter(
            id=category_id,
            is_deleted=False,
        ).exists():
            field_errors["category"] = "Please select a valid category."
        else:
            category = Category.objects.get(
                id=category_id,
                is_deleted=False,
            )

        # Description validation
        if not description:
            field_errors["description"] = (
                "Brand description is required."
            )

        # Listing description validation
        if not listing_description:
            field_errors["listing_description"] = (
                "Listing description is required."
            )

        # Hero description validation
        if not hero_description:
            field_errors["hero_description"] = (
                "Hero description is required."
            )

        # Image validation
        allowed_image_types = [
            "image/jpeg",
            "image/png",
            "image/webp",
        ]

        def validate_image(image_file, field_name):
            if not image_file:
                field_errors[field_name] = "Image is required."
                return

            if image_file.content_type not in allowed_image_types:
                field_errors[field_name] = (
                    "Only JPEG, PNG, and WebP images are allowed."
                )
                return

            try:
                image = Image.open(image_file)
                width, height = image.size

                if min(width, height) < 800:
                    field_errors[field_name] = (
                        "Image must be at least 800px on its shorter side."
                    )

            except Exception:
                field_errors[field_name] = "Invalid image file."

        validate_image(listing_image, "listing_image")
        validate_image(hero_image, "hero_image")

        # Return validation errors
        if field_errors:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        # Generate unique slug
        base_slug = slugify(name)
        slug = base_slug
        counter = 1

        while Brand.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        # Create Brand
        Brand.objects.create(
            category=category,
            name=name,
            slug=slug,
            description=description,
            listing_image=listing_image,
            listing_description=listing_description,
            hero_image=hero_image,
            hero_description=hero_description,
        )

        messages.success(
            request,
            f"Brand '{name}' created successfully!",
        )

        return JsonResponse(
            {
                "success": True,
                "message": f"Brand '{name}' created successfully!",
            },
            status=HTTPStatus.CREATED,
        )

class BrandUpdateView(View):
    template_name = "admin_panel/brands/brand_edit.html"

    def get(self, request, brand_id):
        brand = get_object_or_404(
            Brand,
            id=brand_id,
            is_deleted=False,
        )

        categories = Category.objects.filter(
            is_deleted=False
        ).order_by("name")

        context = {
            "brand": brand,
            "categories": categories,
        }

        return render(request, self.template_name, context)
    
    def post(self, request, brand_id):
        brand = get_object_or_404(
            Brand,
            id=brand_id,
            is_deleted=False,
        )

        name = request.POST.get("name", "").strip()
        category_id = request.POST.get("category", "").strip()
        description = request.POST.get("description", "").strip()
        listing_description = request.POST.get(
            "listing_description", ""
        ).strip()
        hero_description = request.POST.get(
            "hero_description", ""
        ).strip()

        listing_image = request.FILES.get("listing_image")
        hero_image = request.FILES.get("hero_image")

        field_errors = {}

        # Name validation
        if not name:
            field_errors["name"] = "Brand name is required."
        elif Brand.objects.filter(
            name__iexact=name,
            is_deleted=False,
        ).exclude(id=brand.id).exists():
            field_errors["name"] = (
                f"Another brand with the name '{name}' already exists."
            )

        # Category validation
        category = None

        if not category_id:
            field_errors["category"] = "Category is required."
        elif not Category.objects.filter(
            id=category_id,
            is_deleted=False,
        ).exists():
            field_errors["category"] = "Please select a valid category."
        else:
            category = Category.objects.get(
                id=category_id,
                is_deleted=False,
            )

        # Description validation
        if not description:
            field_errors["description"] = (
                "Brand description is required."
            )

        # Listing description validation
        if not listing_description:
            field_errors["listing_description"] = (
                "Listing description is required."
            )

        # Hero description validation
        if not hero_description:
            field_errors["hero_description"] = (
                "Hero description is required."
            )

        # Image validation
        allowed_image_types = [
            "image/jpeg",
            "image/png",
            "image/webp",
        ]

        def validate_image(image_file, field_name):
            if not image_file:
                return

            if image_file.content_type not in allowed_image_types:
                field_errors[field_name] = (
                    "Only JPEG, PNG, and WebP images are allowed."
                )
                return

            try:
                image = Image.open(image_file)
                width, height = image.size

                if min(width, height) < 800:
                    field_errors[field_name] = (
                        "Image must be at least 800px on its shorter side."
                    )

            except Exception:
                field_errors[field_name] = "Invalid image file."

        validate_image(listing_image, "listing_image")
        validate_image(hero_image, "hero_image")

        # Return validation errors
        if field_errors:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        # Generate a new slug only if the name changed
        if name.lower() != brand.name.lower():
            base_slug = slugify(name)
            slug = base_slug
            counter = 1

            while Brand.objects.filter(
                slug=slug
            ).exclude(id=brand.id).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1

            brand.slug = slug

        # Update normal fields
        brand.category = category
        brand.name = name
        brand.description = description
        brand.listing_description = listing_description
        brand.hero_description = hero_description

        # Replace listing image if a new one was uploaded
        if listing_image:
            if brand.listing_image:
                brand.listing_image.delete(save=False)

            brand.listing_image = listing_image

        # Replace hero image if a new one was uploaded
        if hero_image:
            if brand.hero_image:
                brand.hero_image.delete(save=False)

            brand.hero_image = hero_image

        brand.save()

        messages.success(
            request,
            f"Brand '{name}' updated successfully!",
        )

        return JsonResponse(
            {
                "success": True,
                "message": f"Brand '{name}' updated successfully!",
            },
            status=HTTPStatus.OK,
        )

class BrandDeleteView(View):
    def post(self, request, brand_id):
        brand = get_object_or_404(
            Brand,
            id=brand_id,
            is_deleted=False,
        )

        brand_name = brand.name

        brand.is_deleted = True
        brand.save()

        messages.success(
            request,
            f"Brand '{brand_name}' deleted successfully!",
        )

        return JsonResponse(
            {
                "success": True,
                "message": f"Brand '{brand_name}' deleted successfully!",
            },
            status=HTTPStatus.OK,
        )

