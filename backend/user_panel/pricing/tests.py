"""Phase 19.1 pricing engine tests."""

from decimal import Decimal

from django.test import TestCase

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant

from . import services as pricing


def make_catalogue(
    *,
    sku="SKU-PRICE-1",
    price="100.00",
    product_offer="0.00",
    category_offer="0.00",
):
    category = Category.objects.create(
        name=f"Category {sku}",
        description="Category description",
        offer_percentage=Decimal(category_offer),
    )
    brand = Brand.objects.create(
        category=category,
        name=f"Brand {sku}",
        description="Brand description",
        listing_image="brands/listing/test.jpg",
        listing_description="Listing description",
        hero_image="brands/hero/test.jpg",
        hero_description="Hero description",
    )
    product = Product.objects.create(
        brand=brand,
        name=f"Product {sku}",
        description="Product description",
        fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
        offer_percentage=Decimal(product_offer),
    )
    variant = ProductVariant.objects.create(
        product=product,
        size="50ml",
        sku=sku,
        price=Decimal(price),
        stock=10,
    )
    return category, brand, product, variant


class OfferResolutionTests(TestCase):
    def test_product_offer_only(self):
        _, _, _, variant = make_catalogue(product_offer="10.00")

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_PRODUCT, Decimal("10.00")),
        )

    def test_category_offer_only(self):
        _, _, _, variant = make_catalogue(category_offer="12.50")

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_CATEGORY, Decimal("12.50")),
        )

    def test_larger_product_offer_wins(self):
        _, _, _, variant = make_catalogue(
            product_offer="20.00",
            category_offer="15.00",
        )

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_PRODUCT, Decimal("20.00")),
        )

    def test_larger_category_offer_wins(self):
        _, _, _, variant = make_catalogue(
            product_offer="15.00",
            category_offer="20.00",
        )

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_CATEGORY, Decimal("20.00")),
        )

    def test_equal_non_zero_offer_prefers_product(self):
        _, _, _, variant = make_catalogue(
            product_offer="15.00",
            category_offer="15.00",
        )

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_PRODUCT, Decimal("15.00")),
        )

    def test_zero_offers_produce_no_discount(self):
        _, _, _, variant = make_catalogue()

        result = pricing.price_variant(variant, 3)

        self.assertEqual(result.offer_source, pricing.OFFER_SOURCE_NONE)
        self.assertEqual(result.offer_percentage, Decimal("0.00"))
        self.assertEqual(result.offer_discount, Decimal("0.00"))
        self.assertEqual(result.paid_amount, Decimal("300.00"))

    def test_variant_must_be_eligible(self):
        _, _, product, variant = make_catalogue(product_offer="10.00")

        variant.is_deleted = True
        variant.save(update_fields=["is_deleted"])

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_NONE, Decimal("0.00")),
        )

        variant.is_deleted = False
        variant.save(update_fields=["is_deleted"])
        product.is_listed = False
        product.save(update_fields=["is_listed"])

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_NONE, Decimal("0.00")),
        )

    def test_category_must_be_eligible(self):
        category, _, _, variant = make_catalogue(category_offer="10.00")

        category.is_deleted = True
        category.save(update_fields=["is_deleted"])

        self.assertEqual(
            pricing.resolve_offer(variant),
            (pricing.OFFER_SOURCE_NONE, Decimal("0.00")),
        )


class LinePricingTests(TestCase):
    def test_quantity_math_uses_line_total(self):
        _, _, _, variant = make_catalogue(
            price="1200.00",
            product_offer="10.00",
        )

        result = pricing.price_variant(variant, 2)

        self.assertEqual(result.line_total, Decimal("2400.00"))
        self.assertEqual(result.offer_discount, Decimal("240.00"))
        self.assertEqual(result.paid_amount, Decimal("2160.00"))

    def test_discount_rounds_half_up_per_line(self):
        _, _, _, variant = make_catalogue(
            price="99.99",
            product_offer="5.00",
        )

        result = pricing.price_variant(variant, 1)

        # 99.99 * 5% = 4.9995 -> 5.00 with ROUND_HALF_UP.
        self.assertEqual(result.offer_discount, Decimal("5.00"))
        self.assertEqual(result.paid_amount, Decimal("94.99"))

    def test_summarize_matches_line_snapshots(self):
        _, _, _, first = make_catalogue(
            sku="SKU-PRICE-A",
            price="100.00",
            product_offer="10.00",
        )
        _, _, _, second = make_catalogue(
            sku="SKU-PRICE-B",
            price="50.00",
            category_offer="20.00",
        )

        lines = [
            pricing.price_variant(first, 2),
            pricing.price_variant(second, 1),
        ]
        totals = pricing.summarize(lines)

        self.assertEqual(totals["subtotal"], Decimal("250.00"))
        self.assertEqual(totals["offer_discount_total"], Decimal("30.00"))
        self.assertEqual(totals["coupon_discount_total"], Decimal("0.00"))
        self.assertEqual(totals["discount_total"], Decimal("30.00"))
        self.assertEqual(totals["total"], Decimal("220.00"))
        self.assertEqual(
            sum((line.paid_amount for line in lines), Decimal("0.00")),
            totals["total"],
        )
