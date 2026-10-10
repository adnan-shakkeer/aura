"""Tests for the user-side Shop listing page and Product Details regression.

Listing coverage:
- Listing responds 200 with the correct template.
- Eligible products appear; unlisted/deleted products, brands and
  categories are excluded; products without eligible variants excluded.
- Zero-stock products with eligible variants remain visible.
- Starting price is the minimum eligible variant price.
- Representative size / primary image follow the earliest-variant rule.
- Search by product name, brand name and category name.
- Category filter, brand filter, and combined search + filters.
- All five sorting options, including unknown-sort fallback.
- Pagination: page size, preserved query params, invalid-page handling.
- Product cards link to the Product Details route.
- Shared Shop navigation points at the listing page.
- Empty results render a reset action; missing images degrade gracefully.
- Home Page and Product Details routes remain functional.
"""

from django.test import TestCase
from django.urls import reverse

from django.core.files.uploadedfile import SimpleUploadedFile

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant, VariantImage


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


class ShopListingTestCase(TestCase):
    """Base fixture: one fully eligible product hierarchy."""

    def setUp(self):
        self.category = Category.objects.create(
            name="Designer Fragrances",
            is_listed=True,
            is_deleted=False,
        )
        self.brand = Brand.objects.create(
            category=self.category,
            name="Rasasi",
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
            name="Hawas Ice",
            slug="hawas-ice",
            short_description="A short description.",
            description="A detailed description.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
            top_notes="Bergamot",
            heart_notes="Jasmine",
            base_notes="Musk",
            is_listed=True,
            is_deleted=False,
        )
        self.variant_50 = ProductVariant.objects.create(
            product=self.product,
            size="50ml",
            sku="HAWAS-50",
            price="245.00",
            stock=10,
            is_listed=True,
            is_deleted=False,
        )
        self.variant_100 = ProductVariant.objects.create(
            product=self.product,
            size="100ml",
            sku="HAWAS-100",
            price="380.00",
            stock=5,
            is_listed=True,
            is_deleted=False,
        )
        self.image = VariantImage.objects.create(
            variant=self.variant_50,
            image=_fake_image("product.jpg"),
            alt_text="Hawas Ice 50ml",
            display_order=0,
        )

    def listing_url(self, query=""):
        base = reverse("shop:shop_list")
        return f"{base}?{query}" if query else base

    def product_names_in_listing(self, response):
        return [p.name for p in response.context["products"]]


# ---------------------------------------------------------------------------
# 1-2. Listing responds; eligible products appear
# ---------------------------------------------------------------------------

class ListingBasicsTest(ShopListingTestCase):

    def test_listing_returns_200(self):
        self.assertEqual(self.client.get(self.listing_url()).status_code, 200)

    def test_listing_uses_correct_template(self):
        response = self.client.get(self.listing_url())
        self.assertTemplateUsed(response, "shop/shop_list.html")

    def test_eligible_product_appears(self):
        response = self.client.get(self.listing_url())
        self.assertIn("Hawas Ice", self.product_names_in_listing(response))

    def test_hero_uses_static_png_with_jpg_fallback(self):
        response = self.client.get(self.listing_url())
        self.assertContains(response, "images/shop_hero.png")
        self.assertContains(response, "images/shop_hero.jpg")
        self.assertContains(response, "#collection")

    def test_hero_has_no_duplicate_overlay_heading(self):
        # The artwork carries its own baked-in heading; the template must
        # not render a second visible heading over it.
        response = self.client.get(self.listing_url())
        self.assertNotContains(response, "THE FRAGRANCE EDIT")

    def test_toolbar_and_drawer_present(self):
        response = self.client.get(self.listing_url())
        self.assertContains(response, "SEARCH PERFUMES...")
        self.assertContains(response, "SORT BY:")
        self.assertContains(response, "filters-drawer")
        self.assertContains(response, "FILTERS")


# ---------------------------------------------------------------------------
# 3-7. Exclusions
# ---------------------------------------------------------------------------

