from decimal import Decimal, InvalidOperation
from http import HTTPStatus

from django.contrib import messages
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify
from django.views import View

from PIL import Image

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category

from .models import Product, ProductVariant, VariantImage


ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}

MIN_VARIANT_IMAGES = 3
MAX_VARIANT_IMAGES = 5


class ProductListView(View):
    template_name = "admin_panel/products/product_list.html"
    paginate_by = 5

    def get(self, request):
        queryset = (
            Product.objects
            .filter(
                is_deleted=False,
                brand__is_deleted=False,
            )
            .select_related(
                "brand",
                "brand__category",
            )
            .prefetch_related(
                "variants__images",
            )
            .annotate(
                variants_count=Count(
                    "variants",
                    filter=Q(
                        variants__is_deleted=False,
                    ),
                    distinct=True,
                )
            )
        )

        search_query = request.GET.get(
            "search",
            "",
        ).strip()

        if search_query:
            queryset = queryset.filter(
                Q(name__icontains=search_query)
                | Q(brand__name__icontains=search_query)
                | Q(variants__sku__icontains=search_query)
            ).distinct()

        status_filter = request.GET.get(
            "status",
            "all",
        ).strip().lower()

        if status_filter == "active":
            queryset = queryset.filter(
                is_listed=True,
            )
        elif status_filter == "inactive":
            queryset = queryset.filter(
                is_listed=False,
            )

        category_filter = request.GET.get(
            "category",
            "",
        ).strip()

        if category_filter:
            queryset = queryset.filter(
                brand__category_id=category_filter,
            )

        brand_filter = request.GET.get(
            "brand",
            "",
        ).strip()

        if brand_filter:
            queryset = queryset.filter(
                brand_id=brand_filter,
            )

        fragrance_filter = request.GET.get(
            "fragrance_concentration",
            "",
        ).strip()

        valid_concentrations = {
            choice[0]
            for choice in Product.FragranceConcentration.choices
        }

        if fragrance_filter in valid_concentrations:
            queryset = queryset.filter(
                fragrance_concentration=fragrance_filter,
            )

        sort_by = request.GET.get(
            "sort",
            "newest",
        ).strip().lower()

        sort_options = {
            "newest": "-created_at",
            "oldest": "created_at",
            "a-z": "name",
            "z-a": "-name",
        }

        order_field = sort_options.get(
            sort_by,
            "-created_at",
        )

        queryset = queryset.order_by(order_field)

        paginator = Paginator(
            queryset,
            self.paginate_by,
        )

        page_number = request.GET.get(
            "page",
            1,
        )

        try:
            products = paginator.page(page_number)
        except PageNotAnInteger:
            products = paginator.page(1)
        except EmptyPage:
            products = paginator.page(
                paginator.num_pages,
            )

        categories = (
            Category.objects
            .filter(is_deleted=False)
            .order_by("name")
        )

        brands = (
            Brand.objects
            .filter(is_deleted=False)
            .select_related("category")
            .order_by("name")
        )

        context = {
            "products": products,
            "categories": categories,
            "brands": brands,
            "fragrance_concentrations": (
                Product.FragranceConcentration.choices
            ),
            "search_query": search_query,
            "status_filter": status_filter,
            "category_filter": category_filter,
            "brand_filter": brand_filter,
            "fragrance_filter": fragrance_filter,
            "sort_by": sort_by,
            "total_count": paginator.count,
        }

        return render(
            request,
            self.template_name,
            context,
        )


