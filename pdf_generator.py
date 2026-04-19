"""
PDF quote generator — Just Print branded.

Generates a clean, minimalist one-page PDF quote
matching Just Print's visual identity (navy + rainbow accents).
"""

import io
import os
import urllib.request
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Spacer, Paragraph, Image,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER


# Just Print brand colours
NAVY = HexColor("#040f2a")
DARK_BG = HexColor("#0d1b3e")
ACCENT_PINK = HexColor("#ff1493")
ACCENT_YELLOW = HexColor("#ffd700")
ACCENT_CYAN = HexColor("#00bfff")
LIGHT_GREY = HexColor("#f5f5f5")
MID_GREY = HexColor("#999999")
BORDER_GREY = HexColor("#dddddd")

LOGO_URL = "https://just-print.ie/wp-content/themes/just-print/assets/img/tiger_760.png"
_logo_cache: bytes | None = None


def _get_logo() -> bytes | None:
    """Download and cache the tiger logo."""
    global _logo_cache
    if _logo_cache is not None:
        return _logo_cache
    try:
        req = urllib.request.Request(LOGO_URL, headers={"User-Agent": "Craig/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            _logo_cache = resp.read()
        return _logo_cache
    except Exception:
        return None


def generate_quote_pdf(quote) -> bytes:
    """
    Generate a branded PDF quote from a Quote DB record.

    Args:
        quote: a db.models.Quote instance (or any object with the same attrs)

    Returns:
        PDF bytes ready to send as a response.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=25 * mm,
        rightMargin=25 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "QTitle", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=20, textColor=NAVY,
        spaceAfter=4 * mm,
        leading=24,
    )
    subtitle_style = ParagraphStyle(
        "QSubtitle", parent=styles["Normal"],
        fontName="Helvetica", fontSize=9, textColor=MID_GREY,
        spaceAfter=8 * mm,
        spaceBefore=1 * mm,
    )
    section_style = ParagraphStyle(
        "QSection", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=11, textColor=NAVY,
        spaceBefore=5 * mm, spaceAfter=2 * mm,
    )
    body_style = ParagraphStyle(
        "QBody", parent=styles["Normal"],
        fontName="Helvetica", fontSize=10, textColor=black,
        leading=14,
    )
    small_style = ParagraphStyle(
        "QSmall", parent=styles["Normal"],
        fontName="Helvetica", fontSize=8, textColor=MID_GREY,
        leading=11,
    )
    total_style = ParagraphStyle(
        "QTotal", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=16, textColor=NAVY,
        alignment=TA_RIGHT,
    )
    right_style = ParagraphStyle(
        "QRight", parent=styles["Normal"],
        fontName="Helvetica", fontSize=10, textColor=black,
        alignment=TA_RIGHT,
    )
    right_small_style = ParagraphStyle(
        "QRightSmall", parent=styles["Normal"],
        fontName="Helvetica", fontSize=9, textColor=MID_GREY,
        alignment=TA_RIGHT,
    )

    elements = []

    # ── Header: logo + company info ──
    logo_img = None
    logo_data = _get_logo()
    if logo_data:
        logo_buf = io.BytesIO(logo_data)
        try:
            logo_img = Image(logo_buf, width=14 * mm, height=14 * mm)
        except Exception:
            logo_img = None

    header_left = logo_img or Paragraph("Just Print", title_style)
    header_right = Paragraph(
        "Just Print<br/>"
        '<font size="8" color="#999999">just-print.ie</font>',
        ParagraphStyle("HRight", parent=styles["Normal"],
                       fontName="Helvetica-Bold", fontSize=12,
                       textColor=NAVY, alignment=TA_RIGHT),
    )

    header_table = Table(
        [[header_left, header_right]],
        colWidths=[doc.width * 0.5, doc.width * 0.5],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 4 * mm))

    # ── Rainbow accent bar ──
    rainbow_data = [["", "", "", "", ""]]
    rainbow_colors = [
        HexColor("#ff1493"), HexColor("#ffd700"), HexColor("#00bfff"),
        HexColor("#00ff7f"), HexColor("#ff6347"),
    ]
    rainbow = Table(rainbow_data, colWidths=[doc.width / 5] * 5, rowHeights=[2 * mm])
    rainbow.setStyle(TableStyle([
        ("BACKGROUND", (i, 0), (i, 0), rainbow_colors[i]) for i in range(5)
    ]))
    elements.append(rainbow)
    elements.append(Spacer(1, 8 * mm))

    # ── Title ──
    elements.append(Paragraph("QUOTATION", title_style))

    # Date + quote ref
    date_str = quote.created_at.strftime("%d %B %Y") if quote.created_at else datetime.now().strftime("%d %B %Y")
    elements.append(Paragraph(
        f"Date: {date_str}&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;Ref: JP-{quote.id:04d}",
        subtitle_style,
    ))

    # ── Product details ──
    elements.append(Paragraph("Product Details", section_style))

    product_name = (quote.product_key or "").replace("_", " ").title()
    specs = quote.specs or {}

    detail_rows = [
        ["Product", product_name],
        ["Quantity", str(specs.get("quantity", ""))],
    ]
    if specs.get("finish"):
        detail_rows.append(["Finish", specs["finish"].replace("_", " ").title()])
    if specs.get("double_sided") is not None:
        detail_rows.append(["Sides", "Double-sided" if specs["double_sided"] else "Single-sided"])
    if specs.get("format"):
        detail_rows.append(["Format", specs["format"].upper()])
    if specs.get("binding"):
        detail_rows.append(["Binding", specs["binding"].replace("_", " ").title()])
    if specs.get("pages"):
        detail_rows.append(["Pages", f"{specs['pages']}pp"])
    if specs.get("cover_type"):
        detail_rows.append(["Cover", specs["cover_type"].replace("_", " ").title()])

    detail_table = Table(
        [[Paragraph(r[0], ParagraphStyle("DL", parent=body_style, textColor=MID_GREY)),
          Paragraph(r[1], body_style)] for r in detail_rows],
        colWidths=[40 * mm, doc.width - 40 * mm],
    )
    detail_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER_GREY),
    ]))
    elements.append(detail_table)
    elements.append(Spacer(1, 8 * mm))

    # ── Pricing ──
    elements.append(Paragraph("Pricing", section_style))

    price_rows = []
    price_rows.append([
        Paragraph("Base price", body_style),
        Paragraph(f"\u20ac{quote.base_price:.2f}", right_style),
    ])

    surcharges = quote.surcharges or []
    if surcharges:
        for s in surcharges:
            price_rows.append([
                Paragraph(str(s), ParagraphStyle("SL", parent=body_style, textColor=MID_GREY)),
                Paragraph("", right_style),
            ])

    if quote.surcharges and quote.final_price_ex_vat != quote.base_price:
        price_rows.append([
            Paragraph("Subtotal", body_style),
            Paragraph(f"\u20ac{quote.final_price_ex_vat:.2f}", right_style),
        ])

    price_rows.append([
        Paragraph("VAT (23%)", ParagraphStyle("VL", parent=body_style, textColor=MID_GREY)),
        Paragraph(f"\u20ac{quote.vat_amount:.2f}", right_small_style),
    ])

    if quote.artwork_cost and quote.artwork_cost > 0:
        artwork_vat = quote.artwork_cost * 0.23
        price_rows.append([
            Paragraph("Artwork / design", body_style),
            Paragraph(f"\u20ac{quote.artwork_cost + artwork_vat:.2f}", right_style),
        ])

    price_table = Table(
        price_rows,
        colWidths=[doc.width - 40 * mm, 40 * mm],
    )
    price_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER_GREY),
    ]))
    elements.append(price_table)
    elements.append(Spacer(1, 3 * mm))

    # ── Total ──
    total_val = quote.total if quote.total else quote.final_price_inc_vat
    total_bar = Table(
        [[Paragraph("TOTAL", ParagraphStyle("TL", parent=body_style, fontName="Helvetica-Bold")),
          Paragraph(f"\u20ac{total_val:.2f}", total_style)]],
        colWidths=[doc.width - 45 * mm, 45 * mm],
    )
    total_bar.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
        ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ("LEFTPADDING", (0, 0), (0, 0), 3 * mm),
        ("RIGHTPADDING", (1, 0), (1, 0), 3 * mm),
    ]))
    elements.append(total_bar)
    elements.append(Spacer(1, 8 * mm))

    # ── Footer notes ──
    elements.append(Paragraph("Turnaround: 3\u20135 working days", small_style))
    elements.append(Spacer(1, 2 * mm))
    elements.append(Paragraph(
        "This quote is subject to final confirmation by Justin Byrne at Just Print. "
        "Prices include Irish VAT at 23%. Artwork and design charged separately if required.",
        small_style,
    ))
    elements.append(Spacer(1, 4 * mm))

    # ── Bottom rainbow ──
    elements.append(rainbow)

    doc.build(elements)
    return buf.getvalue()
