"""Tests for AURA Image Standard v1: selective processing, safe replacement.

Covers: image_processing module, VariantCreate/Update image merge, ordering,
final 3-5 count, on_commit file safety, background preservation, and the
no-model-change guarantee.
"""

from io import BytesIO

import tempfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from PIL import Image

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category

from . import image_processing
from .image_processing import (
    render_standard_image,
    validate_image_file,
)
from .models import Product, ProductVariant, VariantImage


def make_image_bytes(width, height, color=(200, 30, 30), fmt="JPEG"):
    mode = "RGBA" if fmt == "PNG" and len(color) == 4 else "RGB"
    image = Image.new(mode, (width, height), color)
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def make_upload(name, width=800, height=800, color=(200, 30, 30), fmt="JPEG"):
    mime = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }[fmt]
    return SimpleUploadedFile(
        name,
        make_image_bytes(width, height, color=color, fmt=fmt),
        content_type=mime,
    )


class ValidateImageFileTests(SimpleTestCase):

    def test_valid_jpeg_passes(self):
        self.assertTrue(
            validate_image_file(make_upload("a.jpg"))
        )

    def test_valid_png_and_webp_pass(self):
        self.assertTrue(
            validate_image_file(make_upload("a.png", fmt="PNG"))
        )
        self.assertTrue(
            validate_image_file(make_upload("a.webp", fmt="WEBP"))
        )

    def test_wrong_mime_rejected(self):
        bad = SimpleUploadedFile(
            "a.gif",
            make_image_bytes(100, 100),
            content_type="image/gif",
        )
        with self.assertRaises(ValidationError):
            validate_image_file(bad)

    def test_corrupt_bytes_rejected(self):
        bad = SimpleUploadedFile(
            "a.jpg",
            b"not-an-image-at-all",
            content_type="image/jpeg",
        )
        with self.assertRaises(ValidationError):
            validate_image_file(bad)

    def test_pixel_cap_rejected(self):
        with self.settings():
            original = image_processing.MAX_IMAGE_PIXELS
            image_processing.MAX_IMAGE_PIXELS = 100
            try:
                with self.assertRaises(ValidationError):
                    validate_image_file(
                        make_upload("a.jpg", width=200, height=200)
                    )
            finally:
                image_processing.MAX_IMAGE_PIXELS = original