class ProductCreateView(View):
    template_name = "admin_panel/products/product_add.html"

    def get(self, request):
        categories = (
            Category.objects
            .filter(is_deleted=False)
            .order_by("name")
        )

        brands = (
            Brand.objects
            .filter(is_deleted=False)
            .select_related("category")
            .order_by("name")
        )

        context = {
            "categories": categories,
            "brands": brands,
            "fragrance_concentrations": (
                Product.FragranceConcentration.choices
            ),
        }

        return render(
            request,
            self.template_name,
            context,
        )

    def post(self, request):
        name = request.POST.get(
            "name",
            "",
        ).strip()

        brand_id = request.POST.get(
            "brand",
            "",
        ).strip()

        short_description = request.POST.get(
            "short_description",
            "",
        ).strip()

        description = request.POST.get(
            "description",
            "",
        ).strip()

        fragrance_concentration = request.POST.get(
            "fragrance_concentration",
            "",
        ).strip()

        top_notes = request.POST.get(
            "top_notes",
            "",
        ).strip()

        heart_notes = request.POST.get(
            "heart_notes",
            "",
        ).strip()

        base_notes = request.POST.get(
            "base_notes",
            "",
        ).strip()

        is_listed_input = request.POST.get(
            "is_listed",
        )

        # First Variant
        variant_size = request.POST.get(
            "variant_size",
            "",
        ).strip()

        variant_sku = request.POST.get(
            "variant_sku",
            "",
        ).strip()

        variant_price = request.POST.get(
            "variant_price",
            "",
        ).strip()

        variant_stock = request.POST.get(
            "variant_stock",
            "",
        ).strip()

        variant_is_listed_input = request.POST.get(
            "variant_is_listed",
        )

        variant_images = request.FILES.getlist(
            "variant_images",
        )

        field_errors = {}

        # -------------------------
        # Product validation
        # -------------------------

        if not name:
            field_errors["name"] = (
                "Product name is required."
            )
        elif Product.objects.filter(
            name__iexact=name,
            is_deleted=False,
        ).exists():
            field_errors["name"] = (
                f"Product '{name}' already exists."
            )

        brand = None

        if not brand_id:
            field_errors["brand"] = (
                "Brand is required."
            )
        else:
            brand = (
                Brand.objects
                .filter(
                    id=brand_id,
                    is_deleted=False,
                )
                .select_related("category")
                .first()
            )

            if not brand:
                field_errors["brand"] = (
                    "Please select a valid brand."
                )

        if not description:
            field_errors["description"] = (
                "Product description is required."
            )

        if len(short_description) > 300:
            field_errors["short_description"] = (
                "Short description cannot exceed "
                "300 characters."
            )

        valid_concentrations = {
            choice[0]
            for choice in Product.FragranceConcentration.choices
        }

        if not fragrance_concentration:
            field_errors["fragrance_concentration"] = (
                "Fragrance concentration/type is required."
            )
        elif fragrance_concentration not in valid_concentrations:
            field_errors["fragrance_concentration"] = (
                "Please select a valid fragrance "
                "concentration/type."
            )

        # -------------------------
        # First Variant validation
        # -------------------------

        self._validate_variant_fields(
            field_errors=field_errors,
            size=variant_size,
            sku=variant_sku,
            price=variant_price,
            stock=variant_stock,
            images=variant_images,
            prefix="variant_",
        )

        if variant_sku and ProductVariant.objects.filter(
            sku__iexact=variant_sku,
        ).exists():
            field_errors["variant_sku"] = (
                f"SKU '{variant_sku}' already exists."
            )

        if field_errors:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        is_listed = is_listed_input in [
            "on",
            "true",
            "True",
            True,
        ]

        variant_is_listed = (
            variant_is_listed_input in [
                "on",
                "true",
                "True",
                True,
            ]
        )

        try:
            with transaction.atomic():

                # -------------------------
                # Create Product
                # -------------------------

                slug = self._generate_unique_slug(
                    name,
                )

                product = Product.objects.create(
                    brand=brand,
                    name=name,
                    slug=slug,
                    short_description=short_description,
                    description=description,
                    fragrance_concentration=(
                        fragrance_concentration
                    ),
                    top_notes=top_notes,
                    heart_notes=heart_notes,
                    base_notes=base_notes,
                    is_listed=is_listed,
                )

                # -------------------------
                # Create First Variant
                # -------------------------

                variant = ProductVariant.objects.create(
                    product=product,
                    size=variant_size,
                    sku=variant_sku,
                    price=variant_price,
                    stock=variant_stock,
                    is_listed=variant_is_listed,
                )

                # -------------------------
                # Create Variant Images
                # -------------------------

                for index, image_file in enumerate(
                    variant_images,
                ):
                    VariantImage.objects.create(
                        variant=variant,
                        image=image_file,
                        display_order=index,
                    )

        except Exception:
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Something went wrong while "
                        "creating the product."
                    ),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        messages.success(
            request,
            (
                f"Product '{product.name}' "
                "created successfully!"
            ),
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Product '{product.name}' "
                    "created successfully!"
                ),
                "product_id": product.id,
            },
            status=HTTPStatus.CREATED,
        )

    @staticmethod
    def _generate_unique_slug(name):
        base_slug = slugify(name)
        slug = base_slug
        counter = 1

        while Product.objects.filter(
            slug=slug,
        ).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        return slug

    @staticmethod
    def _validate_variant_fields(
        field_errors,
        size,
        sku,
        price,
        stock,
        images,
        prefix="",
    ):
        if not size:
            field_errors[f"{prefix}size"] = (
                "Variant size is required."
            )

        if not sku:
            field_errors[f"{prefix}sku"] = (
                "SKU is required."
            )

        if not price:
            field_errors[f"{prefix}price"] = (
                "Price is required."
            )
        else:
            try:
                price_value = Decimal(price)

                if price_value < 0:
                    field_errors[f"{prefix}price"] = (
                        "Price cannot be negative."
                    )

            except (InvalidOperation, ValueError):
                field_errors[f"{prefix}price"] = (
                    "Please enter a valid price."
                )

        if not stock:
            field_errors[f"{prefix}stock"] = (
                "Stock is required."
            )
        else:
            try:
                stock_value = int(stock)

                if stock_value < 0:
                    field_errors[f"{prefix}stock"] = (
                        "Stock cannot be negative."
                    )

            except (TypeError, ValueError):
                field_errors[f"{prefix}stock"] = (
                    "Please enter a valid stock quantity."
                )

        if not images:
            field_errors[f"{prefix}images"] = (
                "At least 3 product images are required."
            )
            return

        if len(images) < MIN_VARIANT_IMAGES:
            field_errors[f"{prefix}images"] = (
                "At least 3 product images are required."
            )
            return

        if len(images) > MAX_VARIANT_IMAGES:
            field_errors[f"{prefix}images"] = (
                "A maximum of 5 product images "
                "is allowed."
            )
            return

        for image_file in images:

            if image_file.content_type not in (
                ALLOWED_IMAGE_TYPES
            ):
                field_errors[f"{prefix}images"] = (
                    "Only JPEG, PNG, and WebP images "
                    "are allowed."
                )
                return

            try:
                image = Image.open(image_file)
                image.verify()
                image_file.seek(0)

            except Exception:
                field_errors[f"{prefix}images"] = (
                    "One or more uploaded files "
                    "are not valid images."
                )
                return


