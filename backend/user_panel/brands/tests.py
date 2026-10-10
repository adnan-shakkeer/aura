"""Tests for the user-side Brand Listing page.

Coverage:
- Listing URL returns 200 with the correct template.
- Eligible brands are displayed with image + description data.
- Unlisted/deleted brands (and brands in unlisted/deleted categories)
  are excluded, as are brands without any eligible product.
- Empty catalogue renders gracefully.
- EXPLORE BRAND links resolve to the Shop listing with a brand filter.
- Shop brand destinations respect existing product eligibility rules.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant


def _fake_image(name="test.jpg"):
    """Return a minimal in-memory JPEG as a SimpleUploadedFile."""
    content = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e\xc0"
        b"\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00"
        b"\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01"
        b"\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00"
        b"?\x00\xfb\xd9"
    )
    return SimpleUploadedFile(name, content, content_type="image/jpeg")


class BrandListingTestCase(TestCase):
    """Base fixture: one fully eligible brand hierarchy."""

    def setUp(self):
        self.category = Category.objects.create(
            name="Designer Fragrances",
            is_listed=True,
            is_deleted=False,
        )
        self.brand = Brand.objects.create(
            category=self.category,
            name="Test House",
            description="Full brand story.",
            listing_image=_fake_image("listing.jpg"),
            listing_description="Signature listing copy.",
            hero_image=_fake_image("hero.jpg"),
            hero_description="Hero copy.",
            is_listed=True,
            is_deleted=False,
        )
        self.product = Product.objects.create(
            brand=self.brand,
            name="Test Perfume",
            slug="test-perfume",
            description="A detailed description.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
            is_listed=True,
            is_deleted=False,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            size="50ml",
            sku="TEST-50",
            price="245.00",
            stock=10,
            is_listed=True,
            is_deleted=False,
        )

    def listing_url(self):
        return reverse("user_brands:brand_list")


class BrandListingBasicsTest(BrandListingTestCase):

    def test_listing_returns_200(self):
        self.assertEqual(self.client.get(self.listing_url()).status_code, 200)

    def test_listing_uses_correct_template(self):
        response = self.client.get(self.listing_url())
        self.assertTemplateUsed(response, "brands/brand_list.html")

    def test_page_introduction_present(self):
        response = self.client.get(self.listing_url())
        self.assertContains(response, "HERITAGE HOUSES")
        self.assertContains(response, "BRANDS")
        self.assertContains(response, "timeless craftsmanship")

    def test_eligible_brand_displayed(self):
        response = self.client.get(self.listing_url())
        brands = list(response.context["brands"])
        self.assertIn(self.brand.pk, [b.pk for b in brands])
        self.assertContains(response, "Test House")

    def test_listing_image_and_description_available(self):
        response = self.client.get(self.listing_url())
        brand = next(b for b in response.context["brands"] if b.pk == self.brand.pk)
        self.assertTrue(brand.listing_image)
        self.assertEqual(brand.listing_description, "Signature listing copy.")
        # Template renders the stored image URL and the description text.
        self.assertContains(response, brand.listing_image.url)
        self.assertContains(response, "Signature listing copy.")

    def test_no_hardcoded_sample_brands_leak(self):
        response = self.client.get(self.listing_url())
        content = response.content.decode()
        for name in ("Chanel", "Dior", "Creed", "Lattafa", "Rasasi"):
            self.assertNotIn(name, content)

    def test_shared_navigation_points_to_brands_route(self):
        response = self.client.get(self.listing_url())
        self.assertContains(response, f'href="{reverse("user_brands:brand_list")}"')


class BrandListingExclusionTest(BrandListingTestCase):

    def assert_brand_hidden(self):
        response = self.client.get(self.listing_url())
        self.assertNotIn(
            self.brand.pk, [b.pk for b in response.context["brands"]]
        )
        self.assertNotContains(response, "Test House")

    def test_unlisted_brand_excluded(self):
        self.brand.is_listed = False
        self.brand.save()
        self.assert_brand_hidden()

    def test_deleted_brand_excluded(self):
        self.brand.is_deleted = True
        self.brand.save()
        self.assert_brand_hidden()

    def test_unlisted_category_excluded(self):
        self.category.is_listed = False
        self.category.save()
        self.assert_brand_hidden()

    def test_deleted_category_excluded(self):
        self.category.is_deleted = True
        self.category.save()
        self.assert_brand_hidden()

    def test_brand_without_eligible_product_excluded(self):
        self.variant.is_listed = False
        self.variant.save()
        self.assert_brand_hidden()

    def test_brand_without_any_product_excluded(self):
        self.variant.delete()
        self.product.delete()
        response = self.client.get(self.listing_url())
        self.assertNotIn(
            self.brand.pk, [b.pk for b in response.context["brands"]]
        )

    def test_unlisted_product_does_not_qualify_brand(self):
        self.product.is_listed = False
        self.product.save()
        self.assert_brand_hidden()


class BrandListingEmptyStateTest(TestCase):

    def test_empty_catalogue_renders_gracefully(self):
        response = self.client.get(reverse("user_brands:brand_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["brands"]), [])
        self.assertContains(response, "being curated")


class BrandNavigationTest(BrandListingTestCase):

    def test_explore_brand_link_targets_shop_brand_filter(self):
        response = self.client.get(self.listing_url())
        shop_base = reverse("shop:shop_list")
        self.assertContains(
            response, f'{shop_base}?brand={self.brand.slug}'
        )

    def test_brand_destination_shows_only_eligible_products(self):
        other = Product.objects.create(
            brand=self.brand,
            name="Hidden Gem",
            slug="hidden-gem",
            description="Hidden.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
            is_listed=False,
        )
        ProductVariant.objects.create(
            product=other, size="50ml", sku="HIDDEN-50",
            price="99.00", stock=5,
        )
        url = f"{reverse('shop:shop_list')}?brand={self.brand.slug}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        names = [p.name for p in response.context["products"]]
        self.assertIn("Test Perfume", names)
        self.assertNotIn("Hidden Gem", names)

    def test_ineligible_brand_filter_yields_no_products(self):
        self.brand.is_listed = False
        self.brand.save()
        url = f"{reverse('shop:shop_list')}?brand={self.brand.slug}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["products"]), [])
