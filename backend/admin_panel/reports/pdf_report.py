"""Sales Report PDF generation using ReportLab Platypus.

Reuses the same library and design conventions as user_panel/orders/invoices.py:
- SimpleDocTemplate + Platypus story
- Same AURA color palette (ivory, black, champagne, muted)
- Same header/footer canvas pattern with page numbers
- Times-Roman wordmark, Helvetica body text
"""

from __future__ import annotations

import io
from datetime import date

from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from user_panel.orders.financials import get_order_financials

# ── Colour palette (matches invoices.py) ──────────────────────────────────────
COLOR_IVORY      = colors.HexColor("#F8F5F0")
COLOR_CREAM      = colors.HexColor("#FDFBF7")
COLOR_BLACK      = colors.HexColor("#1A1A1A")
COLOR_MUTED      = colors.HexColor("#6B6A65")
COLOR_CHAMPAGNE  = colors.HexColor("#D4C5B0")
COLOR_BORDER     = colors.HexColor("#E5E0D8")
COLOR_GOLD       = colors.HexColor("#A8842C")
COLOR_GOLD_LIGHT = colors.HexColor("#F3ECD8")
COLOR_WHITE      = colors.HexColor("#FFFFFF")
COLOR_GREEN      = colors.HexColor("#247A4A")
COLOR_RED        = colors.HexColor("#B3261E")

# ── Page geometry ─────────────────────────────────────────────────────────────
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X      = 42
MARGIN_TOP    = 108
MARGIN_BOTTOM = 52
CONTENT_W     = PAGE_WIDTH - MARGIN_X * 2

# ── Branding ──────────────────────────────────────────────────────────────────
STORE_WORDMARK  = "A U R A"
STORE_SUBTITLE  = "LUXURY FRAGRANCE ADMIN"
STORE_FOOTER    = "AURA PERFUMERY  ·  Luxury Fragrance Admin"


# ── Style helpers (mirror invoices.py naming) ─────────────────────────────────

def _label(size=6.5, color=COLOR_MUTED):
    return ParagraphStyle(
        "RLabel", fontName="Helvetica-Bold", fontSize=size,
        leading=size * 1.3, textColor=color,
        spaceAfter=1,
    )


def _body(size=8.5, bold=False, color=COLOR_BLACK, align=None):
    name = "RBody" + ("Bold" if bold else "")
    style = ParagraphStyle(
        name,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size, leading=size * 1.4, textColor=color,
    )
    if align == "right":
        style.alignment = TA_RIGHT
    elif align == "center":
        style.alignment = TA_CENTER
    return style


def _section_heading(text):
    """Gold-tinted section title paragraph."""
    return Paragraph(
        text,
        ParagraphStyle(
            "RSectionHead",
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            textColor=COLOR_GOLD,
            spaceBefore=0,
            spaceAfter=4,
        ),
    )


# ── Money formatter ────────────────────────────────────────────────────────────