class ListingExclusionTest(ShopListingTestCase):

    def assert_not_listed(self):
        response = self.client.get(self.listing_url())
        self.assertNotIn("Hawas Ice", self.product_names_in_listing(response))

    def test_unlisted_product_excluded(self):
        self.product.is_listed = False
        self.product.save()
        self.assert_not_listed()

    def test_deleted_product_excluded(self):
        self.product.is_deleted = True
        self.product.save()
        self.assert_not_listed()

    def test_unlisted_brand_excluded(self):
        self.brand.is_listed = False
        self.brand.save()
        self.assert_not_listed()

    def test_deleted_brand_excluded(self):
        self.brand.is_deleted = True
        self.brand.save()
        self.assert_not_listed()

    def test_unlisted_category_excluded(self):
        self.category.is_listed = False
        self.category.save()
        self.assert_not_listed()

    def test_deleted_category_excluded(self):
        self.category.is_deleted = True
        self.category.save()
        self.assert_not_listed()

    def test_no_eligible_variants_excluded(self):
        self.variant_50.is_deleted = True
        self.variant_50.save()
        self.variant_100.is_listed = False
        self.variant_100.save()
        self.assert_not_listed()

    def test_excluded_product_absent_from_search_too(self):
        self.product.is_deleted = True
        self.product.save()
        response = self.client.get(self.listing_url("q=Hawas"))
        self.assertNotIn("Hawas Ice", self.product_names_in_listing(response))


# ---------------------------------------------------------------------------
# 8-11. Pricing, representative variant, images
# ---------------------------------------------------------------------------

class ListingPricingVariantTest(ShopListingTestCase):

    def test_starting_price_is_minimum_eligible_variant_price(self):
        response = self.client.get(self.listing_url())
        product = next(p for p in response.context["products"] if p.name == "Hawas Ice")
        self.assertEqual(str(product.starting_price), "245.00")

    def test_unlisted_variant_price_ignored(self):
        ProductVariant.objects.create(
            product=self.product, size="10ml", sku="HAWAS-10",
            price="50.00", stock=3, is_listed=False, is_deleted=False,
        )
        response = self.client.get(self.listing_url())
        product = next(p for p in response.context["products"] if p.name == "Hawas Ice")
        self.assertEqual(str(product.starting_price), "245.00")

    def test_representative_size_is_earliest_eligible_variant(self):
        response = self.client.get(self.listing_url())
        product = next(p for p in response.context["products"] if p.name == "Hawas Ice")
        self.assertEqual(product.representative_size, "50ml")

    def test_representative_size_skips_ineligible_earlier_variant(self):
        self.variant_50.is_listed = False
        self.variant_50.save()
        response = self.client.get(self.listing_url())
        product = next(p for p in response.context["products"] if p.name == "Hawas Ice")
        self.assertEqual(product.representative_size, "100ml")

    def test_primary_image_follows_variant_then_display_order(self):
        VariantImage.objects.create(
            variant=self.variant_50, image=_fake_image("second.jpg"),
            alt_text="second", display_order=5,
        )
        VariantImage.objects.create(
            variant=self.variant_100, image=_fake_image("other.jpg"),
            alt_text="other variant", display_order=0,
        )
        response = self.client.get(self.listing_url())
        product = next(p for p in response.context["products"] if p.name == "Hawas Ice")
        self.image.refresh_from_db()
        self.assertEqual(product.primary_image, self.image.image.name)

    def test_zero_stock_product_remains_visible_with_indicator(self):
        self.variant_50.stock = 0
        self.variant_50.save()
        self.variant_100.stock = 0
        self.variant_100.save()
        response = self.client.get(self.listing_url())
        self.assertIn("Hawas Ice", self.product_names_in_listing(response))
        self.assertContains(response, "Out of Stock")


# ---------------------------------------------------------------------------
# 12-15. Search and filters
# ---------------------------------------------------------------------------