class RenderStandardImageTests(SimpleTestCase):

    def test_wide_source_center_crop(self):
        upload = make_upload("wide.jpg", width=2000, height=1000)
        result = render_standard_image(
            upload,
            {"left": 0.25, "top": 0.0, "side": 0.5},
        )
        image = Image.open(BytesIO(result.read()))
        self.assertEqual(image.size, (1200, 1200))

    def test_portrait_source_crop(self):
        upload = make_upload("tall.jpg", width=1000, height=2000)
        result = render_standard_image(
            upload,
            {"left": 0.0, "top": 0.25, "side": 1.0},
        )
        image = Image.open(BytesIO(result.read()))
        self.assertEqual(image.size, (1200, 1200))

    def test_square_source_full_crop(self):
        upload = make_upload("sq.jpg", width=900, height=900)
        result = render_standard_image(
            upload,
            {"left": 0.0, "top": 0.0, "side": 1.0},
        )
        image = Image.open(BytesIO(result.read()))
        self.assertEqual(image.size, (1200, 1200))

    def _two_tone_wide(self):
        image = Image.new("RGB", (2000, 1000))
        image.paste(Image.new("RGB", (1000, 1000), (255, 0, 0)), (0, 0))
        image.paste(Image.new("RGB", (1000, 1000), (0, 0, 255)), (1000, 0))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=100)
        return buffer.getvalue()

    def test_crop_left_half(self):
        payload = self._two_tone_wide()
        out = Image.open(
            BytesIO(
                render_standard_image(
                    SimpleUploadedFile(
                        "t.jpg", payload, content_type="image/jpeg"
                    ),
                    {"left": 0.0, "top": 0.0, "side": 0.5},
                ).read()
            )
        )
        self.assertEqual(out.size, (1200, 1200))
        self.assertGreater(out.getpixel((600, 600))[0], 150)
        self.assertLess(out.getpixel((600, 600))[2], 100)

    def test_crop_right_half(self):
        payload = self._two_tone_wide()
        out = Image.open(
            BytesIO(
                render_standard_image(
                    SimpleUploadedFile(
                        "t.jpg", payload, content_type="image/jpeg"
                    ),
                    {"left": 0.5, "top": 0.0, "side": 0.5},
                ).read()
            )
        )
        self.assertEqual(out.size, (1200, 1200))
        self.assertGreater(out.getpixel((600, 600))[2], 150)
        self.assertLess(out.getpixel((600, 600))[0], 100)

    def test_crop_center_shows_boundary_undistorted(self):
        # Center square straddles the red/blue boundary: left half of the
        # output must be red-ish and right half blue-ish (proportional,
        # undistorted resize of the selected square).
        payload = self._two_tone_wide()
        out = Image.open(
            BytesIO(
                render_standard_image(
                    SimpleUploadedFile(
                        "t.jpg", payload, content_type="image/jpeg"
                    ),
                    {"left": 0.25, "top": 0.0, "side": 0.5},
                ).read()
            )
        )
        self.assertEqual(out.size, (1200, 1200))
        self.assertGreater(out.getpixel((300, 600))[0], 150)
        self.assertGreater(out.getpixel((900, 600))[2], 150)

    def test_out_of_range_crop_is_clamped_inside(self):
        upload = make_upload("sq.jpg", width=1000, height=1000)
        out = Image.open(
            BytesIO(
                render_standard_image(
                    upload,
                    {"left": 0.9, "top": 0.9, "side": 0.5},
                ).read()
            )
        )
        self.assertEqual(out.size, (1200, 1200))

    def test_crop_matches_editor_viewport_math(self):
        # Mirror of the frontend crop-frame mapping for a 2000x3000
        # portrait source at reset (zoom = minimum covering the frame):
        # stage 600, frame 480 => full-width square (0, 500, 2000, 2000).
        image = Image.new("RGB", (2000, 3000), (255, 0, 0))
        image.paste(
            Image.new("RGB", (2000, 500), (0, 0, 255)), (0, 0)
        )
        image.paste(
            Image.new("RGB", (2000, 500), (0, 0, 255)), (0, 2500)
        )
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=100)
        upload = SimpleUploadedFile(
            "p.jpg", buffer.getvalue(), content_type="image/jpeg"
        )
        out = Image.open(
            BytesIO(
                render_standard_image(
                    upload,
                    {"left": 0.0, "top": 500 / 3000, "side": 1.0},
                ).read()
            )
        )
        self.assertEqual(out.size, (1200, 1200))
        for point in [(0, 0), (1199, 0), (0, 1199), (1199, 1199), (600, 600)]:
            pixel = out.getpixel(point)
            self.assertGreater(pixel[0], 150)
            self.assertLess(pixel[2], 100)

    def test_background_preserved_no_fill(self):
        # Solid ivory background must survive byte-similar (no white fill,
        # no edge-sample fill: output corners equal the source color).
        ivory = (245, 240, 230)
        upload = make_upload(
            "ivory.jpg", width=1500, height=900, color=ivory
        )
        out = Image.open(
            BytesIO(
                render_standard_image(
                    upload,
                    {"left": 0.2, "top": 0.0, "side": 0.6},
                ).read()
            )
        )
        for point in [(0, 0), (1199, 0), (0, 1199), (1199, 1199)]:
            pixel = out.getpixel(point)
            for channel, expected in zip(pixel[:3], ivory):
                self.assertAlmostEqual(channel, expected, delta=8)

    def test_transparent_png_keeps_alpha(self):
        upload = make_upload(
            "t.png",
            width=900,
            height=900,
            color=(10, 20, 30, 0),
            fmt="PNG",
        )
        out = Image.open(
            BytesIO(
                render_standard_image(
                    upload,
                    {"left": 0.0, "top": 0.0, "side": 1.0},
                ).read()
            )
        )
        self.assertEqual(out.size, (1200, 1200))
        self.assertIn(out.mode, ("RGBA", "LA", "PA"))
        self.assertEqual(out.getpixel((0, 0))[3], 0)

    def test_jpeg_stays_jpeg(self):
        upload = make_upload("a.jpg", width=900, height=700)
        out = Image.open(
            BytesIO(
                render_standard_image(
                    upload,
                    {"left": 0.1, "top": 0.0, "side": 0.8},
                ).read()
            )
        )
        self.assertEqual(out.format, "JPEG")
        self.assertEqual(out.size, (1200, 1200))


