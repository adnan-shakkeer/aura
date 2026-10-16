from decimal import Decimal, InvalidOperation
from http import HTTPStatus
import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify
from django.views import View

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category

from .image_processing import (
    ALLOWED_IMAGE_TYPES,
    ERROR_INVALID_IMAGE,
    MAX_VARIANT_IMAGES,
    MIN_VARIANT_IMAGES,
    TARGET_SIZE,
    get_image_info,
    render_standard_image,
    save_replacement_image,
    validate_image_file,
)
from .models import Product, ProductVariant, VariantImage


def _delete_obsolete_files(file_names):
    """Delete obsolete media files after a successful DB commit.

    Filesystem operations do not roll back with database transactions,
    so this must only run via transaction.on_commit().
    """
    for name in file_names or []:
        if not name:
            continue
        try:
            if default_storage.exists(name):
                default_storage.delete(name)
        except Exception:
            continue


def _first_error_message(exc, default):
    messages = getattr(exc, "messages", None)

    if messages:
        return messages[0]

    return default


def _validate_uploads(files):
    """Validate incoming image files. Returns an error message or None."""
    for uploaded_file in files:
        try:
            validate_image_file(uploaded_file)
        except ValidationError as exc:
            return _first_error_message(exc, ERROR_INVALID_IMAGE)

    return None


def _parse_transform(raw_value):
    if not raw_value:
        return None

    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return None

    return parsed if isinstance(parsed, dict) else None


def _parse_new_file_map(request, files, field_errors, error_key="images"):
    """Map client image keys to uploaded files.

    The frontend uploads new/edited-new files in staged order and sends the
    matching ``new_image_keys`` list so order can be resolved safely.
    """
    keys = [
        key.strip()
        for key in request.POST.getlist("new_image_keys")
        if key.strip()
    ]

    if keys and len(keys) != len(files):
        field_errors[error_key] = (
            "Image upload data is inconsistent. Please retry."
        )
        return None

    if not keys:
        keys = [f"file-{index}" for index in range(len(files))]

    mapping = {}

    for key, uploaded_file in zip(keys, files):
        if key in mapping:
            field_errors[error_key] = (
                "Duplicate image data received. Please retry."
            )
            return None

        mapping[key] = uploaded_file

    return mapping


def _edited_new_keys(request):
    return {
        key.strip()
        for key in request.POST.getlist("edited_new_keys")
        if key.strip()
    }


def _require_standard_square(uploaded_file):
    """Enforce the 1200x1200 editor contract for explicitly edited blobs."""
    try:
        _format, width, height = get_image_info(uploaded_file)
    except ValidationError as exc:
        return _first_error_message(exc, ERROR_INVALID_IMAGE)

    if width != TARGET_SIZE or height != TARGET_SIZE:
        return "Edited image data is invalid. Please re-apply the edit."

    return None


def _resolve_edited_file(uploaded_file, transform, fallback_source=None):
    """Resolve the file to persist for an explicitly edited image.

    Edited blobs are the frontend-rendered 1200x1200 composition and are
    persisted as-is after validation (no reprocessing). If a blob fails the
    square check but a transform plus the original source is available, the
    composition is re-rendered server-side. Returns (file, error).
    """
    error = _validate_uploads([uploaded_file])

    if error:
        return None, error

    square_error = _require_standard_square(uploaded_file)

    if not square_error:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass

        return uploaded_file, None

    if transform is not None and fallback_source is not None:
        try:
            return (
                render_standard_image(fallback_source, transform),
                None,
            )
        except ValidationError as exc:
            return None, _first_error_message(exc, ERROR_INVALID_IMAGE)

    return None, square_error


