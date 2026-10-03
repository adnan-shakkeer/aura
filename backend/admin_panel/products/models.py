from django.db import models
from django.utils.text import slugify

from admin_panel.brands.models import Brand


class Product(models.Model):

    class FragranceConcentration(models.TextChoices):
        PARFUM = "PARFUM", "Parfum"
        EAU_DE_PARFUM = "EDP", "Eau de Parfum (EDP)"
        EAU_DE_TOILETTE = "EDT", "Eau de Toilette (EDT)"
        EAU_DE_COLOGNE = "EDC", "Eau de Cologne (EDC)"
        ATTAR = "ATTAR", "Attar"

    brand = models.ForeignKey(
        Brand,
        on_delete=models.PROTECT,
        related_name="products",
    )

    name = models.CharField(max_length=200)

    slug = models.SlugField(
        max_length=220,
        unique=True,
        blank=True,
    )

    short_description = models.CharField(
        max_length=300,
        blank=True,
    )

    description = models.TextField()

    fragrance_concentration = models.CharField(
        max_length=20,
        choices=FragranceConcentration.choices,
    )

    top_notes = models.TextField(
        blank=True,
    )

    heart_notes = models.TextField(
        blank=True,
    )

    base_notes = models.TextField(
        blank=True,
    )

    is_listed = models.BooleanField(default=True)

    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1

            while Product.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1

            self.slug = slug

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ProductVariant(models.Model):

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="variants",
    )

    size = models.CharField(
        max_length=50,
    )

    sku = models.CharField(
        max_length=100,
        unique=True,
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    stock = models.PositiveIntegerField(
        default=0,
    )

    is_listed = models.BooleanField(
        default=True,
    )

    is_deleted = models.BooleanField(
        default=False,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.product.name} - {self.size}"


class VariantImage(models.Model):

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        related_name="images",
    )

    image = models.ImageField(
        upload_to="products/variants/",
    )

    alt_text = models.CharField(
        max_length=255,
        blank=True,
    )

    display_order = models.PositiveIntegerField(
        default=0,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["display_order", "created_at"]

    def __str__(self):
        return f"{self.variant} - Image {self.display_order + 1}"