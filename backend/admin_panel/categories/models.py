from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify


class Category(models.Model):
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Name of the fragrance category (e.g., Designer Fragrances).",
    )
    slug = models.SlugField(
        max_length=120,
        unique=True,
        blank=True,
        help_text="URL-friendly slug generated automatically from name.",
    )
    description = models.TextField(
        blank=True,
        null=True,
        help_text="Short subtitle describing the category.",
    )

    # Category Offer (Managed inside Add/Edit Category Modal)
    offer_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00,
        validators=[
            MinValueValidator(0.00),
            MaxValueValidator(99.00),
        ],
        help_text="Discount percentage for this category (0 to 99%).",
    )

    # Status & Soft Delete
    is_listed = models.BooleanField(
        default=True,
        help_text="Controls if the category is Active/Listed in shop frontend.",
    )
    is_deleted = models.BooleanField(
        default=False,
        help_text="Soft delete flag to preserve historical order records.",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Category"
        verbose_name_plural = "Categories"
        ordering = ["-created_at"]  # Latest categories appear first

    def save(self, *args, **kwargs):
        # Auto-generate slug from name if not provided
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

# Create your models here.