class ProductUpdateView(View):
    template_name = "admin_panel/products/product_edit.html"

    def get(self, request, product_id):
        product = get_object_or_404(
            Product.objects.select_related(
                "brand",
                "brand__category",
            ),
            id=product_id,
            is_deleted=False,
        )

        categories = (
            Category.objects
            .filter(is_deleted=False)
            .order_by("name")
        )

        brands = (
            Brand.objects
            .filter(is_deleted=False)
            .select_related("category")
            .order_by("name")
        )

        context = {
            "product": product,
            "categories": categories,
            "brands": brands,
            "fragrance_concentrations": (
                Product.FragranceConcentration.choices
            ),
        }

        return render(
            request,
            self.template_name,
            context,
        )

    def post(self, request, product_id):
        product = get_object_or_404(
            Product,
            id=product_id,
            is_deleted=False,
        )

        name = request.POST.get(
            "name",
            "",
        ).strip()

        brand_id = request.POST.get(
            "brand",
            "",
        ).strip()

        short_description = request.POST.get(
            "short_description",
            "",
        ).strip()

        description = request.POST.get(
            "description",
            "",
        ).strip()

        fragrance_concentration = request.POST.get(
            "fragrance_concentration",
            "",
        ).strip()

        top_notes = request.POST.get(
            "top_notes",
            "",
        ).strip()

        heart_notes = request.POST.get(
            "heart_notes",
            "",
        ).strip()

        base_notes = request.POST.get(
            "base_notes",
            "",
        ).strip()

        is_listed_input = request.POST.get(
            "is_listed",
        )

        field_errors = {}

        if not name:
            field_errors["name"] = (
                "Product name is required."
            )
        elif Product.objects.filter(
            name__iexact=name,
            is_deleted=False,
        ).exclude(
            id=product.id,
        ).exists():
            field_errors["name"] = (
                f"Product '{name}' already exists."
            )

        brand = None

        if not brand_id:
            field_errors["brand"] = (
                "Brand is required."
            )
        else:
            brand = (
                Brand.objects
                .filter(
                    id=brand_id,
                    is_deleted=False,
                )
                .select_related("category")
                .first()
            )

            if not brand:
                field_errors["brand"] = (
                    "Please select a valid brand."
                )

        if not description:
            field_errors["description"] = (
                "Product description is required."
            )

        if len(short_description) > 300:
            field_errors["short_description"] = (
                "Short description cannot exceed "
                "300 characters."
            )

        valid_concentrations = {
            choice[0]
            for choice in Product.FragranceConcentration.choices
        }

        if not fragrance_concentration:
            field_errors["fragrance_concentration"] = (
                "Fragrance concentration/type is required."
            )
        elif fragrance_concentration not in valid_concentrations:
            field_errors["fragrance_concentration"] = (
                "Please select a valid fragrance "
                "concentration/type."
            )

        if field_errors:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        new_slug = product.slug

        if name != product.name:
            new_slug = ProductCreateView._generate_unique_slug(
                name,
            )

        product.name = name
        product.brand = brand
        product.slug = new_slug
        product.short_description = short_description
        product.description = description
        product.fragrance_concentration = (
            fragrance_concentration
        )
        product.top_notes = top_notes
        product.heart_notes = heart_notes
        product.base_notes = base_notes
        product.is_listed = is_listed_input in [
            "on",
            "true",
            "True",
            True,
        ]

        product.save()

        messages.success(
            request,
            (
                f"Product '{product.name}' "
                "updated successfully!"
            ),
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Product '{product.name}' "
                    "updated successfully!"
                ),
            },
            status=HTTPStatus.OK,
        )


