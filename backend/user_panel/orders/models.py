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
    tax amounts. Coupons are not implemented.

    The delivery address is stored as a snapshot so later edits or deletion
    of the customer's saved Address never change the historical order.
    """

    class OrderStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SHIPPED = "SHIPPED", "Shipped"
        OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY", "Out for delivery"
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

    payment_collected_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    payment_collected_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    payment_collected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders_payment_collected",
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
            models.CheckConstraint(
                condition=(
                    models.Q(payment_collected_amount__isnull=True)
                    | models.Q(payment_collected_amount__gte=0)
                ),
                name="order_payment_collected_amount_gte_0",
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

    cancelled_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    cancellation_reason = models.TextField(
        blank=True,
        default="",
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
            models.CheckConstraint(
                condition=(
                    models.Q(cancelled_at__isnull=False)
                    | models.Q(cancellation_reason="")
                ),
                name="order_item_reason_requires_cancellation",
            ),
        ]

    @property
    def is_cancelled(self):
        return self.cancelled_at is not None

    def __str__(self):
        return f"{self.product_name} - {self.variant_size} x {self.quantity}"


class ReturnRequest(models.Model):
    """A customer's request to return one or more delivered order items.

    The request groups the returned lines and records who submitted it. Each
    product-level reason, decision and stock-restoration state lives on
    ``ReturnItem``. ``ReturnItem.order_item`` is one-to-one so an order item
    can never be submitted for return twice.
    """

    request_number = models.CharField(
        max_length=32,
        unique=True,
        editable=False,
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="return_requests",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="return_requests",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Return request"
        verbose_name_plural = "Return requests"

    def save(self, *args, **kwargs):
        if not self.request_number:
            self.request_number = self._generate_request_number()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_request_number():
        """Return a candidate request number; uniqueness is enforced by the database."""
        stamp = timezone.now().strftime("%Y%m%d")
        return f"RET-{stamp}-{uuid.uuid4().hex[:8].upper()}"

    def __str__(self):
        return self.request_number


class ReturnItem(models.Model):
    """A returned order line with its own product-level decision.

    The one-to-one relationship makes a return request permanent: once an
    order item has a return record, rejected or otherwise, it can never be
    submitted for return again.
    """

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    return_request = models.ForeignKey(
        ReturnRequest,
        on_delete=models.CASCADE,
        related_name="items",
    )

    order_item = models.OneToOneField(
        OrderItem,
        on_delete=models.CASCADE,
        related_name="return_item",
    )

    quantity = models.PositiveIntegerField()

    reason = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.REQUESTED,
    )

    requested_at = models.DateTimeField(auto_now_add=True)

    decided_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="return_item_decisions",
    )

    decision_note = models.TextField(
        blank=True,
        default="",
    )

    stock_restored_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "Return item"
        verbose_name_plural = "Return items"
        indexes = [
            models.Index(
                fields=["status", "requested_at"],
                name="orders_return_item_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="return_item_quantity_gte_1",
            ),
            models.CheckConstraint(
                condition=~models.Q(reason=""),
                name="return_item_reason_required",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="REQUESTED",
                        decided_at__isnull=True,
                        decided_by__isnull=True,
                        decision_note="",
                        stock_restored_at__isnull=True,
                    )
                    | ~models.Q(status="REQUESTED")
                ),
                name="return_item_requested_has_no_decision",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["APPROVED", "REJECTED"])
                    | models.Q(decided_at__isnull=False)
                ),
                name="return_item_decided_has_timestamp",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="REJECTED")
                    | ~models.Q(decision_note="")
                ),
                name="return_item_rejected_note_required",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(status="APPROVED")
                        & models.Q(stock_restored_at__isnull=False)
                    )
                    | (
                        ~models.Q(status="APPROVED")
                        & models.Q(stock_restored_at__isnull=True)
                    )
                ),
                name="return_item_stock_restored_only_approved",
            ),
        ]

    def __str__(self):
        return f"Return: {self.order_item}"


class OrderStatusEvent(models.Model):
    """An append-only record of an order-level status transition."""

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="status_events",
    )

    status = models.CharField(
        max_length=20,
        choices=Order.OrderStatus.choices,
    )

    previous_status = models.CharField(
        max_length=20,
        choices=Order.OrderStatus.choices,
        blank=True,
        default="",
    )

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_status_events",
    )

    note = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Order status event"
        verbose_name_plural = "Order status events"
        indexes = [
            models.Index(
                fields=["order", "created_at"],
                name="orders_status_event_order_idx",
            ),
        ]

    def __str__(self):
        return f"{self.order.order_number}: {self.status}"
