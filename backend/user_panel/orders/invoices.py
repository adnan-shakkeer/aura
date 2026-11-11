"""Original-purchase PDF invoices for delivered customer orders."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from django.utils import timezone
from django.utils.text import get_valid_filename
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .models import Order


INVOICE_ELIGIBLE_STATUS = Order.OrderStatus.DELIVERED

STORE_NAME = "AURA"
STORE_WORDMARK = "A U R A"
STORE_SUBTITLE = "HAUTE PARFUMERIE & BESPOKE FRAGRANCES"
STORE_FOOTER_NAME = "AURA PERFUMERY"
STORE_FOOTER_LOCATION = "Designed in Paris · Crafted in Grasse"

COLOR_IVORY = colors.HexColor("#F8F5F0")
COLOR_CREAM = colors.HexColor("#FDFBF7")
COLOR_BLACK = colors.HexColor("#1A1A1A")
COLOR_MUTED = colors.HexColor("#6B6A65")
COLOR_CHAMPAGNE = colors.HexColor("#D4C5B0")
COLOR_CHAMPAGNE_DARK = colors.HexColor("#BFA98F")
COLOR_BORDER = colors.HexColor("#E5E0D8")
COLOR_RED = colors.HexColor("#B3261E")
COLOR_GREEN = colors.HexColor("#247A4A")
COLOR_WHITE = colors.HexColor("#FFFFFF")

PAGE_WIDTH, PAGE_HEIGHT = A4
PAGE_MARGIN_X = 42
PAGE_MARGIN_TOP = 102
PAGE_MARGIN_BOTTOM = 52
CONTENT_WIDTH = PAGE_WIDTH - (PAGE_MARGIN_X * 2)

INVOICE_FOOTER_NOTE = (
    "This invoice records the original order as placed. Post-purchase "
    "cancellations and returns are documented separately and do not alter "
    "the original amounts shown."
)


@dataclass(frozen=True)
class InvoiceLine:
    index: int
    product_name: str
    brand_name: str
    variant_size: str
    sku: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    is_cancelled: bool
    cancelled_at: datetime | None
    cancellation_reason: str


@dataclass(frozen=True)
class InvoiceContext:
    invoice_number: str
    order_number: str
    order_date: datetime
    customer_name: str
    customer_email: str
    shipping_address_lines: tuple[str, ...]
    shipping_phone: str
    lines: tuple[InvoiceLine, ...]
    subtotal: Decimal
    discount_total: Decimal
    shipping_charge: Decimal
    tax_total: Decimal
    total: Decimal
    payment_method: str
    payment_status: str
    payment_collected_at: datetime | None
    payment_collected_amount: Decimal | None
    payment_differs_from_total: bool
    offer_discount_total: Decimal = Decimal("0.00")
    coupon_discount_total: Decimal = Decimal("0.00")
    coupon_code: str = ""


def is_invoice_available(order):
    return order.status == INVOICE_ELIGIBLE_STATUS


def build_invoice_number(order):
    return f"INV-{order.order_number}"


def build_invoice_filename(order):
    return get_valid_filename(f"invoice-{build_invoice_number(order)}.pdf")


def _money(value):
    if value is None:
        return None
    return Decimal(value).quantize(Decimal("0.01"))


def _customer_email(order):
    user = getattr(order, "user", None)
    if user is None:
        return ""
    return (user.email or "").strip()


def _shipping_address_lines(order):
    lines = []

    if order.delivery_address_line1:
        lines.append(order.delivery_address_line1)
    if order.delivery_address_line2:
        lines.append(order.delivery_address_line2)
    if order.delivery_landmark:
        lines.append(f"Landmark: {order.delivery_landmark}")

    locality = ", ".join(
        part for part in (order.delivery_city, order.delivery_state) if part
    )
    if locality and order.delivery_pincode:
        locality = f"{locality} - {order.delivery_pincode}"
    elif order.delivery_pincode:
        locality = order.delivery_pincode

    if locality:
        lines.append(locality)

    return tuple(lines)


def build_invoice_context(order):
    invoice_number = build_invoice_number(order)
    total = _money(order.total)
    collected = _money(order.payment_collected_amount)

    items = list(order.items.all())
    lines = tuple(
        InvoiceLine(
            index=index,
            product_name=item.product_name,
            brand_name=item.brand_name,
            variant_size=item.variant_size,
            sku=item.sku,
            quantity=item.quantity,
            unit_price=_money(item.unit_price),
            line_total=_money(item.line_total),
            is_cancelled=item.cancelled_at is not None,
            cancelled_at=item.cancelled_at,
            cancellation_reason=(item.cancellation_reason or "").strip(),
        )
        for index, item in enumerate(items, start=1)
    )

    return InvoiceContext(
        invoice_number=invoice_number,
        order_number=order.order_number,
        order_date=order.created_at,
        customer_name=order.delivery_full_name,
        customer_email=_customer_email(order),
        shipping_address_lines=_shipping_address_lines(order),
        shipping_phone=order.delivery_phone_number,
        lines=lines,
        subtotal=_money(order.subtotal),
        discount_total=_money(order.discount_total),
        shipping_charge=_money(order.shipping_charge),
        tax_total=_money(order.tax_total),
        total=total,
        payment_method=order.get_payment_method_display(),
        payment_status=order.get_payment_status_display(),
        payment_collected_at=order.payment_collected_at,
        payment_collected_amount=collected,
        payment_differs_from_total=(
            collected is not None and collected != total
        ),
        offer_discount_total=_money(order.offer_discount_total),
        coupon_discount_total=_money(order.coupon_discount_total),
        coupon_code=order.coupon_code or "",
    )


def _format_money(value):
    return f"INR {value:,.2f}"


def _format_date(value, fallback=""):
    if value is None:
        return fallback
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    value = timezone.localtime(value)
    return value.strftime("%d %b %Y")


def _label_style():
    return ParagraphStyle(
        "InvoiceLabel",
        fontName="Helvetica-Bold",
        fontSize=6.5,
        leading=8,
        textColor=COLOR_MUTED,
    )


def _body_style(font_size=8.5, leading=11):
    return ParagraphStyle(
        "InvoiceBody",
        fontName="Helvetica",
        fontSize=font_size,
        leading=leading,
        textColor=COLOR_BLACK,
    )


def _serif_style(font_size=10, leading=12):
    return ParagraphStyle(
        "InvoiceSerif",
        fontName="Times-Roman",
        fontSize=font_size,
        leading=leading,
        textColor=COLOR_BLACK,
    )


def _right_style(font_size=8.5, leading=11, color=COLOR_BLACK):
    return ParagraphStyle(
        "InvoiceRight",
        fontName="Helvetica",
        fontSize=font_size,
        leading=leading,
        textColor=color,
        alignment=TA_RIGHT,
    )


def _center_style(font_size=8.5, leading=11):
    return ParagraphStyle(
        "InvoiceCenter",
        fontName="Helvetica",
        fontSize=font_size,
        leading=leading,
        textColor=COLOR_BLACK,
        alignment=TA_CENTER,
    )


def _product_cell(line):
    parts = [
        f'<font name="Times-Roman" size="10">{escape(line.product_name)}</font>',
        (
            f'<font name="Helvetica" size="7" color="#6B6A65">'
            f"{escape(line.brand_name).upper()}</font>"
        ),
    ]

    if line.variant_size:
        parts.append(
            f'<font name="Helvetica" size="7.5">{escape(line.variant_size)}</font>'
        )

    if line.is_cancelled:
        parts.append(
            '<font name="Helvetica-Bold" size="6.5" color="#B3261E">'
            f"CANCELLED BEFORE DELIVERY — {_format_date(line.cancelled_at)}"
            "</font>"
        )
        if line.cancellation_reason:
            parts.append(
                '<font name="Helvetica" size="6.5" color="#B3261E">'
                f"Reason: {escape(line.cancellation_reason)}</font>"
            )

    return Paragraph("<br/>".join(parts), _serif_style(10, 12))


def _header_footer(canvas, doc, context, first_page):
    canvas.saveState()
    canvas.setFillColor(COLOR_IVORY)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

    canvas.setFillColor(COLOR_BLACK)
    canvas.setFont("Times-Roman", 22 if first_page else 14)
    canvas.drawString(PAGE_MARGIN_X, PAGE_HEIGHT - 48, STORE_WORDMARK)

    canvas.setFillColor(COLOR_MUTED)
    canvas.setFont("Helvetica", 7 if first_page else 6)
    canvas.drawString(
        PAGE_MARGIN_X,
        PAGE_HEIGHT - (64 if first_page else 58),
        STORE_SUBTITLE,
    )

    canvas.setFillColor(COLOR_BLACK)
    canvas.setFont("Helvetica-Bold", 9 if first_page else 7)
    canvas.drawRightString(
        PAGE_WIDTH - PAGE_MARGIN_X,
        PAGE_HEIGHT - 45,
        "INVOICE",
    )

    canvas.setFillColor(COLOR_MUTED)
    canvas.setFont("Helvetica", 8 if first_page else 7)
    canvas.drawRightString(
        PAGE_WIDTH - PAGE_MARGIN_X,
        PAGE_HEIGHT - 60,
        context.invoice_number,
    )

    canvas.setStrokeColor(COLOR_CHAMPAGNE)
    canvas.setLineWidth(0.8)
    canvas.line(
        PAGE_MARGIN_X,
        PAGE_HEIGHT - 78,
        PAGE_WIDTH - PAGE_MARGIN_X,
        PAGE_HEIGHT - 78,
    )

    canvas.setStrokeColor(COLOR_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(
        PAGE_MARGIN_X,
        36,
        PAGE_WIDTH - PAGE_MARGIN_X,
        36,
    )

    canvas.setFillColor(COLOR_MUTED)
    canvas.setFont("Helvetica", 6.5)
    footer = f"{STORE_FOOTER_NAME}  ·  {STORE_FOOTER_LOCATION}"
    canvas.drawString(PAGE_MARGIN_X, 23, footer)
    canvas.drawRightString(
        PAGE_WIDTH - PAGE_MARGIN_X,
        23,
        f"Page {doc.page}",
    )

    canvas.restoreState()


def _meta_block(context):
    label = _label_style()
    body = _body_style(9, 11)

    data = [
        [
            Paragraph("ORDER NUMBER", label),
            Paragraph("ORDER DATE", label),
        ],
        [
            Paragraph(escape(context.order_number), body),
            Paragraph(_format_date(context.order_date), body),
        ],
    ]
    table = Table(data, colWidths=[CONTENT_WIDTH * 0.58, CONTENT_WIDTH * 0.42])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, 0), 0),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 0),
            ]
        )
    )
    return table


def _customer_block(context):
    label = _label_style()
    body = _body_style(8.5, 12)

    customer_parts = [f"<b>{escape(context.customer_name)}</b>"]
    if context.customer_email:
        customer_parts.append(escape(context.customer_email))
    customer = Paragraph("<br/>".join(customer_parts), body)

    shipping_parts = [f"<b>{escape(context.customer_name)}</b>"]
    shipping_parts.extend(
        escape(line) for line in context.shipping_address_lines
    )
    if context.shipping_phone:
        shipping_parts.append(f"Phone: {escape(context.shipping_phone)}")
    shipping = Paragraph("<br/>".join(shipping_parts), body)

    data = [
        [
            Paragraph("CUSTOMER", label),
            Paragraph("SHIP TO", label),
        ],
        [customer, shipping],
    ]
    table = Table(data, colWidths=[CONTENT_WIDTH * 0.44, CONTENT_WIDTH * 0.56])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, -1), 14),
                ("RIGHTPADDING", (1, 0), (1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, 0), 0),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 0),
            ]
        )
    )
    return table


def _items_table(context):
    label = _label_style()
    body = _body_style(8, 10)
    right = _right_style(8, 10)
    center = _center_style(8, 10)

    data = [
        [
            Paragraph("#", label),
            Paragraph("PRODUCT / VARIANT", label),
            Paragraph("SKU", label),
            Paragraph("QTY", label),
            Paragraph("UNIT PRICE", label),
            Paragraph("LINE TOTAL", label),
        ]
    ]

    for line in context.lines:
        data.append(
            [
                Paragraph(str(line.index), center),
                _product_cell(line),
                Paragraph(escape(line.sku), body),
                Paragraph(str(line.quantity), center),
                Paragraph(_format_money(line.unit_price), right),
                Paragraph(_format_money(line.line_total), right),
            ]
        )

    if not context.lines:
        data.append(
            [
                Paragraph("", body),
                Paragraph("No line items recorded.", body),
                Paragraph("", body),
                Paragraph("", body),
                Paragraph("", body),
                Paragraph("", body),
            ]
        )

    widths = [
        20,
        224,
        72,
        34,
        80,
        CONTENT_WIDTH - (20 + 224 + 72 + 34 + 80),
    ]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_CREAM),
                ("TEXTCOLOR", (0, 0), (-1, 0), COLOR_MUTED),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [COLOR_WHITE, COLOR_IVORY]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (3, 0), (3, -1), "CENTER"),
                ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, COLOR_BLACK),
                ("LINEBELOW", (0, 1), (-1, -2), 0.35, COLOR_BORDER),
                ("LINEBELOW", (0, -1), (-1, -1), 0.8, COLOR_BLACK),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def _payment_block(context):
    label = _label_style()
    body = _body_style(8, 11)
    right = _right_style(8, 11)

    rows = [
        [
            Paragraph("Payment method", body),
            Paragraph(escape(context.payment_method), right),
        ],
        [
            Paragraph("Payment status", body),
            Paragraph(escape(context.payment_status), right),
        ],
    ]

    if context.payment_collected_at:
        rows.append(
            [
                Paragraph("Collected on", body),
                Paragraph(_format_date(context.payment_collected_at), right),
            ]
        )

    if context.payment_collected_amount is not None:
        amount_label = (
            "Recorded post-adjustment collection"
            if context.payment_differs_from_total
            else "Amount collected"
        )
        rows.append(
            [
                Paragraph(amount_label, body),
                Paragraph(
                    _format_money(context.payment_collected_amount),
                    right,
                ),
            ]
        )

    table = Table(rows, colWidths=[135, 105])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    parts = [Paragraph("PAYMENT INFORMATION", label), Spacer(1, 6), table]

    if context.payment_differs_from_total:
        parts.append(Spacer(1, 7))
        parts.append(
            Paragraph(
                "The collected amount reflects a recorded post-adjustment "
                "collection. The original order total above is unchanged.",
                ParagraphStyle(
                    "InvoicePaymentNote",
                    fontName="Helvetica",
                    fontSize=6.5,
                    leading=9,
                    textColor=COLOR_MUTED,
                ),
            )
        )

    return parts


def _totals_block(context):
    label = _body_style(8.5, 11)
    value = _right_style(8.5, 11)
    free = _right_style(8.5, 11, COLOR_GREEN)

    rows = [
        [
            Paragraph("Subtotal", label),
            Paragraph(_format_money(context.subtotal), value),
        ]
    ]

    offer_discount = context.offer_discount_total or Decimal("0.00")
    coupon_discount = context.coupon_discount_total or Decimal("0.00")

    if offer_discount > 0:
        rows.append(
            [
                Paragraph("Offer discount", label),
                Paragraph(f"-{_format_money(offer_discount)}", value),
            ]
        )

    if coupon_discount > 0:
        coupon_label = (
            f"Coupon discount ({context.coupon_code})"
            if context.coupon_code
            else "Coupon discount"
        )
        rows.append(
            [
                Paragraph(coupon_label, label),
                Paragraph(f"-{_format_money(coupon_discount)}", value),
            ]
        )

    if (
        offer_discount == 0
        and coupon_discount == 0
        and context.discount_total > 0
    ):
        rows.append(
            [
                Paragraph("Discount", label),
                Paragraph(f"-{_format_money(context.discount_total)}", value),
            ]
        )

    if context.shipping_charge > 0:
        rows.append(
            [
                Paragraph("Shipping charges", label),
                Paragraph(_format_money(context.shipping_charge), value),
            ]
        )
    else:
        rows.append(
            [
                Paragraph("Shipping charges", label),
                Paragraph("FREE", free),
            ]
        )

    if context.tax_total > 0:
        rows.append(
            [
                Paragraph("Tax", label),
                Paragraph(_format_money(context.tax_total), value),
            ]
        )

    total_label = ParagraphStyle(
        "InvoiceTotalLabel",
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=COLOR_BLACK,
    )
    total_value = ParagraphStyle(
        "InvoiceTotalValue",
        fontName="Times-Bold",
        fontSize=15,
        leading=18,
        textColor=COLOR_BLACK,
        alignment=TA_RIGHT,
    )

    rows.append(
        [
            Paragraph("ORIGINAL ORDER TOTAL", total_label),
            Paragraph(_format_money(context.total), total_value),
        ]
    )

    table = Table(rows, colWidths=[150, 120])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -2), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -2), 3),
                ("TOPPADDING", (0, -1), (-1, -1), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 0),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, COLOR_BLACK),
            ]
        )
    )

    return [
        Paragraph("FINANCIAL SUMMARY", _label_style()),
        Spacer(1, 6),
        table,
    ]


def render_invoice_pdf(context):
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=PAGE_MARGIN_X,
        rightMargin=PAGE_MARGIN_X,
        topMargin=PAGE_MARGIN_TOP,
        bottomMargin=PAGE_MARGIN_BOTTOM,
        title=f"Invoice {context.invoice_number}",
        author=STORE_NAME,
        subject=f"Original purchase invoice for order {context.order_number}",
    )

    def first_page(canvas, document):
        _header_footer(canvas, document, context, first_page=True)

    def later_page(canvas, document):
        _header_footer(canvas, document, context, first_page=False)

    story = [
        Spacer(1, 6),
        _meta_block(context),
        Spacer(1, 16),
        _customer_block(context),
        Spacer(1, 18),
        _items_table(context),
        Spacer(1, 18),
    ]

    bottom = Table(
        [[_payment_block(context), "", _totals_block(context)]],
        colWidths=[240, 20, CONTENT_WIDTH - 260],
    )
    bottom.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    footer_note = Paragraph(
        INVOICE_FOOTER_NOTE,
        ParagraphStyle(
            "InvoiceFooterNote",
            fontName="Helvetica",
            fontSize=6.5,
            leading=9,
            textColor=COLOR_MUTED,
        ),
    )

    story.append(
        KeepTogether(
            [
                bottom,
                Spacer(1, 16),
                footer_note,
            ]
        )
    )

    doc.build(
        story,
        onFirstPage=first_page,
        onLaterPages=later_page,
    )
    return buffer.getvalue()
