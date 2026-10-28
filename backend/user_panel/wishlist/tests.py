from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant

from .models import WishlistItem


User = get_user_model()


class WishlistItemModelTest(TestCase):
    """Model-level tests for the variant-level WishlistItem."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="wishlist_alice",
            password="test-pass-12345",
        )
        self.other_user = User.objects.create_user(
            username="wishlist_bob",
            password="test-pass-12345",
        )

        self.category = Category.objects.create(name="Designer Fragrances")
        self.brand = Brand.objects.create(
            category=self.category,
            name="Rasasi",
            description="Test brand description",
            listing_image="test/listing.jpg",
            listing_description="Test listing description",
            hero_image="test/hero.jpg",
            hero_description="Test hero description",
        )
        self.product = Product.objects.create(
            brand=self.brand,
            name="Hawas Ice",
            slug="hawas-ice",
            short_description="A short description.",
            description="A detailed description.",
            fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
        )
        self.variant_50 = ProductVariant.objects.create(
            product=self.product,
            size="50ml",
            sku="WISHLIST-HAWAS-50",
            price="245.00",
            stock=10,
        )
        self.variant_100 = ProductVariant.objects.create(
            product=self.product,
            size="100ml",
            sku="WISHLIST-HAWAS-100",
            price="380.00",
            stock=5,
        )

    def test_can_create_wishlist_entry(self):
        item = WishlistItem.objects.create(
            user=self.user,
            variant=self.variant_50,
        )

        self.assertEqual(item.user, self.user)
        self.assertEqual(item.variant, self.variant_50)
        self.assertIsNotNone(item.added_at)

    def test_snapshot_fields_are_populated_from_variant(self):
        item = WishlistItem.objects.create(
            user=self.user,
            variant=self.variant_50,
        )

        self.assertEqual(item.product_name, "Hawas Ice")
        self.assertEqual(item.brand_name, "Rasasi")
        self.assertEqual(item.variant_size, "50ml")

    def test_explicit_snapshot_values_are_preserved(self):
        item = WishlistItem.objects.create(
            user=self.user,
            variant=self.variant_50,
            product_name="Custom Product",
            brand_name="Custom Brand",
            variant_size="Custom Size",
        )

        self.assertEqual(item.product_name, "Custom Product")
        self.assertEqual(item.brand_name, "Custom Brand")
        self.assertEqual(item.variant_size, "Custom Size")

    def test_duplicate_entry_for_same_user_and_variant_is_rejected(self):
        WishlistItem.objects.create(user=self.user, variant=self.variant_50)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WishlistItem.objects.create(
                    user=self.user,
                    variant=self.variant_50,
                )

        self.assertEqual(WishlistItem.objects.count(), 1)

    def test_different_users_can_save_the_same_variant(self):
        WishlistItem.objects.create(user=self.user, variant=self.variant_50)
        WishlistItem.objects.create(user=self.other_user, variant=self.variant_50)

        self.assertEqual(WishlistItem.objects.count(), 2)

    def test_same_user_can_save_different_variants_of_same_product(self):
        WishlistItem.objects.create(user=self.user, variant=self.variant_50)
        WishlistItem.objects.create(user=self.user, variant=self.variant_100)

        self.assertEqual(WishlistItem.objects.count(), 2)

    def test_hard_deleted_variant_sets_null_and_preserves_snapshot(self):
        item = WishlistItem.objects.create(
            user=self.user,
            variant=self.variant_50,
        )

        self.variant_50.delete()
        item.refresh_from_db()

        self.assertIsNone(item.variant_id)
        self.assertIsNone(item.variant)
        self.assertEqual(item.product_name, "Hawas Ice")
        self.assertEqual(item.brand_name, "Rasasi")
        self.assertEqual(item.variant_size, "50ml")

    def test_soft_deleted_variant_keeps_its_reference(self):
        item = WishlistItem.objects.create(
            user=self.user,
            variant=self.variant_50,
        )

        self.variant_50.is_deleted = True
        self.variant_50.is_listed = False
        self.variant_50.save(
            update_fields=["is_deleted", "is_listed", "updated_at"]
        )
        item.refresh_from_db()

        self.assertEqual(item.variant_id, self.variant_50.id)
        self.assertIsNotNone(item.variant)

    def test_entry_without_variant_is_allowed(self):
        item = WishlistItem.objects.create(
            user=self.user,
            variant=None,
            product_name="Removed Product",
            brand_name="Removed Brand",
            variant_size="50ml",
        )

        self.assertIsNone(item.variant)
        self.assertEqual(item.product_name, "Removed Product")

    def test_unique_constraint_is_declared(self):
        constraint_names = {
            constraint.name for constraint in WishlistItem._meta.constraints
        }

        self.assertIn(
            "unique_wishlist_variant_per_user",
            constraint_names,
        )