class ListingSearchFilterTest(ShopListingTestCase):

    def setUp(self):
        super().setUp()
        self.category2 = Category.objects.create(name="Niche Perfumes")
        self.brand2 = Brand.objects.create(
            category=self.category2, name="Lattafa",
            description="d", listing_image=_fake_image("l2.jpg"),
            listing_description="ld", hero_image=_fake_image("h2.jpg"),
            hero_description="hd",
        )
        self.product2 = Product.objects.create(
            brand=self.brand2, name="Khamrah Amber", slug="khamrah-amber",
            description="spicy amber scent.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
        )
        ProductVariant.objects.create(
            product=self.product2, size="100ml", sku="KHAM-100",
            price="120.00", stock=7,
        )

    def test_search_by_product_name(self):
        response = self.client.get(self.listing_url("q=Hawas"))
        names = self.product_names_in_listing(response)
        self.assertIn("Hawas Ice", names)
        self.assertNotIn("Khamrah Amber", names)

    def test_search_by_brand_name(self):
        response = self.client.get(self.listing_url("q=Rasasi"))
        names = self.product_names_in_listing(response)
        self.assertIn("Hawas Ice", names)
        self.assertNotIn("Khamrah Amber", names)

    def test_search_by_category_name(self):
        response = self.client.get(self.listing_url("q=Designer"))
        names = self.product_names_in_listing(response)
        self.assertIn("Hawas Ice", names)
        self.assertNotIn("Khamrah Amber", names)

    def test_search_retains_value(self):
        response = self.client.get(self.listing_url("q=Hawas"))
        self.assertEqual(response.context["current_q"], "Hawas")
        self.assertContains(response, 'value="Hawas"')

    def test_category_filter(self):
        response = self.client.get(
            self.listing_url(f"category={self.category2.slug}")
        )
        names = self.product_names_in_listing(response)
        self.assertIn("Khamrah Amber", names)
        self.assertNotIn("Hawas Ice", names)

    def test_brand_filter(self):
        response = self.client.get(
            self.listing_url(f"brand={self.brand.slug}")
        )
        names = self.product_names_in_listing(response)
        self.assertIn("Hawas Ice", names)
        self.assertNotIn("Khamrah Amber", names)

    def test_search_and_filters_combine(self):
        query = f"q=Hawas&brand={self.brand.slug}&category={self.category.slug}"
        response = self.client.get(self.listing_url(query))
        self.assertEqual(self.product_names_in_listing(response), ["Hawas Ice"])

    def test_conflicting_search_and_filter_yields_empty(self):
        query = f"q=Hawas&brand={self.brand2.slug}"
        response = self.client.get(self.listing_url(query))
        self.assertEqual(list(response.context["products"]), [])

    def test_filter_options_only_offer_eligible_choices(self):
        self.brand2.is_listed = False
        self.brand2.save()
        response = self.client.get(self.listing_url())
        brand_slugs = [b.slug for b in response.context["brands"]]
        self.assertIn(self.brand.slug, brand_slugs)
        self.assertNotIn(self.brand2.slug, brand_slugs)

    def test_hidden_category_never_offered(self):
        hidden = Category.objects.create(
            name="Hidden Gems", is_listed=False,
        )
        response = self.client.get(self.listing_url())
        category_slugs = [c.slug for c in response.context["categories"]]
        self.assertNotIn(hidden.slug, category_slugs)


# ---------------------------------------------------------------------------
# 16. Sorting
# ---------------------------------------------------------------------------

class ListingSortingTest(ShopListingTestCase):

    def setUp(self):
        super().setUp()
        # Rename fixture product so name ordering is deterministic.
        self.product.name = "Mango Zest"
        self.product.slug = "mango-zest"
        self.product.save()
        self.alpha = Product.objects.create(
            brand=self.brand, name="Alpha Musk", slug="alpha-musk",
            description="alpha.", fragrance_concentration="EDP",
        )
        ProductVariant.objects.create(
            product=self.alpha, size="50ml", sku="ALPHA-50",
            price="500.00", stock=4,
        )
        self.zulu = Product.objects.create(
            brand=self.brand, name="Zulu Oud", slug="zulu-oud",
            description="zulu.", fragrance_concentration="EDP",
        )
        ProductVariant.objects.create(
            product=self.zulu, size="50ml", sku="ZULU-50",
            price="99.00", stock=4,
        )

    def ordered_names(self, query):
        return self.product_names_in_listing(self.client.get(self.listing_url(query)))

    def test_name_ascending(self):
        self.assertEqual(
            self.ordered_names("sort=name_asc"),
            ["Alpha Musk", "Mango Zest", "Zulu Oud"],
        )

    def test_price_low_to_high(self):
        self.assertEqual(
            self.ordered_names("sort=price_low"),
            ["Zulu Oud", "Mango Zest", "Alpha Musk"],
        )

    def test_price_high_to_low(self):
        self.assertEqual(
            self.ordered_names("sort=price_high"),
            ["Alpha Musk", "Mango Zest", "Zulu Oud"],
        )

    def test_newest_first(self):
        # zulu created last, fixture product (renamed) first.
        self.assertEqual(
            self.ordered_names("sort=newest"),
            ["Zulu Oud", "Alpha Musk", "Mango Zest"],
        )

    def test_featured_default_is_deterministic(self):
        first = self.ordered_names("")
        second = self.ordered_names("")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)

    def test_unknown_sort_falls_back_to_default(self):
        self.assertEqual(
            self.ordered_names("sort=nonsense"),
            self.ordered_names(""),
        )

    def test_sort_preserves_search_and_filter(self):
        query = f"q=Oud&sort=price_low&brand={self.brand.slug}"
        response = self.client.get(self.listing_url(query))
        self.assertEqual(self.product_names_in_listing(response), ["Zulu Oud"])
        self.assertEqual(response.context["current_sort"], "price_low")


