"""Phase 6.6 PDF invoice generation tests."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from reportlab import rl_config

from admin_panel.brands.models import Brand
from admin_panel.categories.models import Category
from admin_panel.products.models import Product, ProductVariant
from user_panel.orders.financials import get_order_financials
from user_panel.orders.invoices import (
    InvoiceContext,
    InvoiceLine,
    build_invoice_context,
    build_invoice_filename,
    is_invoice_available,
    render_invoice_pdf,
)
from user_panel.orders.models import (
    Order,
    OrderItem,
    ReturnItem,
    ReturnRequest,
)

User = get_user_model()


def make_user(username="invoice_customer", **overrides):
    values = {
        "username": username,
        "email": f"{username}@example.com",
        "password": "InvoicePass123!",
    }
    values.update(overrides)
    return User.objects.create_user(**values)


def make_catalogue(*, sku="SKU-INV-001", price="1500.00", stock=20, name="Ambre Noir"):
    category = Category.objects.create(
        name=f"Category {sku}",
        description="Invoice category",
    )
    brand = Brand.objects.create(
        category=category,
        name=f"Brand {sku}",
        description="Invoice brand",
        listing_image="brands/listing/test.jpg",
        listing_description="Listing description",
        hero_image="brands/hero/test.jpg",
        hero_description="Hero description",
    )
    product = Product.objects.create(
        brand=brand,
        name=name,
        description="Invoice product",
        fragrance_concentration=Product.FragranceConcentration.EAU_DE_PARFUM,
    )
    variant = ProductVariant.objects.create(
        product=product,
        size="100ml",
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )
    return category, brand, product, variant


def make_variant(product, *, sku, price="1200.00", stock=20, size="50ml"):
    return ProductVariant.objects.create(
        product=product,
        size=size,
        sku=sku,
        price=Decimal(price),
        stock=stock,
    )


def make_order(
    user,
    order_number,
    *,
    subtotal="3000.00",
    discount_total="0.00",
    shipping_charge="0.00",
    tax_total="0.00",
    total="3000.00",
    status=None,
    payment_status=None,
    payment_collected_at=None,
    payment_collected_amount=None,
):
    order_discount = Decimal(discount_total)
    return Order.objects.create(
        user=user,
        order_number=order_number,
        status=status or Order.OrderStatus.DELIVERED,
        payment_method=Order.PaymentMethod.COD,
        payment_status=payment_status or Order.PaymentStatus.PAID,
        payment_collected_at=payment_collected_at,
        payment_collected_amount=payment_collected_amount,
        subtotal=Decimal(subtotal),
        discount_total=order_discount,
        offer_discount_total=order_discount,
        shipping_charge=Decimal(shipping_charge),
        tax_total=Decimal(tax_total),
        total=Decimal(total),
        delivery_full_name="Jane Doe",
        delivery_phone_number="9876543210",
        delivery_address_line1="42 Amber Boulevard",
        delivery_address_line2="Apt 4B",
        delivery_landmark="Near Clocktower",
        delivery_city="Bengaluru",
        delivery_state="Karnataka",
        delivery_pincode="560001",
        delivery_address_type=Order.DeliveryAddressType.HOME,
    )


def add_item(
    order,
    variant,
    *,
    quantity=1,
    product_name=None,
    brand_name=None,
    unit_price=None,
    cancelled_at=None,
    cancellation_reason="",
):
    price = Decimal(unit_price) if unit_price is not None else variant.price
    return OrderItem.objects.create(
        order=order,
        variant=variant,
        product_name=product_name or variant.product.name,
        brand_name=brand_name or variant.product.brand.name,
        variant_size=variant.size,
        sku=variant.sku,
        unit_price=price,
        quantity=quantity,
        line_total=price * quantity,
        cancelled_at=cancelled_at,
        cancellation_reason=cancellation_reason,
    )


class InvoiceEligibilityTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.order = make_order(
            self.user,
            "AURA-20261104-INV00001",
            status=Order.OrderStatus.PENDING,
        )
        self.item = add_item(self.order, self.variant)

    def test_only_delivered_orders_are_eligible(self):
        outcomes = {
            Order.OrderStatus.PENDING: False,
            Order.OrderStatus.SHIPPED: False,
            Order.OrderStatus.OUT_FOR_DELIVERY: False,
            Order.OrderStatus.CANCELLED: False,
            Order.OrderStatus.DELIVERED: True,
        }

        for status, expected in outcomes.items():
            self.order.status = status
            self.assertEqual(is_invoice_available(self.order), expected)

    def test_cancelled_item_does_not_change_delivered_eligibility(self):
        self.order.status = Order.OrderStatus.DELIVERED
        self.order.save(update_fields=["status"])
        self.item.cancelled_at = timezone.now()
        self.item.cancellation_reason = "Cancelled before shipment"
        self.item.save(update_fields=["cancelled_at", "cancellation_reason"])

        self.assertTrue(is_invoice_available(self.order))

    def test_invoice_number_and_filename(self):
        context = build_invoice_context(self.order)
        self.assertEqual(
            context.invoice_number,
            "INV-AURA-20261104-INV00001",
        )
        self.assertEqual(
            build_invoice_filename(self.order),
            "invoice-INV-AURA-20261104-INV00001.pdf",
        )


class InvoiceContextTests(TestCase):
    def setUp(self):
        self.user = make_user("context_customer")
        self.category, self.brand, self.product, self.variant = make_catalogue()
        self.second_variant = make_variant(
            self.product,
            sku="SKU-INV-002",
            price="1200.00",
        )
        self.order = make_order(
            self.user,
            "AURA-20261104-CTX00001",
            subtotal="4200.00",
            discount_total="100.00",
            shipping_charge="50.00",
            tax_total="0.00",
            total="4150.00",
            status=Order.OrderStatus.DELIVERED,
            payment_collected_at=timezone.now(),
            payment_collected_amount=Decimal("4150.00"),
        )
        self.item1 = add_item(
            self.order,
            self.variant,
            quantity=2,
            unit_price="1500.00",
        )
        self.item2 = add_item(
            self.order,
            self.second_variant,
            quantity=1,
            unit_price="1200.00",
        )

    def test_original_order_totals_and_item_snapshots_are_used(self):
        context = build_invoice_context(self.order)

        self.assertEqual(context.subtotal, Decimal("4200.00"))
        self.assertEqual(context.discount_total, Decimal("100.00"))
        self.assertEqual(context.shipping_charge, Decimal("50.00"))
        self.assertEqual(context.tax_total, Decimal("0.00"))
        self.assertEqual(context.total, Decimal("4150.00"))

        self.assertEqual(len(context.lines), 2)
        line = context.lines[0]
        self.assertEqual(line.product_name, "Ambre Noir")
        self.assertEqual(line.brand_name, f"Brand SKU-INV-001")
        self.assertEqual(line.variant_size, "100ml")
        self.assertEqual(line.sku, "SKU-INV-001")
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.unit_price, Decimal("1500.00"))
        self.assertEqual(line.line_total, Decimal("3000.00"))

    def test_live_catalogue_changes_do_not_affect_invoice(self):
        self.variant.price = Decimal("9999.00")
        self.variant.size = "200ml"
        self.variant.sku = "LIVE-SKU-CHANGED"
        self.variant.save()
        self.product.name = "Live Product Name"
        self.product.save()
        self.brand.name = "Live Brand Name"
        self.brand.save()

        context = build_invoice_context(self.order)
        line = context.lines[0]

        self.assertEqual(line.product_name, "Ambre Noir")
        self.assertEqual(line.brand_name, "Brand SKU-INV-001")
        self.assertEqual(line.variant_size, "100ml")
        self.assertEqual(line.sku, "SKU-INV-001")
        self.assertEqual(line.unit_price, Decimal("1500.00"))
        self.assertEqual(line.line_total, Decimal("3000.00"))

    def test_cancelled_item_keeps_original_values_and_annotation_data(self):
        cancelled_at = timezone.now() - timedelta(days=2)
        self.item1.cancelled_at = cancelled_at
        self.item1.cancellation_reason = "Changed mind before dispatch"
        self.item1.save(update_fields=["cancelled_at", "cancellation_reason"])

        context = build_invoice_context(self.order)
        line = context.lines[0]

        self.assertTrue(line.is_cancelled)
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.unit_price, Decimal("1500.00"))
        self.assertEqual(line.line_total, Decimal("3000.00"))
        self.assertEqual(line.cancelled_at, cancelled_at)
        self.assertEqual(
            line.cancellation_reason,
            "Changed mind before dispatch",
        )
        self.assertEqual(context.total, Decimal("4150.00"))

    def test_return_decisions_do_not_alter_invoice_totals(self):
        third_variant = make_variant(
            self.product,
            sku="SKU-INV-003",
            price="700.00",
        )
        item3 = add_item(
            self.order,
            third_variant,
            quantity=1,
            unit_price="700.00",
        )
        self.order.subtotal = Decimal("4900.00")
        self.order.total = Decimal("4850.00")
        self.order.save(update_fields=["subtotal", "total"])

        admin = make_user(
            "invoice_admin",
            is_staff=True,
        )
        now = timezone.now()
        return_request = ReturnRequest.objects.create(
            order=self.order,
            requested_by=self.user,
        )

        ReturnItem.objects.create(
            return_request=return_request,
            order_item=self.item1,
            quantity=self.item1.quantity,
            reason="Return requested",
            status=ReturnItem.Status.REQUESTED,
        )
        ReturnItem.objects.create(
            return_request=return_request,
            order_item=self.item2,
            quantity=self.item2.quantity,
            reason="Return approved",
            status=ReturnItem.Status.APPROVED,
            decided_at=now,
            decided_by=admin,
            stock_restored_at=now,
        )
        ReturnItem.objects.create(
            return_request=return_request,
            order_item=item3,
            quantity=item3.quantity,
            reason="Return rejected",
            status=ReturnItem.Status.REJECTED,
            decided_at=now,
            decided_by=admin,
            decision_note="Outside return window",
        )

        context = build_invoice_context(self.order)
        financials = get_order_financials(self.order)

        self.assertEqual(context.total, self.order.total)
        self.assertEqual(context.total, Decimal("4850.00"))
        self.assertEqual(context.subtotal, Decimal("4900.00"))
        self.assertEqual(
            sum(line.line_total for line in context.lines),
            Decimal("4900.00"),
        )
        self.assertLess(financials.adjusted_total, context.total)
        self.assertEqual(context.lines[1].line_total, Decimal("1200.00"))

    def test_missing_email_and_optional_address_fields(self):
        self.user.email = ""
        self.user.save(update_fields=["email"])
        self.order.delivery_address_line2 = ""
        self.order.delivery_landmark = ""
        self.order.save(
            update_fields=["delivery_address_line2", "delivery_landmark"]
        )

        context = build_invoice_context(self.order)

        self.assertEqual(context.customer_email, "")
        self.assertEqual(
            context.shipping_address_lines,
            ("42 Amber Boulevard", "Bengaluru, Karnataka - 560001"),
        )

    def test_deleted_customer_does_not_break_invoice_context(self):
        self.order.user = None
        self.order.save(update_fields=["user"])
        self.order.refresh_from_db()

        context = build_invoice_context(self.order)
        self.assertEqual(context.customer_email, "")

    def test_missing_payment_details_are_supported(self):
        self.order.payment_collected_at = None
        self.order.payment_collected_amount = None
        self.order.payment_status = Order.PaymentStatus.UNPAID
        self.order.save(
            update_fields=[
                "payment_collected_at",
                "payment_collected_amount",
                "payment_status",
            ]
        )

        context = build_invoice_context(self.order)

        self.assertIsNone(context.payment_collected_at)
        self.assertIsNone(context.payment_collected_amount)
        self.assertFalse(context.payment_differs_from_total)
        self.assertEqual(context.payment_status, "Unpaid")

    def test_payment_difference_flag(self):
        self.order.payment_collected_amount = Decimal("1000.00")
        self.order.save(update_fields=["payment_collected_amount"])

        context = build_invoice_context(self.order)
        self.assertTrue(context.payment_differs_from_total)

        self.order.payment_collected_amount = self.order.total
        self.order.save(update_fields=["payment_collected_amount"])
        context = build_invoice_context(self.order)
        self.assertFalse(context.payment_differs_from_total)

    def test_empty_item_collection(self):
        self.order.items.all().delete()

        context = build_invoice_context(self.order)

        self.assertEqual(context.lines, ())
        pdf_bytes = render_invoice_pdf(context)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))


class InvoicePdfRenderingTests(TestCase):
    def _context(self, *, lines=None, total="3000.00", collected=None):
        now = timezone.now()
        return InvoiceContext(
            invoice_number="INV-AURA-20261104-PDF00001",
            order_number="AURA-20261104-PDF00001",
            order_date=now,
            customer_name="Jane Doe",
            customer_email="jane@example.com",
            shipping_address_lines=(
                "42 Amber Boulevard",
                "Apt 4B",
                "Bengaluru, Karnataka - 560001",
            ),
            shipping_phone="9876543210",
            lines=tuple(lines or ()),
            subtotal=Decimal(total),
            discount_total=Decimal("0.00"),
            shipping_charge=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal(total),
            payment_method="Cash on Delivery",
            payment_status="Paid",
            payment_collected_at=now,
            payment_collected_amount=(
                Decimal(collected) if collected is not None else None
            ),
            payment_differs_from_total=(
                collected is not None and Decimal(collected) != Decimal(total)
            ),
        )

    def _line(self, index, **overrides):
        values = {
            "index": index,
            "product_name": f"Invoice Product {index}",
            "brand_name": "AURA",
            "variant_size": "100ml",
            "sku": f"SKU-PDF-{index}",
            "quantity": 1,
            "unit_price": Decimal("1000.00"),
            "line_total": Decimal("1000.00"),
            "is_cancelled": False,
            "cancelled_at": None,
            "cancellation_reason": "",
        }
        values.update(overrides)
        return InvoiceLine(**values)

    def test_pdf_has_non_empty_bytes_and_signature(self):
        context = self._context(lines=[self._line(1)])

        pdf_bytes = render_invoice_pdf(context)

        self.assertGreater(len(pdf_bytes), 0)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))

    def test_multi_item_and_multi_page_rendering(self):
        lines = [
            self._line(
                index,
                product_name=(
                    f"Extraordinarily Long AURA Fragrance Product Name "
                    f"Number {index} With Layered Descriptors"
                ),
            )
            for index in range(1, 61)
        ]
        context = self._context(lines=lines, total="60000.00")

        pdf_bytes = render_invoice_pdf(context)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertGreaterEqual(
            pdf_bytes.count(b"/Type /Page") - pdf_bytes.count(b"/Type /Pages"),
            2,
        )

    def test_long_product_names_and_addresses_wrap(self):
        context = self._context(
            lines=[
                self._line(
                    1,
                    product_name="A Very Long Product Name " * 8,
                    brand_name="A Very Long Brand Name " * 4,
                    variant_size="A Very Long Variant Description " * 3,
                    sku="A-VERY-LONG-SKU-" * 4,
                )
            ]
        )

        pdf_bytes = render_invoice_pdf(context)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))

    def test_cancelled_annotation_and_original_total_render(self):
        cancelled_at = timezone.now() - timedelta(days=3)
        context = self._context(
            lines=[
                self._line(
                    1,
                    is_cancelled=True,
                    cancelled_at=cancelled_at,
                    cancellation_reason="Changed mind before dispatch",
                )
            ]
        )

        with patch.object(rl_config, "pageCompression", 0):
            pdf_bytes = render_invoice_pdf(context)

        self.assertIn(b"CANCELLED BEFORE DELIVERY", pdf_bytes)
        self.assertIn(b"Changed mind before dispatch", pdf_bytes)
        self.assertIn(b"ORIGINAL ORDER TOTAL", pdf_bytes)
        self.assertIn(b"INR 3,000.00", pdf_bytes)

    def test_payment_collection_information_renders(self):
        context = self._context(
            lines=[self._line(1)],
            total="3000.00",
            collected="1000.00",
        )

        with patch.object(rl_config, "pageCompression", 0):
            pdf_bytes = render_invoice_pdf(context)

        self.assertIn(b"Recorded post-adjustment collection", pdf_bytes)
        self.assertIn(b"INR 1,000.00", pdf_bytes)
        self.assertIn(b"INR 3,000.00", pdf_bytes)

    def test_rendering_does_not_write_to_database(self):
        user = make_user("render_customer")
        category, brand, product, variant = make_catalogue(
            sku="SKU-RENDER-001"
        )
        order = make_order(
            user,
            "AURA-20261104-RND00001",
            status=Order.OrderStatus.DELIVERED,
            payment_collected_at=timezone.now(),
            payment_collected_amount=Decimal("3000.00"),
        )
        item = add_item(order, variant, quantity=2, unit_price="1500.00")

        before_order = (
            order.status,
            order.total,
            order.payment_status,
            order.payment_collected_amount,
            order.updated_at,
        )
        before_item = (
            item.quantity,
            item.unit_price,
            item.line_total,
            item.cancelled_at,
            item.cancellation_reason,
        )

        context = build_invoice_context(order)
        render_invoice_pdf(context)

        order.refresh_from_db()
        item.refresh_from_db()

        self.assertEqual(
            (
                order.status,
                order.total,
                order.payment_status,
                order.payment_collected_amount,
                order.updated_at,
            ),
            before_order,
        )
        self.assertEqual(
            (
                item.quantity,
                item.unit_price,
                item.line_total,
                item.cancelled_at,
                item.cancellation_reason,
            ),
            before_item,
        )


class InvoiceEndpointTests(TestCase):
    def setUp(self):
        self.user = make_user("endpoint_customer")
        self.other_user = make_user("endpoint_other")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            sku="SKU-ENDPOINT-001"
        )
        self.order = make_order(
            self.user,
            "AURA-20261104-END00001",
            status=Order.OrderStatus.DELIVERED,
            payment_collected_at=timezone.now(),
            payment_collected_amount=Decimal("3000.00"),
        )
        self.item = add_item(
            self.order,
            self.variant,
            quantity=2,
            unit_price="1500.00",
        )
        self.url = reverse(
            "orders:download_invoice",
            args=[self.order.order_number],
        )

    def test_authentication_is_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("user_auth:login"), response.url)

    def test_cross_customer_and_nonexistent_orders_return_404(self):
        self.client.force_login(self.other_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

        missing_url = reverse(
            "orders:download_invoice",
            args=["AURA-20261104-MISSING01"],
        )
        response = self.client.get(missing_url)
        self.assertEqual(response.status_code, 404)

    def test_non_delivered_statuses_return_403(self):
        self.client.force_login(self.user)

        for status in (
            Order.OrderStatus.PENDING,
            Order.OrderStatus.SHIPPED,
            Order.OrderStatus.OUT_FOR_DELIVERY,
            Order.OrderStatus.CANCELLED,
        ):
            self.order.status = status
            self.order.save(update_fields=["status"])
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 403, status)

    def test_delivered_order_returns_pdf_response(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)
        expected_filename = build_invoice_filename(self.order)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="{expected_filename}"',
        )
        self.assertEqual(
            int(response["Content-Length"]),
            len(response.content),
        )
        self.assertEqual(
            response["X-Content-Type-Options"],
            "nosniff",
        )
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_get_only_enforcement(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 405)

    def test_repeated_downloads_do_not_mutate_order_or_payment(self):
        self.client.force_login(self.user)
        order_before = (
            self.order.status,
            self.order.payment_status,
            self.order.payment_collected_at,
            self.order.payment_collected_amount,
            self.order.updated_at,
        )
        item_before = (
            self.item.quantity,
            self.item.unit_price,
            self.item.line_total,
            self.item.cancelled_at,
        )

        first = self.client.get(self.url)
        second = self.client.get(self.url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        self.order.refresh_from_db()
        self.item.refresh_from_db()

        self.assertEqual(
            (
                self.order.status,
                self.order.payment_status,
                self.order.payment_collected_at,
                self.order.payment_collected_amount,
                self.order.updated_at,
            ),
            order_before,
        )
        self.assertEqual(
            (
                self.item.quantity,
                self.item.unit_price,
                self.item.line_total,
                self.item.cancelled_at,
            ),
            item_before,
        )


class InvoiceUiTests(TestCase):
    def setUp(self):
        self.user = make_user("ui_customer")
        self.category, self.brand, self.product, self.variant = make_catalogue(
            sku="SKU-UI-001"
        )
        self.order = make_order(
            self.user,
            "AURA-20261104-UI000001",
            status=Order.OrderStatus.DELIVERED,
        )
        self.item = add_item(self.order, self.variant)
        self.detail_url = reverse(
            "orders:order_detail",
            args=[self.order.order_number],
        )
        self.invoice_url = reverse(
            "orders:download_invoice",
            args=[self.order.order_number],
        )

    def test_delivered_order_displays_real_download_link(self):
        self.client.force_login(self.user)

        response = self.client.get(self.detail_url)

        self.assertContains(response, "DOWNLOAD INVOICE")
        self.assertContains(response, f'href="{self.invoice_url}"')
        self.assertNotContains(response, 'aria-disabled="true"')
        self.assertNotContains(
            response,
            "Invoice download will be available in Phase 6.6",
        )

    def test_non_delivered_orders_do_not_display_download_action(self):
        self.client.force_login(self.user)

        for status in (
            Order.OrderStatus.PENDING,
            Order.OrderStatus.SHIPPED,
            Order.OrderStatus.OUT_FOR_DELIVERY,
            Order.OrderStatus.CANCELLED,
        ):
            self.order.status = status
            self.order.save(update_fields=["status"])
            response = self.client.get(self.detail_url)
            self.assertNotContains(
                response,
                "DOWNLOAD INVOICE",
                msg_prefix=str(status),
            )
            self.assertNotContains(
                response,
                self.invoice_url,
                msg_prefix=str(status),
            )
