from django.db import models
from django.utils.text import slugify

from admin_panel.categories.models import Category


class Brand(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="brands",
    )
    name = models.CharField(
        max_length=100,
        unique=True,
    )
    slug = models.SlugField(
        max_length=120,
        unique=True,
        blank=True,
    )
    description = models.TextField()

    listing_image = models.ImageField(
        upload_to="brands/listing/",
    )
    listing_description = models.CharField(
        max_length=300,
    )

    hero_image = models.ImageField(
        upload_to="brands/hero/",
    )
    hero_description = models.TextField()

    is_listed = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name