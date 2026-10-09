"""Tests for user_panel.shop.

Covers:
- Eligible product detail request (200 OK, correct context).
- Direct access to an ineligible or deleted product (404).
- Unlisted/deleted Brand and Category cases (404).
- Products with no eligible variants (404).
- Zero-stock selected variant (200, unavailable state in context).
- Variant-specific pricing and image selection.
- Image ordering and primary image behaviour.
- Variant switching via query-string.
- Empty fragrance-note fields.
- Review empty state (no review architecture exists).
- Related-product eligibility and exclusion of current product.
- Missing/invalid product slug (404).
- Home Page Featured Perfumes and New Arrivals regression.
"""

from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant, VariantImage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


class ShopTestCase(TestCase):
    """Base class that creates a minimal, fully eligible product hierarchy."""

    def setUp(self):
        self.category = Category.objects.create(
            name="Test Category",
            is_listed=True,
            is_deleted=False,
        )
        self.brand = Brand.objects.create(
            category=self.category,
            name="Test Brand",
            description="Test brand description",
            listing_image=_fake_image("listing.jpg"),
            listing_description="Listing description",
            hero_image=_fake_image("hero.jpg"),
            hero_description="Hero description",
            is_listed=True,
            is_deleted=False,
        )
        self.product = Product.objects.create(
            brand=self.brand,
            name="Test Perfume",
            slug="test-perfume",
            short_description="A short description.",
            description="A detailed description.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
            top_notes="Rose, Bergamot",
            heart_notes="Jasmine, Oud",
            base_notes="Sandalwood, Musk",
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
        self.image = VariantImage.objects.create(
            variant=self.variant,
            image=_fake_image("product.jpg"),
            alt_text="Test Perfume 50ml",
            display_order=0,
        )

    def _url(self, slug=None):
        return reverse("shop:product_details", kwargs={"slug": slug or self.product.slug})


# ---------------------------------------------------------------------------
# 1. Eligible product — basic 200
# ---------------------------------------------------------------------------

class EligibleProductTest(ShopTestCase):

    def test_eligible_product_returns_200(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_correct_product_in_context(self):
        response = self.client.get(self._url())
        self.assertEqual(response.context["product"].pk, self.product.pk)

    def test_selected_variant_defaults_to_first_eligible(self):
        response = self.client.get(self._url())
        self.assertEqual(response.context["selected_variant"].pk, self.variant.pk)

    def test_images_belong_to_selected_variant(self):
        response = self.client.get(self._url())
        image_pks = list(response.context["images"].values_list("pk", flat=True))
        self.assertIn(self.image.pk, image_pks)

    def test_uses_correct_template(self):
        response = self.client.get(self._url())
        self.assertTemplateUsed(response, "shop/product_details.html")

    def test_review_empty_state_no_review_model(self):
        """No review architecture exists. Template must not crash."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# 2. 404 for ineligible products
# ---------------------------------------------------------------------------

class IneligibleProduct404Test(ShopTestCase):

    def test_product_is_deleted_returns_404(self):
        self.product.is_deleted = True
        self.product.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_product_is_unlisted_returns_404(self):
        self.product.is_listed = False
        self.product.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_brand_is_deleted_returns_404(self):
        self.brand.is_deleted = True
        self.brand.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_brand_is_unlisted_returns_404(self):
        self.brand.is_listed = False
        self.brand.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_category_is_deleted_returns_404(self):
        self.category.is_deleted = True
        self.category.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_category_is_unlisted_returns_404(self):
        self.category.is_listed = False
        self.category.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_invalid_slug_returns_404(self):
        self.assertEqual(self.client.get(self._url("nonexistent-slug")).status_code, 404)


# ---------------------------------------------------------------------------
# 3. No eligible variants → 404
# ---------------------------------------------------------------------------

class NoEligibleVariantsTest(ShopTestCase):

    def test_all_variants_deleted_returns_404(self):
        self.variant.is_deleted = True
        self.variant.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_all_variants_unlisted_returns_404(self):
        self.variant.is_listed = False
        self.variant.save()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_no_variants_at_all_returns_404(self):
        self.variant.delete()
        self.assertEqual(self.client.get(self._url()).status_code, 404)


# ---------------------------------------------------------------------------
# 4. Zero-stock variant — 200, correct unavailable state
# ---------------------------------------------------------------------------

class ZeroStockVariantTest(ShopTestCase):

    def setUp(self):
        super().setUp()
        self.variant.stock = 0
        self.variant.save()

    def test_zero_stock_variant_still_returns_200(self):
        """Product is eligible; zero stock doesn't make product ineligible."""
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_zero_stock_variant_in_context(self):
        response = self.client.get(self._url())
        self.assertEqual(response.context["selected_variant"].stock, 0)

    def test_zero_stock_shown_in_template(self):
        response = self.client.get(self._url())
        self.assertContains(response, "Out of Stock")


# ---------------------------------------------------------------------------
# 5. Variant switching via query-string
# ---------------------------------------------------------------------------

class VariantSwitchingTest(ShopTestCase):

    def setUp(self):
        super().setUp()
        self.variant2 = ProductVariant.objects.create(
            product=self.product,
            size="100ml",
            sku="TEST-100",
            price="380.00",
            stock=5,
            is_listed=True,
            is_deleted=False,
        )
        self.image2 = VariantImage.objects.create(
            variant=self.variant2,
            image=_fake_image("product2.jpg"),
            alt_text="Test Perfume 100ml",
            display_order=0,
        )

    def test_variant_param_selects_correct_variant(self):
        response = self.client.get(self._url() + "?variant=TEST-100")
        self.assertEqual(response.context["selected_variant"].pk, self.variant2.pk)

    def test_variant_param_updates_price(self):
        response = self.client.get(self._url() + "?variant=TEST-100")
        self.assertEqual(str(response.context["selected_variant"].price), "380.00")

    def test_variant_param_updates_images(self):
        response = self.client.get(self._url() + "?variant=TEST-100")
        image_pks = list(response.context["images"].values_list("pk", flat=True))
        self.assertIn(self.image2.pk, image_pks)
        self.assertNotIn(self.image.pk, image_pks)

    def test_invalid_variant_sku_falls_back_to_first(self):
        response = self.client.get(self._url() + "?variant=INVALID-SKU")
        self.assertEqual(response.context["selected_variant"].pk, self.variant.pk)

    def test_default_is_earliest_variant_by_created_at(self):
        response = self.client.get(self._url())
        # self.variant was created first in setUp
        self.assertEqual(response.context["selected_variant"].pk, self.variant.pk)


# ---------------------------------------------------------------------------
# 6. Image ordering
# ---------------------------------------------------------------------------

class ImageOrderingTest(ShopTestCase):

    def setUp(self):
        super().setUp()
        self.image_high_order = VariantImage.objects.create(
            variant=self.variant,
            image=_fake_image("second.jpg"),
            alt_text="Second image",
            display_order=1,
        )
        self.image_low_order = VariantImage.objects.create(
            variant=self.variant,
            image=_fake_image("primary.jpg"),
            alt_text="Primary image",
            display_order=0,
        )

    def test_images_ordered_by_display_order(self):
        response = self.client.get(self._url())
        images = list(response.context["images"])
        orders = [img.display_order for img in images]
        self.assertEqual(orders, sorted(orders))

    def test_variant_with_no_images_returns_empty_list(self):
        bare_variant = ProductVariant.objects.create(
            product=self.product,
            size="200ml",
            sku="TEST-200",
            price="500.00",
            stock=3,
            is_listed=True,
            is_deleted=False,
        )
        response = self.client.get(self._url() + f"?variant={bare_variant.sku}")
        self.assertEqual(len(response.context["images"]), 0)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# 7. Fragrance notes — empty fields
# ---------------------------------------------------------------------------

class FragranceNotesTest(ShopTestCase):

    def test_all_notes_populated(self):
        response = self.client.get(self._url())
        self.assertContains(response, "Top Notes")
        self.assertContains(response, "Heart Notes")
        self.assertContains(response, "Base Notes")

    def test_empty_notes_not_rendered(self):
        self.product.top_notes = ""
        self.product.heart_notes = ""
        self.product.base_notes = ""
        self.product.save()
        response = self.client.get(self._url())
        self.assertNotContains(response, "Top Notes")
        self.assertNotContains(response, "Heart Notes")
        self.assertNotContains(response, "Base Notes")
        # The entire Fragrance Notes accordion section should be absent.
        self.assertNotContains(response, 'id="content-notes"')

    def test_partial_notes_only_populated_sections_shown(self):
        self.product.heart_notes = ""
        self.product.base_notes = ""
        self.product.save()
        response = self.client.get(self._url())
        self.assertContains(response, "Top Notes")
        self.assertNotContains(response, "Heart Notes")
        self.assertNotContains(response, "Base Notes")


# ---------------------------------------------------------------------------
# 8. Related products
# ---------------------------------------------------------------------------

class RelatedProductsTest(ShopTestCase):

    def _make_related_product(self, name, slug):
        """Create a fully eligible sibling product (same category)."""
        product = Product.objects.create(
            brand=self.brand,
            name=name,
            slug=slug,
            description="A related product.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
            is_listed=True,
            is_deleted=False,
        )
        # Use the full slug for SKU uniqueness across all related products.
        ProductVariant.objects.create(
            product=product,
            size="50ml",
            sku=f"REL-{slug}",
            price="199.00",
            stock=5,
            is_listed=True,
            is_deleted=False,
        )
        return product

    def test_current_product_excluded_from_related(self):
        response = self.client.get(self._url())
        related_pks = [p.pk for p in response.context["related_products"]]
        self.assertNotIn(self.product.pk, related_pks)

    def test_related_products_capped_at_4(self):
        for i in range(6):
            self._make_related_product(f"Related {i}", f"related-{i}")
        response = self.client.get(self._url())
        self.assertLessEqual(len(response.context["related_products"]), 4)

    def test_ineligible_related_products_excluded(self):
        related = self._make_related_product("Unlisted Related", "unlisted-related")
        related.is_listed = False
        related.save()
        response = self.client.get(self._url())
        related_pks = [p.pk for p in response.context["related_products"]]
        self.assertNotIn(related.pk, related_pks)

    def test_no_related_products_renders_gracefully(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# 9. Home Page regression — featured & new arrivals still work
# ---------------------------------------------------------------------------

class HomepageRegressionTest(ShopTestCase):

    def test_home_page_returns_200(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)

    def test_featured_products_in_home_context(self):
        response = self.client.get(reverse("home"))
        self.assertIn("featured_products", response.context)

    def test_new_arrivals_in_home_context(self):
        response = self.client.get(reverse("home"))
        self.assertIn("new_arrivals", response.context)

    def test_eligible_product_appears_in_featured(self):
        response = self.client.get(reverse("home"))
        featured_pks = [p.pk for p in response.context["featured_products"]]
        self.assertIn(self.product.pk, featured_pks)

    def test_unlisted_product_not_in_featured(self):
        self.product.is_listed = False
        self.product.save()
        response = self.client.get(reverse("home"))
        featured_pks = [p.pk for p in response.context["featured_products"]]
        self.assertNotIn(self.product.pk, featured_pks)