def _inr(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    return f"\u20b9{v:,.2f}"


def _fmt_date(dt):
    if dt is None:
        return ""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return timezone.localtime(dt).strftime("%d %b %Y")


# ── Header / Footer canvas callback ───────────────────────────────────────────

def _page_header_footer(canvas, doc, start_str, end_str):
    canvas.saveState()

    # ivory background
    canvas.setFillColor(COLOR_IVORY)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

    # Wordmark
    canvas.setFillColor(COLOR_BLACK)
    canvas.setFont("Times-Roman", 18)
    canvas.drawString(MARGIN_X, PAGE_HEIGHT - 46, STORE_WORDMARK)

    # Subtitle
    canvas.setFillColor(COLOR_MUTED)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN_X, PAGE_HEIGHT - 60, STORE_SUBTITLE)

    # Right: "SALES REPORT" label
    canvas.setFillColor(COLOR_BLACK)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawRightString(PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 44, "SALES REPORT")

    # Right: date range
    canvas.setFillColor(COLOR_MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(
        PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 56,
        f"{start_str}  \u2013  {end_str}",
    )

    # Champagne rule under header
    canvas.setStrokeColor(COLOR_CHAMPAGNE)
    canvas.setLineWidth(0.8)
    canvas.line(MARGIN_X, PAGE_HEIGHT - 76, PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 76)

    # Footer rule
    canvas.setStrokeColor(COLOR_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN_X, 36, PAGE_WIDTH - MARGIN_X, 36)

    # Footer text
    canvas.setFillColor(COLOR_MUTED)
    canvas.setFont("Helvetica", 6.5)
    canvas.drawString(MARGIN_X, 24, STORE_FOOTER)
    canvas.drawRightString(
        PAGE_WIDTH - MARGIN_X, 24,
        f"Page {doc.page}",
    )

    canvas.restoreState()


# ── Section builders ──────────────────────────────────────────────────────────

def _build_cover_block(start_str, end_str, generated, pm_label, os_label, ps_label):
    """Section A — Report meta information."""
    label = _label(6.5)
    body  = _body(9)

    rows = [
        [Paragraph("DATE RANGE", label),    Paragraph(f"{start_str}  \u2013  {end_str}", body)],
        [Paragraph("GENERATED ON", label),  Paragraph(generated, body)],
        [Paragraph("PAYMENT METHOD", label),Paragraph(pm_label, body)],
        [Paragraph("ORDER STATUS", label),  Paragraph(os_label, body)],
        [Paragraph("PAYMENT STATUS", label),Paragraph(ps_label, body)],
    ]
    table = Table(rows, colWidths=[110, CONTENT_W - 110])
    table.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("LINEBELOW",    (0, -1), (-1, -1), 0.5, COLOR_BORDER),
    ]))
    return [_section_heading("REPORT DETAILS"), table]


def _build_kpi_block(metrics):
    """Section B — Sales Overview KPI summary."""
    label_s = _label(6.5)
    value_s = _body(9, align="right")
    value_bold = _body(10, bold=True, align="right")

    rows_data = [
        ("Gross Sales",                     _inr(metrics["gross_sales"]),                    False, COLOR_GOLD_LIGHT),
        ("Discounts",                       _inr(metrics["discounts"]),                      False, COLOR_WHITE),
        ("Net Sales",                       _inr(metrics["net_sales"]),                      False, COLOR_GOLD_LIGHT),
        ("Collected Revenue",               _inr(metrics["collected_revenue"]),              True,  COLOR_WHITE),
        ("Total Orders",                    str(metrics["total_orders"]),                    False, COLOR_WHITE),
        ("Paid Orders",                     str(metrics["paid_orders"]),                     False, COLOR_GOLD_LIGHT),
        ("Average Collected / Paid Order",  _inr(metrics["average_collected_per_paid_order"]), False, COLOR_WHITE),
    ]

    table_data = [
        [Paragraph("METRIC", label_s), Paragraph("VALUE", label_s)],
    ]
    for metric_name, metric_val, is_bold, row_bg in rows_data:
        vs = value_bold if is_bold else value_s
        table_data.append([
            Paragraph(metric_name, _body(8.5)),
            Paragraph(metric_val, vs),
        ])

    table = Table(table_data, colWidths=[CONTENT_W * 0.65, CONTENT_W * 0.35])
    table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), COLOR_CREAM),
        ("LINEBELOW",    (0, 0), (-1, 0), 0.8, COLOR_BLACK),
        ("LINEBELOW",    (0, 1), (-1, -2), 0.35, COLOR_BORDER),
        ("LINEBELOW",    (0, -1), (-1, -1), 0.8, COLOR_BLACK),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("ALIGN",        (1, 0), (1, -1), "RIGHT"),
    ]))
    # Per-row champagne/white backgrounds matching the UI pattern
    for idx, (_name, _val, _bold, row_bg) in enumerate(rows_data, start=1):
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, idx), (-1, idx), row_bg),
        ]))
    return [_section_heading("SALES OVERVIEW"), table]