class ProductDeleteView(View):

    def post(self, request, product_id):
        product = get_object_or_404(
            Product,
            id=product_id,
            is_deleted=False,
        )

        active_variants = ProductVariant.objects.filter(
            product=product,
            is_deleted=False,
        )

        if active_variants.exists():
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        f"Cannot delete '{product.name}' "
                        "while it has active variants. "
                        "Please remove or deactivate its "
                        "variants first."
                    ),
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        product.is_deleted = True
        product.is_listed = False

        product.save(
            update_fields=[
                "is_deleted",
                "is_listed",
                "updated_at",
            ],
        )

        messages.success(
            request,
            (
                f"Product '{product.name}' "
                "deleted successfully!"
            ),
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Product '{product.name}' "
                    "deleted successfully!"
                ),
            },
            status=HTTPStatus.OK,
        )


class VariantManagementView(View):
    template_name = (
        "admin_panel/products/variant_management.html"
    )

    def get(self, request, product_id):
        product = get_object_or_404(
            Product.objects.select_related(
                "brand",
                "brand__category",
            ),
            id=product_id,
            is_deleted=False,
        )

        variants = (
            ProductVariant.objects
            .filter(
                product=product,
                is_deleted=False,
            )
            .prefetch_related("images")
            .order_by("created_at")
        )

        search_query = request.GET.get(
            "search",
            "",
        ).strip()

        if search_query:
            variants = variants.filter(
                Q(size__icontains=search_query)
                | Q(sku__icontains=search_query)
            )

        context = {
            "product": product,
            "variants": variants,
            "search_query": search_query,
        }

        return render(
            request,
            self.template_name,
            context,
        )


class VariantCreateView(View):

    def post(self, request, product_id):
        product = get_object_or_404(
            Product,
            id=product_id,
            is_deleted=False,
        )

        size = request.POST.get(
            "size",
            "",
        ).strip()

        sku = request.POST.get(
            "sku",
            "",
        ).strip()

        price = request.POST.get(
            "price",
            "",
        ).strip()

        stock = request.POST.get(
            "stock",
            "",
        ).strip()

        is_listed_input = request.POST.get(
            "is_listed",
        )

        images = request.FILES.getlist(
            "images",
        )

        field_errors = {}

        ProductCreateView._validate_variant_fields(
            field_errors=field_errors,
            size=size,
            sku=sku,
            price=price,
            stock=stock,
            images=images,
        )

        if sku and ProductVariant.objects.filter(
            sku__iexact=sku,
        ).exists():
            field_errors["sku"] = (
                f"SKU '{sku}' already exists."
            )

        if field_errors:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        is_listed = is_listed_input in [
            "on",
            "true",
            "True",
            True,
        ]

        try:
            with transaction.atomic():

                variant = ProductVariant.objects.create(
                    product=product,
                    size=size,
                    sku=sku,
                    price=price,
                    stock=stock,
                    is_listed=is_listed,
                )

                for index, image_file in enumerate(
                    images,
                ):
                    VariantImage.objects.create(
                        variant=variant,
                        image=image_file,
                        display_order=index,
                    )

        except Exception:
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Something went wrong while "
                        "creating the variant."
                    ),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Variant '{size}' "
                    "created successfully!"
                ),
                "variant_id": variant.id,
            },
            status=HTTPStatus.CREATED,
        )