# ---------------------------------------------------------------------------
# 17. Pagination
# ---------------------------------------------------------------------------

class ListingPaginationTest(ShopListingTestCase):

    def setUp(self):
        super().setUp()
        for i in range(14):
            product = Product.objects.create(
                brand=self.brand, name=f"Paged Perfume {i:02d}",
                slug=f"paged-perfume-{i:02d}", description="paged.",
                fragrance_concentration="EDP",
            )
            ProductVariant.objects.create(
                product=product, size="50ml", sku=f"PAGED-{i:02d}",
                price=f"{100 + i}.00", stock=3,
            )

    def test_first_page_has_twelve_products(self):
        response = self.client.get(self.listing_url())
        self.assertEqual(len(response.context["products"]), 12)
        self.assertTrue(response.context["page_obj"].has_next())

    def test_second_page_holds_remainder(self):
        response = self.client.get(self.listing_url("page=2"))
        self.assertEqual(len(response.context["products"]), 3)
        self.assertFalse(response.context["page_obj"].has_next())

    def test_invalid_page_resolves_gracefully(self):
        for bad in ("not-a-number", "9999"):
            response = self.client.get(self.listing_url(f"page={bad}"))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(len(response.context["products"]) > 0)

    def test_pagination_preserves_query_params(self):
        query = f"q=Paged&sort=price_low&brand={self.brand.slug}&page=2"
        response = self.client.get(self.listing_url(query))
        self.assertEqual(response.status_code, 200)
        self.assertIn("q=Paged", response.context["base_query"])
        self.assertIn("sort=price_low", response.context["base_query"])
        self.assertIn(f"brand={self.brand.slug}", response.context["base_query"])
        self.assertContains(response, "page=1")

    def test_pagination_never_exposes_ineligible_products(self):
        hidden = Product.objects.create(
            brand=self.brand, name="Paged Hidden", slug="paged-hidden",
            description="hidden.", fragrance_concentration="EDP",
            is_listed=False,
        )
        ProductVariant.objects.create(
            product=hidden, size="50ml", sku="PAGED-HID",
            price="10.00", stock=3,
        )
        seen = set()
        for page in ("", "page=2"):
            response = self.client.get(self.listing_url(page))
            seen.update(self.product_names_in_listing(response))
        self.assertNotIn("Paged Hidden", seen)


# ---------------------------------------------------------------------------
# 18-21. Cards, navigation, empty states, missing images
# ---------------------------------------------------------------------------

class ListingPresentationTest(ShopListingTestCase):

    def test_product_card_links_to_details_route(self):
        response = self.client.get(self.listing_url())
        details = reverse("shop:product_details", kwargs={"slug": "hawas-ice"})
        self.assertContains(response, f'href="{details}"')

    def test_shared_shop_navigation_points_to_listing(self):
        for url in (reverse("home"), reverse("shop:shop_list")):
            response = self.client.get(url)
            self.assertContains(
                response, f'href="{reverse("shop:shop_list")}"'
            )

    def test_empty_search_shows_reset_action(self):
        response = self.client.get(self.listing_url("q=no-such-perfume-xyz"))
        self.assertEqual(list(response.context["products"]), [])
        self.assertContains(response, "No Fragrances Found")
        self.assertContains(response, "Clear Search")

    def test_empty_filters_show_reset_action(self):
        response = self.client.get(self.listing_url("brand=no-such-brand"))
        self.assertContains(response, "No Fragrances Found")

    def test_product_without_image_renders_fallback(self):
        self.image.delete()
        response = self.client.get(self.listing_url())
        self.assertEqual(response.status_code, 200)
        self.assertIn("Hawas Ice", self.product_names_in_listing(response))

    def test_hero_missing_file_keeps_page_functional(self):
        response = self.client.get(self.listing_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "images/shop_hero.png")


