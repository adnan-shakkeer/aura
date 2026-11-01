import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from admin_panel.products.models import ProductVariant


class Order(models.Model):
    """A customer order.

    Order status tracks fulfilment progress independently from payment
    status. The first release supports Cash on Delivery (COD) orders only:
    orders start as pending and unpaid, with zero discount, shipping and
    tax amounts. Coupons and fulfilment/cancellation transitions are not
    implemented in this phase.

    The delivery address is stored as a snapshot so later edits or deletion
    of the customer's saved Address never change the historical order.
    """

    class OrderStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        SHIPPED = "SHIPPED", "Shipped"
        DELIVERED = "DELIVERED", "Delivered"
        CANCELLED = "CANCELLED", "Cancelled"

    class PaymentMethod(models.TextChoices):
        COD = "COD", "Cash on Delivery"

    class PaymentStatus(models.TextChoices):
        UNPAID = "UNPAID", "Unpaid"
        PAID = "PAID", "Paid"

    class DeliveryAddressType(models.TextChoices):
        HOME = "HOME", "Home"
        WORK = "WORK", "Work"
        OTHER = "OTHER", "Other"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    order_number = models.CharField(
        max_length=32,
        unique=True,
        editable=False,
    )

    idempotency_key = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        editable=False,
    )

    status = models.CharField(
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
    )

    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.COD,
    )

    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
    )

    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    discount_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0.00,
    )

    shipping_charge = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0.00,
    )

    tax_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0.00,
    )

    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    # Delivery-address snapshot (no live Address foreign key).
    delivery_full_name = models.CharField(max_length=100)
    delivery_phone_number = models.CharField(max_length=15)
    delivery_address_line1 = models.CharField(max_length=255)
    delivery_address_line2 = models.CharField(max_length=255, blank=True)
    delivery_landmark = models.CharField(max_length=255, blank=True)
    delivery_city = models.CharField(max_length=100)
    delivery_state = models.CharField(max_length=100)
    delivery_pincode = models.CharField(max_length=10)
    delivery_address_type = models.CharField(
        max_length=10,
        choices=DeliveryAddressType.choices,
        default=DeliveryAddressType.HOME,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Order"
        verbose_name_plural = "Orders"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(subtotal__gte=0),
                name="order_subtotal_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_total__gte=0),
                name="order_discount_total_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(shipping_charge__gte=0),
                name="order_shipping_charge_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(tax_total__gte=0),
                name="order_tax_total_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=0),
                name="order_total_gte_0",
            ),
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="unique_order_idempotency_key",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = self._generate_order_number()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_order_number():
        """Return a candidate order number; uniqueness is enforced by the database."""
        stamp = timezone.now().strftime("%Y%m%d")
        return f"AURA-{stamp}-{uuid.uuid4().hex[:8].upper()}"

    def __str__(self):
        return self.order_number


class OrderItem(models.Model):
    """A single line item belonging to an order.

    Catalogue snapshots are captured when the order is placed so later
    product, brand, size or price changes never alter historical orders.
    The variant reference is protected to keep the link intact; catalogue
    rows in this project are soft-deleted rather than hard-deleted.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        related_name="order_items",
    )

    product_name = models.CharField(max_length=200)
    brand_name = models.CharField(max_length=100)
    variant_size = models.CharField(max_length=50)
    sku = models.CharField(max_length=100)

    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    quantity = models.PositiveIntegerField()

    line_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "variant"],
                name="unique_variant_per_order",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="order_item_quantity_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0),
                name="order_item_unit_price_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(line_total__gte=0),
                name="order_item_line_total_gte_0",
            ),
        ]

    def __str__(self):
        return f"{self.product_name} - {self.variant_size} x {self.quantity}"