class VariantUpdateView(View):

    def post(self, request, variant_id):
        variant = get_object_or_404(
            ProductVariant.objects.select_related(
                "product",
            ),
            id=variant_id,
            is_deleted=False,
            product__is_deleted=False,
        )

        size = request.POST.get(
            "size",
            "",
        ).strip()

        sku = request.POST.get(
            "sku",
            "",
        ).strip()

        price = request.POST.get(
            "price",
            "",
        ).strip()

        stock = request.POST.get(
            "stock",
            "",
        ).strip()

        is_listed_input = request.POST.get(
            "is_listed",
        )

        new_images = request.FILES.getlist(
            "images",
        )

        field_errors = {}

        if not size:
            field_errors["size"] = (
                "Variant size is required."
            )

        if not sku:
            field_errors["sku"] = (
                "SKU is required."
            )
        elif ProductVariant.objects.filter(
            sku__iexact=sku,
        ).exclude(
            id=variant.id,
        ).exists():
            field_errors["sku"] = (
                f"SKU '{sku}' already exists."
            )

        if not price:
            field_errors["price"] = (
                "Price is required."
            )
        else:
            try:
                price_value = Decimal(price)

                if price_value < 0:
                    field_errors["price"] = (
                        "Price cannot be negative."
                    )

            except (InvalidOperation, ValueError):
                field_errors["price"] = (
                    "Please enter a valid price."
                )

        if not stock:
            field_errors["stock"] = (
                "Stock is required."
            )
        else:
            try:
                stock_value = int(stock)

                if stock_value < 0:
                    field_errors["stock"] = (
                        "Stock cannot be negative."
                    )

            except (TypeError, ValueError):
                field_errors["stock"] = (
                    "Please enter a valid stock quantity."
                )

        existing_image_count = variant.images.count()

        if new_images:

            if (
                existing_image_count + len(new_images)
                > MAX_VARIANT_IMAGES
            ):
                field_errors["images"] = (
                    "A maximum of 5 product images "
                    "is allowed."
                )

            for image_file in new_images:

                if image_file.content_type not in (
                    ALLOWED_IMAGE_TYPES
                ):
                    field_errors["images"] = (
                        "Only JPEG, PNG, and WebP images "
                        "are allowed."
                    )
                    break

                try:
                    image = Image.open(image_file)
                    image.verify()
                    image_file.seek(0)

                except Exception:
                    field_errors["images"] = (
                        "One or more uploaded files "
                        "are not valid images."
                    )
                    break

        if (
            existing_image_count + len(new_images)
            < MIN_VARIANT_IMAGES
        ):
            field_errors["images"] = (
                "A minimum of 3 product images is required."
            )

        if field_errors:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        variant.size = size
        variant.sku = sku
        variant.price = price
        variant.stock = stock
        variant.is_listed = is_listed_input in [
            "on",
            "true",
            "True",
            True,
        ]

        try:
            with transaction.atomic():

                variant.save()

                current_image_count = (
                    variant.images.count()
                )

                for index, image_file in enumerate(
                    new_images,
                    start=current_image_count,
                ):
                    VariantImage.objects.create(
                        variant=variant,
                        image=image_file,
                        display_order=index,
                    )

        except Exception:
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Something went wrong while "
                        "updating the variant."
                    ),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Variant '{variant.size}' "
                    "updated successfully!"
                ),
            },
            status=HTTPStatus.OK,
        )


class VariantDeleteView(View):

    def post(self, request, variant_id):
        variant = get_object_or_404(
            ProductVariant,
            id=variant_id,
            is_deleted=False,
            product__is_deleted=False,
        )

        active_variant_count = (
            ProductVariant.objects.filter(
                product=variant.product,
                is_deleted=False,
            ).count()
        )

        if active_variant_count <= 1:
            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "A product must have at least "
                        "one active variant."
                    ),
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        variant.is_deleted = True
        variant.is_listed = False

        variant.save(
            update_fields=[
                "is_deleted",
                "is_listed",
                "updated_at",
            ],
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Variant '{variant.size}' "
                    "deleted successfully!"
                ),
            },
            status=HTTPStatus.OK,
        )