class VariantImageFlowTests(TestCase):

    def setUp(self):
        self._media_dir = tempfile.mkdtemp(prefix="aura_test_media_")
        self._override = override_settings(MEDIA_ROOT=self._media_dir)
        self._override.enable()
        self.addCleanup(self._override.disable)
        category = Category.objects.create(name="Designer")
        brand = Brand.objects.create(
            category=category,
            name="TestBrand",
            description="desc",
            listing_image=make_upload("listing.jpg", 400, 400),
            listing_description="listing",
            hero_image=make_upload("hero.jpg", 400, 400),
            hero_description="hero",
        )
        self.product = Product.objects.create(
            brand=brand,
            name="Test Product",
            description="desc",
            fragrance_concentration="EDP",
        )

    def _create_variant(self, colors=((200, 30, 30), (30, 200, 30), (30, 30, 200))):
        variant = ProductVariant.objects.create(
            product=self.product,
            size="50ml",
            sku="SKU-001",
            price="100.00",
            stock=10,
        )
        images = []
        for index, color in enumerate(colors):
            images.append(
                VariantImage.objects.create(
                    variant=variant,
                    image=make_upload(
                        f"img{index}.jpg", color=color
                    ),
                    display_order=index,
                )
            )
        return variant, images

    def _post_update(self, variant, data, files=None):
        from django.test import Client

        client = Client()
        url = f"/admin-panel/products/variants/{variant.id}/edit/"
        payload = dict(data)
        if files:
            payload.update(files)
        return client.post(url, payload)

    def test_untouched_images_byte_identical(self):
        variant, images = self._create_variant()
        before = {
            img.id: (img.image.name, img.image.read())
            for img in images
        }
        response = self._post_update(
            variant,
            {
                "size": "50ml",
                "sku": "SKU-001",
                "price": "100.00",
                "stock": "10",
            },
        )
        self.assertEqual(response.status_code, 200)
        for img in VariantImage.objects.filter(variant=variant):
            name, content = before[img.id]
            self.assertEqual(img.image.name, name)
            img.image.open()
            self.assertEqual(img.image.read(), content)

    def test_combined_flow_with_new_file(self):
        variant, images = self._create_variant()
        img_a, img_b, img_c = images
        old_a_name = img_a.image.name
        old_b_name = img_b.image.name
        img_c.image.open()
        c_content = img_c.image.read()
        c_name = img_c.image.name

        edited_a = make_upload(
            "a_edited.jpg", width=1200, height=1200, color=(9, 9, 9)
        )
        new_d = make_upload("d_new.jpg", color=(100, 100, 5))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/admin-panel/products/variants/{variant.id}/edit/",
                data={
                    "size": "50ml",
                    "sku": "SKU-001",
                    "price": "120.00",
                    "stock": "7",
                    "remove_image_ids": [str(img_b.id)],
                    "image_order": [
                        "n:newD",
                        f"e:{img_a.id}",
                        f"e:{img_c.id}",
                    ],
                    "images": [new_d],
                    "new_image_keys": ["newD"],
                    f"edited_image_{img_a.id}": edited_a,
                },
            )
        self.assertEqual(response.status_code, 200)

        final = list(
            VariantImage.objects.filter(variant=variant).order_by(
                "display_order"
            )
        )
        self.assertEqual(len(final), 3)
        self.assertEqual(
            [img.display_order for img in final], [0, 1, 2]
        )

        # D first (primary), A replaced second, C untouched third.
        final[0].image.open()
        new_d.seek(0)
        self.assertEqual(final[0].image.read(), new_d.read())
        self.assertNotEqual(final[1].image.name, old_a_name)
        final[1].image.open()
        edited_a.seek(0)
        self.assertEqual(final[1].image.read(), edited_a.read())
        self.assertEqual(final[2].image.name, c_name)
        final[2].image.open()
        self.assertEqual(final[2].image.read(), c_content)

        # Old physical files deleted only after success.
        from django.core.files.storage import default_storage

        self.assertFalse(default_storage.exists(old_a_name))
        self.assertFalse(default_storage.exists(old_b_name))

    def test_staged_order_without_changes_leaves_files(self):
        variant, images = self._create_variant()
        before = {
            img.id: (img.image.name, img.image.read()) for img in images
        }
        response = self.client.post(
            f"/admin-panel/products/variants/{variant.id}/edit/",
            data={
                "size": "50ml",
                "sku": "SKU-001",
                "price": "100.00",
                "stock": "10",
                "image_order": [f"e:{img.id}" for img in images],
            },
        )
        self.assertEqual(response.status_code, 200)
        for img in VariantImage.objects.filter(variant=variant):
            name, content = before[img.id]
            self.assertEqual(img.image.name, name)
            img.image.open()
            self.assertEqual(img.image.read(), content)

    def test_remove_below_minimum_rejected(self):
        variant, images = self._create_variant()
        response = self.client.post(
            f"/admin-panel/products/variants/{variant.id}/edit/",
            data={
                "size": "50ml",
                "sku": "SKU-001",
                "price": "100.00",
                "stock": "10",
                "remove_image_ids": [str(images[0].id)],
                "image_order": [
                    f"e:{images[1].id}",
                    f"e:{images[2].id}",
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            VariantImage.objects.filter(variant=variant).count(), 3
        )

    def test_invalid_replacement_leaves_original(self):
        from django.core.files.storage import default_storage

        variant, images = self._create_variant()
        img_a = images[0]
        old_name = img_a.image.name
        bad = SimpleUploadedFile(
            "bad.jpg", b"corrupt", content_type="image/jpeg"
        )
        response = self.client.post(
            f"/admin-panel/products/variants/{variant.id}/edit/",
            data={
                "size": "50ml",
                "sku": "SKU-001",
                "price": "100.00",
                "stock": "10",
                "image_order": [f"e:{img.id}" for img in images],
                f"edited_image_{img_a.id}": bad,
            },
        )
        self.assertEqual(response.status_code, 400)
        img_a.refresh_from_db()
        self.assertEqual(img_a.image.name, old_name)
        self.assertTrue(default_storage.exists(old_name))

    def test_fallback_renders_crop_when_blob_not_square(self):
        import json

        variant, images = self._create_variant()
        img_a = images[0]
        old_name = img_a.image.name
        # Non-square blob with crop params: server re-renders the selected
        # square from the stored original instead of trusting the blob.
        offspec = make_upload("offspec.jpg", width=800, height=800)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/admin-panel/products/variants/{variant.id}/edit/",
                data={
                    "size": "50ml",
                    "sku": "SKU-001",
                    "price": "100.00",
                    "stock": "10",
                    "image_order": [f"e:{img.id}" for img in images],
                    f"edited_image_{img_a.id}": offspec,
                    f"transform_{img_a.id}": json.dumps(
                        {"left": 0.0, "top": 0.0, "side": 1.0}
                    ),
                },
            )
        self.assertEqual(response.status_code, 200)
        img_a.refresh_from_db()
        self.assertNotEqual(img_a.image.name, old_name)
        img_a.image.open()
        rendered = Image.open(BytesIO(img_a.image.read()))
        self.assertEqual(rendered.size, (1200, 1200))


class NoModelChangeTests(SimpleTestCase):

    def test_variant_image_schema_unchanged(self):
        field_names = {
            field.name for field in VariantImage._meta.get_fields()
        }
        self.assertNotIn("is_primary", field_names)
        self.assertNotIn("is_deleted", field_names)
        self.assertIn("display_order", field_names)


class VariantCreateOrderTests(TestCase):

    def setUp(self):
        self._media_dir = tempfile.mkdtemp(prefix="aura_test_media_")
        self._override = override_settings(MEDIA_ROOT=self._media_dir)
        self._override.enable()
        self.addCleanup(self._override.disable)
        category = Category.objects.create(name="Designer")
        brand = Brand.objects.create(
            category=category,
            name="TestBrand",
            description="desc",
            listing_image=make_upload("listing.jpg", 400, 400),
            listing_description="listing",
            hero_image=make_upload("hero.jpg", 400, 400),
            hero_description="hero",
        )
        self.product = Product.objects.create(
            brand=brand,
            name="Test Product",
            description="desc",
            fragrance_concentration="EDP",
        )

    def test_create_respects_staged_order(self):
        red = make_upload("red.jpg", color=(255, 0, 0))
        green = make_upload("green.jpg", color=(0, 255, 0))
        blue = make_upload("blue.jpg", color=(0, 0, 255))
        response = self.client.post(
            f"/admin-panel/products/{self.product.id}/variants/add/",
            data={
                "size": "100ml",
                "sku": "SKU-ORDER-1",
                "price": "200.00",
                "stock": "5",
                "is_listed": "on",
                "image_order": ["n:k3", "n:k1", "n:k2"],
                "new_image_keys": ["k1", "k2", "k3"],
                "images": [red, green, blue],
            },
        )
        self.assertEqual(response.status_code, 201)
        variant = ProductVariant.objects.get(sku="SKU-ORDER-1")
        ordered = list(
            VariantImage.objects.filter(variant=variant).order_by(
                "display_order"
            )
        )
        self.assertEqual(
            [img.display_order for img in ordered], [0, 1, 2]
        )
        # k3 (blue) first, then k1 (red), then k2 (green).
        ordered[0].image.open()
        blue.seek(0)
        self.assertEqual(ordered[0].image.read(), blue.read())
        ordered[1].image.open()
        red.seek(0)
        self.assertEqual(ordered[1].image.read(), red.read())
        ordered[2].image.open()
        green.seek(0)
        self.assertEqual(ordered[2].image.read(), green.read())

    def test_create_rejects_inconsistent_order(self):
        response = self.client.post(
            f"/admin-panel/products/{self.product.id}/variants/add/",
            data={
                "size": "100ml",
                "sku": "SKU-ORDER-2",
                "price": "200.00",
                "stock": "5",
                "image_order": ["n:k1", "n:MISSING", "n:k3"],
                "new_image_keys": ["k1", "k2", "k3"],
                "images": [
                    make_upload("a.jpg"),
                    make_upload("b.jpg"),
                    make_upload("c.jpg"),
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            ProductVariant.objects.filter(sku="SKU-ORDER-2").exists()
        )


class ProductCreateFirstVariantTests(TestCase):

    def setUp(self):
        self._media_dir = tempfile.mkdtemp(prefix="aura_test_media_")
        self._override = override_settings(MEDIA_ROOT=self._media_dir)
        self._override.enable()
        self.addCleanup(self._override.disable)
        category = Category.objects.create(name="Designer")
        self.brand = Brand.objects.create(
            category=category,
            name="TestBrand",
            description="desc",
            listing_image=make_upload("listing.jpg", 400, 400),
            listing_description="listing",
            hero_image=make_upload("hero.jpg", 400, 400),
            hero_description="hero",
        )

    def test_product_create_with_ordered_images(self):
        response = self.client.post(
            "/admin-panel/products/add/",
            data={
                "name": "Ordered Product",
                "brand": str(self.brand.id),
                "description": "desc",
                "fragrance_concentration": "EDP",
                "variant_size": "50ml",
                "variant_sku": "SKU-FIRST-1",
                "variant_price": "150.00",
                "variant_stock": "8",
                "image_order": ["n:k2", "n:k3", "n:k1"],
                "new_image_keys": ["k1", "k2", "k3"],
                "variant_images": [
                    make_upload("r.jpg", color=(255, 0, 0)),
                    make_upload("g.jpg", color=(0, 255, 0)),
                    make_upload("b.jpg", color=(0, 0, 255)),
                ],
            },
        )
        self.assertEqual(response.status_code, 201)
        variant = ProductVariant.objects.get(sku="SKU-FIRST-1")
        ordered = list(
            VariantImage.objects.filter(variant=variant).order_by(
                "display_order"
            )
        )
        self.assertEqual(len(ordered), 3)
        ordered[0].image.open()
        pixel = Image.open(BytesIO(ordered[0].image.read())).getpixel(
            (10, 10)
        )
        # k2 = green.jpg uploaded second, ordered first.
        self.assertGreater(pixel[1], 150)


class AdminPageSmokeTests(TestCase):

    def setUp(self):
        category = Category.objects.create(name="Designer")
        brand = Brand.objects.create(
            category=category,
            name="TestBrand",
            description="desc",
            listing_image=make_upload("listing.jpg", 400, 400),
            listing_description="listing",
            hero_image=make_upload("hero.jpg", 400, 400),
            hero_description="hero",
        )
        self.product = Product.objects.create(
            brand=brand,
            name="Smoke Product",
            description="desc",
            fragrance_concentration="EDP",
        )
        variant = ProductVariant.objects.create(
            product=self.product,
            size="50ml",
            sku="SKU-SMOKE-1",
            price="100.00",
            stock=10,
        )
        VariantImage.objects.create(
            variant=variant,
            image=make_upload("s1.jpg"),
            display_order=0,
        )

    def test_variant_management_renders(self):
        import json
        import re

        response = self.client.get(
            f"/admin-panel/products/{self.product.id}/variants/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Image Editor")
        self.assertContains(response, "data-edit-variant")
        match = re.search(
            r"data-variant-images='(\[.*?\])'", response.content.decode()
        )
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        self.assertEqual(len(payload), 1)
        self.assertIn("url", payload[0])
        self.assertIn("id", payload[0])

    def test_product_add_renders(self):
        response = self.client.get("/admin-panel/products/add/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Image Editor")
        self.assertContains(response, "variant-images-list")