def _build_payment_breakdown(payment_breakdown):
    """Section C — Payment Method Breakdown."""
    label_s = _label(6.5)
    body_s  = _body(8)
    right_s = _body(8, align="right")

    table_data = [[
        Paragraph("PAYMENT METHOD", label_s),
        Paragraph("ORDERS", label_s),
        Paragraph("ORDER VALUE", label_s),
        Paragraph("COLLECTED", label_s),
    ]]
    for _code, method in payment_breakdown.items():
        table_data.append([
            Paragraph(method["label"], body_s),
            Paragraph(str(method["order_count"]), right_s),
            Paragraph(_inr(method["order_value"]), right_s),
            Paragraph(_inr(method["collected_revenue"]), right_s),
        ])

    if len(table_data) == 1:
        table_data.append([Paragraph("No payment data.", body_s), "", "", ""])

    col_w = CONTENT_W / 4
    table = Table(table_data, colWidths=[col_w * 1.4, col_w * 0.7, col_w * 0.95, col_w * 0.95])
    table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), COLOR_CREAM),
        ("LINEBELOW",    (0, 0), (-1, 0), 0.8, COLOR_BLACK),
        ("LINEBELOW",    (0, 1), (-1, -2), 0.35, COLOR_BORDER),
        ("LINEBELOW",    (0, -1), (-1, -1), 0.8, COLOR_BLACK),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [COLOR_WHITE, COLOR_IVORY]),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("ALIGN",        (1, 0), (-1, -1), "RIGHT"),
    ]))
    # Subtle champagne treatment for Razorpay (middle row)
    for idx, (_code, method) in enumerate(payment_breakdown.items(), start=1):
        if method["label"] == "Razorpay":
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, idx), (-1, idx), COLOR_GOLD_LIGHT),
            ]))
    return [_section_heading("PAYMENT METHOD BREAKDOWN"), table]


def _build_best_selling(best_selling_products):
    """Section D — Best-Selling Products (top 3)."""
    label_s = _label(6.5)
    body_s  = _body(8)
    right_s = _body(8, align="right")

    table_data = [[
        Paragraph("PRODUCT", label_s),
        Paragraph("BRAND", label_s),
        Paragraph("VARIANT", label_s),
        Paragraph("UNITS", label_s),
        Paragraph("REVENUE", label_s),
    ]]
    for p in best_selling_products:
        table_data.append([
            Paragraph(str(p["product_name"]), body_s),
            Paragraph(str(p["brand_name"]), body_s),
            Paragraph(str(p["variant_size"]), body_s),
            Paragraph(str(p["units_sold"]), right_s),
            Paragraph(_inr(p["product_revenue"]), right_s),
        ])

    if len(table_data) == 1:
        table_data.append([Paragraph("No product data.", body_s), "", "", "", ""])

    cw = CONTENT_W
    table = Table(
        table_data,
        colWidths=[cw * 0.32, cw * 0.18, cw * 0.18, cw * 0.12, cw * 0.20],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), COLOR_CREAM),
        ("LINEBELOW",    (0, 0), (-1, 0), 0.8, COLOR_BLACK),
        ("LINEBELOW",    (0, 1), (-1, -2), 0.35, COLOR_BORDER),
        ("LINEBELOW",    (0, -1), (-1, -1), 0.8, COLOR_BLACK),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [COLOR_WHITE, COLOR_IVORY]),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("ALIGN",        (3, 0), (-1, -1), "RIGHT"),
    ]))
    return [_section_heading("BEST-SELLING PRODUCTS (TOP 3)"), table]