def _resolve_ordered_new_files(
    request,
    files,
    field_errors,
    error_key="images",
):
    """Resolve staged order for create flows.

    Returns the uploaded files in final display order, or None when
    field_errors was populated. Without ``image_order`` tokens the upload
    order is preserved (legacy behavior).
    """
    tokens = [
        token.strip()
        for token in request.POST.getlist("image_order")
        if token.strip()
    ]

    if not tokens:
        return list(files)

    file_map = _parse_new_file_map(
        request,
        files,
        field_errors,
        error_key=error_key,
    )

    if file_map is None:
        return None

    edited_keys = _edited_new_keys(request)
    ordered = []
    seen = set()

    for token in tokens:
        if not token.startswith("n:") or token in seen:
            field_errors[error_key] = (
                "Invalid image order data. Please retry."
            )
            return None

        seen.add(token)
        key = token[2:]

        if not key or key not in file_map:
            field_errors[error_key] = (
                "Image upload data is inconsistent. Please retry."
            )
            return None

        ordered.append(file_map[key])

    if len(ordered) != len(files):
        field_errors[error_key] = (
            "Image upload data is inconsistent. Please retry."
        )
        return None

    for token, uploaded_file in zip(tokens, ordered):
        if token[2:] in edited_keys:
            square_error = _require_standard_square(uploaded_file)

            if square_error:
                field_errors[error_key] = square_error
                return None

    return ordered


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
        elif len(name) > 200:
            field_errors["name"] = (
                "Product name cannot exceed 200 characters."
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

        ordered_variant_images = None

        if not field_errors:
            ordered_variant_images = _resolve_ordered_new_files(
                request,
                variant_images,
                field_errors,
                error_key="variant_images",
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
                    ordered_variant_images,
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

                if price_value <= 0:
                    field_errors[f"{prefix}price"] = (
                        "Price must be greater than zero."
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

        upload_error = _validate_uploads(images)

        if upload_error:
            field_errors[f"{prefix}images"] = upload_error
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
        elif len(name) > 200:
            field_errors["name"] = (
                "Product name cannot exceed 200 characters."
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

        ordered_images = None

        if not field_errors:
            ordered_images = _resolve_ordered_new_files(
                request,
                images,
                field_errors,
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
                    ordered_images,
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

                if price_value <= 0:
                    field_errors["price"] = (
                        "Price must be greater than zero."
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

        raw_remove_ids = request.POST.getlist(
            "remove_image_ids",
        )

        parsed_remove_ids = set()

        for raw_id in raw_remove_ids:
            try:
                parsed_remove_ids.add(
                    int(str(raw_id).strip())
                )
            except (TypeError, ValueError):
                continue

        order_tokens = [
            token.strip()
            for token in request.POST.getlist("image_order")
            if token.strip()
        ]

        if order_tokens:
            return self._save_with_staged_order(
                request=request,
                variant=variant,
                size=size,
                sku=sku,
                price=price,
                stock=stock,
                is_listed_input=is_listed_input,
                new_images=new_images,
                field_errors=field_errors,
                order_tokens=order_tokens,
                parsed_remove_ids=parsed_remove_ids,
            )

        if parsed_remove_ids:
            images_to_remove = list(
                variant.images.filter(
                    id__in=parsed_remove_ids,
                ).order_by(
                    "display_order",
                    "created_at",
                    "id",
                )
            )
        else:
            images_to_remove = []

        remove_count = len(images_to_remove)

        existing_image_count = variant.images.count()

        final_image_count = (
            existing_image_count
            - remove_count
            + len(new_images)
        )

        if new_images:

            if (
                final_image_count
                > MAX_VARIANT_IMAGES
            ):
                field_errors["images"] = (
                    "A maximum of 5 product images "
                    "is allowed."
                )
            else:
                upload_error = _validate_uploads(new_images)

                if upload_error:
                    field_errors["images"] = upload_error

        if (
            final_image_count
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
            obsolete_file_names = []

            with transaction.atomic():

                variant.save()

                for image_record in images_to_remove:
                    if image_record.image:
                        obsolete_file_names.append(
                            image_record.image.name,
                        )
                    image_record.delete()

                remaining_images = list(
                    variant.images.order_by(
                        "display_order",
                        "created_at",
                        "id",
                    )
                )

                for order, image_record in enumerate(
                    remaining_images,
                ):
                    if image_record.display_order != order:
                        image_record.display_order = order
                        image_record.save(
                            update_fields=[
                                "display_order",
                            ]
                        )

                for index, image_file in enumerate(
                    new_images,
                    start=len(remaining_images),
                ):
                    VariantImage.objects.create(
                        variant=variant,
                        image=image_file,
                        display_order=index,
                    )

            if obsolete_file_names:
                transaction.on_commit(
                    lambda: _delete_obsolete_files(
                        obsolete_file_names,
                    )
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

    def _save_with_staged_order(
        self,
        request,
        variant,
        size,
        sku,
        price,
        stock,
        is_listed_input,
        new_images,
        field_errors,
        order_tokens,
        parsed_remove_ids,
    ):
        """Merge staged image state sent by the Image Editor collection.

        Token grammar (``image_order[]``): ``e:<id>`` for surviving existing
        images, ``n:<key>`` for newly uploaded files. Tokens must cover
        exactly the surviving existing images plus every uploaded new file.
        Untouched existing rows are never read or rewritten.
        """
        if field_errors:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        existing = {
            image.id: image
            for image in variant.images.all()
        }

        for remove_id in parsed_remove_ids:
            if remove_id not in existing:
                field_errors["images"] = (
                    "Invalid image selection. Please retry."
                )
                return JsonResponse(
                    {
                        "success": False,
                        "errors": field_errors,
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )

        removed_ids = set(parsed_remove_ids)
        survivor_ids = set(existing) - removed_ids

        replacements = {}

        for survivor_id in survivor_ids:
            uploaded = request.FILES.get(
                f"edited_image_{survivor_id}"
            )

            if uploaded is not None:
                replacements[survivor_id] = (
                    uploaded,
                    _parse_transform(
                        request.POST.get(f"transform_{survivor_id}")
                    ),
                )

        new_file_map = _parse_new_file_map(
            request,
            new_images,
            field_errors,
        )

        if new_file_map is None:
            return JsonResponse(
                {
                    "success": False,
                    "errors": field_errors,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        edited_new_keys = _edited_new_keys(request)
        ordered = []
        seen_tokens = set()

        for token in order_tokens:
            if token in seen_tokens:
                field_errors["images"] = (
                    "Invalid image order data. Please retry."
                )
                break

            seen_tokens.add(token)

            if token.startswith("e:"):
                try:
                    image_id = int(token[2:])
                except (TypeError, ValueError):
                    field_errors["images"] = (
                        "Invalid image order data. Please retry."
                    )
                    break

                if image_id not in survivor_ids:
                    field_errors["images"] = (
                        "Invalid image order data. Please retry."
                    )
                    break

                ordered.append(("existing", image_id))
            elif token.startswith("n:"):
                key = token[2:]

                if not key or key not in new_file_map:
                    field_errors["images"] = (
                        "Image upload data is inconsistent. "
                        "Please retry."
                    )
                    break

                ordered.append(("new", key))
            else:
                field_errors["images"] = (
                    "Invalid image order data. Please retry."
                )
                break

        if not field_errors:
            ordered_existing = {
                ref for kind, ref in ordered if kind == "existing"
            }
            ordered_new = [
                ref for kind, ref in ordered if kind == "new"
            ]

            if ordered_existing != survivor_ids or sorted(
                ordered_new
            ) != sorted(new_file_map):
                field_errors["images"] = (
                    "Image upload data is inconsistent. "
                    "Please retry."
                )

        if not field_errors:
            if len(ordered) < MIN_VARIANT_IMAGES:
                field_errors["images"] = (
                    "A minimum of 3 product images is required."
                )
            elif len(ordered) > MAX_VARIANT_IMAGES:
                field_errors["images"] = (
                    "A maximum of 5 product images "
                    "is allowed."
                )

        prepared_replacements = {}

        if not field_errors:
            for survivor_id, (uploaded, transform) in (
                replacements.items()
            ):
                stored_file = existing[survivor_id].image

                try:
                    stored_file.open()
                except Exception:
                    pass

                resolved, error = _resolve_edited_file(
                    uploaded,
                    transform,
                    fallback_source=stored_file,
                )

                if error:
                    field_errors["images"] = error
                    break

                prepared_replacements[survivor_id] = resolved

        if not field_errors:
            for kind, ref in ordered:
                if kind != "new":
                    continue

                uploaded = new_file_map[ref]
                upload_error = _validate_uploads([uploaded])

                if upload_error:
                    field_errors["images"] = upload_error
                    break

                if ref in edited_new_keys:
                    square_error = _require_standard_square(
                        uploaded
                    )

                    if square_error:
                        field_errors["images"] = square_error
                        break

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
            obsolete_file_names = []

            with transaction.atomic():
                variant.save()

                for remove_id in removed_ids:
                    record = existing[remove_id]

                    if record.image:
                        obsolete_file_names.append(
                            record.image.name,
                        )

                    record.delete()

                for kind, ref in ordered:
                    if (
                        kind == "existing"
                        and ref in prepared_replacements
                    ):
                        obsolete_file_names.append(
                            save_replacement_image(
                                existing[ref],
                                prepared_replacements[ref],
                            )
                        )

                for index, (kind, ref) in enumerate(ordered):
                    if kind == "existing":
                        record = existing[ref]

                        if record.display_order != index:
                            record.display_order = index
                            record.save(
                                update_fields=[
                                    "display_order",
                                ]
                            )
                    else:
                        VariantImage.objects.create(
                            variant=variant,
                            image=new_file_map[ref],
                            display_order=index,
                        )

            if obsolete_file_names:
                transaction.on_commit(
                    lambda: _delete_obsolete_files(
                        obsolete_file_names,
                    )
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
        )

        variant.is_deleted = True
        variant.is_listed = False
        variant.save(
            update_fields=[
                "is_deleted",
                "is_listed",
                "updated_at",
            ]
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Variant '{variant.size}' "
                    "deleted successfully."
                ),
            },
            status=HTTPStatus.OK,
        )