# ---------------------------------------------------------------------------
# 23. Concentration filtering + filter drawer
# ---------------------------------------------------------------------------

class ListingConcentrationDrawerTest(ShopListingTestCase):

    def setUp(self):
        super().setUp()
        self.edt_product = Product.objects.create(
            brand=self.brand, name="Citrus Splash", slug="citrus-splash",
            description="fresh daytime scent.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_TOILETTE,
        )
        ProductVariant.objects.create(
            product=self.edt_product, size="100ml", sku="CITRUS-100",
            price="150.00", stock=6,
        )

    def test_concentration_filter_single_value(self):
        response = self.client.get(self.listing_url("concentration=EDT"))
        names = self.product_names_in_listing(response)
        self.assertIn("Citrus Splash", names)
        self.assertNotIn("Hawas Ice", names)

    def test_concentration_filter_multiple_values_combine(self):
        response = self.client.get(
            self.listing_url("concentration=EDP&concentration=EDT")
        )
        names = self.product_names_in_listing(response)
        self.assertIn("Hawas Ice", names)
        self.assertIn("Citrus Splash", names)

    def test_invalid_concentration_ignored(self):
        response = self.client.get(self.listing_url("concentration=NOPE"))
        names = self.product_names_in_listing(response)
        self.assertIn("Hawas Ice", names)
        self.assertIn("Citrus Splash", names)
        self.assertEqual(response.context["current_concentrations"], [])

    def test_concentration_combines_with_brand_and_search(self):
        query = f"q=Citrus&brand={self.brand.slug}&concentration=EDT"
        response = self.client.get(self.listing_url(query))
        self.assertEqual(self.product_names_in_listing(response), ["Citrus Splash"])

    def test_multiple_brands_combine(self):
        brand2 = Brand.objects.create(
            category=self.category, name="Second House",
            description="d", listing_image=_fake_image("s2l.jpg"),
            listing_description="ld", hero_image=_fake_image("s2h.jpg"),
            hero_description="hd",
        )
        other = Product.objects.create(
            brand=brand2, name="Other Oud", slug="other-oud",
            description="o.", fragrance_concentration="EDP",
        )
        ProductVariant.objects.create(
            product=other, size="50ml", sku="OTHER-50",
            price="200.00", stock=2,
        )
        response = self.client.get(
            self.listing_url(f"brand={self.brand.slug}&brand={brand2.slug}")
        )
        names = self.product_names_in_listing(response)
        self.assertIn("Hawas Ice", names)
        self.assertIn("Other Oud", names)

    def test_drawer_renders_dynamic_options_with_checked_state(self):
        query = f"brand={self.brand.slug}&concentration=EDP"
        response = self.client.get(self.listing_url(query))
        # Dynamic brand/category names, never hardcoded.
        self.assertContains(response, self.brand.name)
        self.assertContains(response, self.category.name)
        # Real model concentration labels.
        self.assertContains(response, "Eau de Parfum (EDP)")
        self.assertContains(response, "Eau de Toilette (EDT)")
        # Active filters reflected as checked.
        self.assertContains(response, "checked")
        # Unsupported attributes must not appear as functional filters.
        self.assertNotContains(response, 'name="gender"')
        self.assertNotContains(response, 'name="scent_family"')

    def test_toolbar_preserves_params_across_forms(self):
        query = f"q=Hawas&sort=price_low&brand={self.brand.slug}"
        response = self.client.get(self.listing_url(query))
        self.assertContains(response, f'name="sort" value="price_low"')
        self.assertContains(response, f'name="q" value="Hawas"')
        self.assertContains(response, f'name="brand" value="{self.brand.slug}"')

    def test_pagination_preserves_concentration(self):
        response = self.client.get(self.listing_url("concentration=EDP"))
        self.assertIn("concentration=EDP", response.context["base_query"])


# ---------------------------------------------------------------------------
# 22. Home Page and Product Details routes remain functional
# ---------------------------------------------------------------------------

class RouteRegressionTest(ShopListingTestCase):

    def test_home_page_still_responds(self):
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_product_details_still_responds(self):
        url = reverse("shop:product_details", kwargs={"slug": "hawas-ice"})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_product_details_ineligible_still_404(self):
        self.product.is_deleted = True
        self.product.save()
        url = reverse("shop:product_details", kwargs={"slug": "hawas-ice"})
        self.assertEqual(self.client.get(url).status_code, 404)