def _build_transactions(orders_qs):
    """Section E — Full transactions table, repeating header across pages."""
    label_s = _label(6.5)
    body_s  = _body(7.5)
    right_s = _body(7.5, align="right")

    table_data = [[
        Paragraph("ORDER ID", label_s),
        Paragraph("CUSTOMER", label_s),
        Paragraph("DATE", label_s),
        Paragraph("METHOD", label_s),
        Paragraph("PAY STATUS", label_s),
        Paragraph("ORDER STATUS", label_s),
        Paragraph("SUBTOTAL", label_s),
        Paragraph("DISCOUNT", label_s),
        Paragraph("FINAL TOTAL", label_s),
    ]]

    for order in orders_qs.select_related("user"):
        financials = get_order_financials(order)
        customer = (
            order.delivery_full_name
            or (order.user.get_full_name() if order.user else "")
            or (order.user.username if order.user else "")
            or "—"
        )
        ps = order.get_payment_status_display()
        ps_color = COLOR_GREEN if order.payment_status == "PAID" else (
            COLOR_RED if order.payment_status in ("FAILED", "EXPIRED") else COLOR_BLACK
        )
        table_data.append([
            Paragraph(str(order.order_number), body_s),
            Paragraph(str(customer)[:30], body_s),
            Paragraph(_fmt_date(order.created_at), body_s),
            Paragraph(order.get_payment_method_display(), body_s),
            Paragraph(ps, _body(7.5, color=ps_color)),
            Paragraph(order.get_status_display(), body_s),
            Paragraph(_inr(order.subtotal), right_s),
            Paragraph(_inr(order.discount_total), right_s),
            Paragraph(_inr(financials.adjusted_total), _body(7.5, bold=True, align="right")),
        ])

    if len(table_data) == 1:
        table_data.append([
            Paragraph("No transactions found for this period.", body_s),
            "", "", "", "", "", "", "", "",
        ])

    cw = CONTENT_W
    col_widths = [
        cw * 0.14,  # Order ID
        cw * 0.14,  # Customer
        cw * 0.09,  # Date
        cw * 0.09,  # Method
        cw * 0.10,  # Pay status
        cw * 0.10,  # Order status
        cw * 0.11,  # Subtotal
        cw * 0.10,  # Discount
        cw * 0.13,  # Final Total
    ]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), COLOR_BLACK),
        ("TEXTCOLOR",    (0, 0), (-1, 0), COLOR_WHITE),
        ("LINEBELOW",    (0, 0), (-1, 0), 0.8, COLOR_CHAMPAGNE),
        ("LINEBELOW",    (0, 1), (-1, -2), 0.35, COLOR_BORDER),
        ("LINEBELOW",    (0, -1), (-1, -1), 0.8, COLOR_BLACK),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [COLOR_WHITE, COLOR_IVORY]),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("ALIGN",        (6, 0), (-1, -1), "RIGHT"),
    ]))
    return [_section_heading("DETAILED SALES TRANSACTIONS"), table]


# ── Main render function ───────────────────────────────────────────────────────

def render_sales_report_pdf(
    orders_qs, start_date, end_date,
    metrics, payment_breakdown, best_selling_products,
    pm_label, os_label, ps_label,
):
    """Return PDF bytes for the Admin Sales Report."""
    start_str = start_date.isoformat() if start_date else "N/A"
    end_str   = end_date.isoformat() if end_date else "N/A"
    generated = date.today().strftime("%d %b %Y")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title="AURA Sales Report",
        author="AURA Luxury Fragrance Admin",
        subject=f"Sales Report {start_str} to {end_str}",
    )

    def _page_cb(canvas, document):
        _page_header_footer(canvas, document, start_str, end_str)

    story = []

    # Section A — Report details
    story += _build_cover_block(start_str, end_str, generated, pm_label, os_label, ps_label)
    story.append(Spacer(1, 14))

    # Section B — KPIs
    story += _build_kpi_block(metrics)
    story.append(Spacer(1, 14))

    # Section C — Payment breakdown
    story += _build_payment_breakdown(payment_breakdown)
    story.append(Spacer(1, 14))

    # Section D — Best sellers
    story += _build_best_selling(best_selling_products)
    story.append(Spacer(1, 14))

    # Section E — Full transactions (may span pages)
    story.append(PageBreak())
    story += _build_transactions(orders_qs)

    doc.build(story, onFirstPage=_page_cb, onLaterPages=_page_cb)
    return buffer.getvalue()
