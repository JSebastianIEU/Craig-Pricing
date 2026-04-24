"""
Just Print branded quote PDF generator.

Matches the layout of Justin's canonical quote (Quote 1519487.pdf):

  ┌─────┬──────────────────────────────────────────────┐
  │  Q  │    [triangles]                                │
  │  U  │            Just-Print.ie                      │
  │  O  │      PRINT.DESIGN.SIGNAGE.&MORE...            │
  │  T  │                                               │
  │  A  │    Job Reference: ...        Ref: xxxx        │
  │  T  │    Date: ...                 Tel: ...         │
  │  I  │    To: ...                   Email: ...       │
  │  O  │    Company: ...                               │
  │  N  │    ┌────────┬───┬──────┬───┬──────┐           │
  │     │    │ DESC   │QTY│PRICE │VAT│TOTAL │  (navy +  │
  │     │    ├────────┼───┼──────┼───┼──────┤  pink on  │
  │     │    │ rows...                      │  PRICE)   │
  │     │    └──────────────────────────────┘           │
  │     │    Retention of Title: ...                    │
  │     │    Credit Accounts Strictly 30 Days...        │
  │     │    IBAN ... BIC ...                           │
  │     │    TERMS AND CONDITIONS                        │
  │     │    • bullet 1 ...                             │
  │     │    • ...                                       │
  │ [△] │                                               │
  │     │    T: ... E: ... W: ...                       │
  │     │    Unit 7, Ballymount Cross Business Park...  │
  │     │    COMPANY REG. No. ... VAT No. ...           │
  └─────┴──────────────────────────────────────────────┘

Entry point:

    generate_quote_pdf(quote: db.models.Quote) -> bytes

Signature unchanged so `app.py::quote_pdf` and the Missive handler keep
working without edits.
"""

import io
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Table, TableStyle, Spacer, Paragraph,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER


# ───────────────────────── brand ─────────────────────────
NAVY = HexColor("#040f2a")
PINK = HexColor("#e30686")
YELLOW = HexColor("#feea03")
BLUE = HexColor("#3e8fcd")
LIME = HexColor("#c4cf00")
ORANGE = HexColor("#f37021")
MID_GREY = HexColor("#888888")
BORDER_GREY = HexColor("#cccccc")


# ───────────────── tenant info (Just Print — hardcoded v1) ─────────────────
# When we onboard a second tenant, move these to per-tenant Settings rows and
# look them up by `quote.organization_slug` inside generate_quote_pdf.
COMPANY_NAME = "Just-Print.ie"
COMPANY_ADDRESS = "Unit 7, Ballymount Cross Business Park, Ballymount, Dublin 24 D24 E5NH"
COMPANY_PHONE = "01 494 0222"
COMPANY_EMAIL = "info@just-print.ie"
COMPANY_WEB = "www.just-print.ie"
COMPANY_REG = "450382"
COMPANY_VAT = "IE9673764R"
COMPANY_IBAN = "IE87 BOFI 9045 8768 2957 30"
COMPANY_BIC = "BOFIIE2D"

TERMS_BULLETS = [
    "All prices are subject to VAT where applicable and at appropriate rate.",
    "Quotation valid for 30 days only from above date.",
    "Carriage maybe extra depending on delivery location.",
    "Artwork charged extra at cost unless supplied.",
    "Prices quoted are subject to sight of artwork.",
    "Terms: COD for first time orders or customers that have not dealt with us in six months, all customers 30 days from date of invoice thereafter.",
    "Delivery cannot be confirmed until receipt of signed order confirmation and approval of final artwork.",
]


# ───────────────────────── page drawing ─────────────────────────

def _draw_page_frame(canv, doc):
    """
    onPage callback — draws everything that is the SAME on every page:
      - Orange left sidebar with vertical "QUOTATION"
      - Top-right decorative colored triangles
      - Just-Print.ie logo + colored tagline
      - Bottom-left decorative triangles
      - Retention of Title + payment block
      - Terms and Conditions bullets
      - Contact line + address + registration numbers

    Flowables (header block + pricing table) render inside the frame between
    these fixed layers.
    """
    page_w, page_h = A4

    # ── Orange vertical sidebar ──────────────────────────────────────
    sidebar_w = 11 * mm
    canv.setFillColor(ORANGE)
    canv.rect(0, 0, sidebar_w, page_h, fill=1, stroke=0)
    canv.setFillColor(white)
    canv.setFont("Helvetica-Bold", 20)
    canv.saveState()
    canv.translate(sidebar_w / 2 + 2 * mm, page_h * 0.58)
    canv.rotate(90)
    canv.drawString(0, 0, "QUOTATION")
    canv.restoreState()

    # ── Top-right decorative triangles ───────────────────────────────
    # Three overlapping triangles echoing the widget's rainbow accents.
    def _tri(color, x1, y1, x2, y2, x3, y3):
        canv.setFillColor(color)
        p = canv.beginPath()
        p.moveTo(x1, y1); p.lineTo(x2, y2); p.lineTo(x3, y3); p.close()
        canv.drawPath(p, fill=1, stroke=0)

    tr_right = page_w
    tr_top = page_h
    _tri(PINK,
         tr_right - 40 * mm, tr_top,
         tr_right,            tr_top - 40 * mm,
         tr_right,            tr_top)
    _tri(YELLOW,
         tr_right - 30 * mm, tr_top,
         tr_right - 10 * mm, tr_top - 25 * mm,
         tr_right - 5 * mm,  tr_top)
    _tri(BLUE,
         tr_right - 55 * mm, tr_top - 5 * mm,
         tr_right - 30 * mm, tr_top - 30 * mm,
         tr_right - 25 * mm, tr_top - 5 * mm)

    # ── Logo + tagline ──────────────────────────────────────────────
    logo_cx = (sidebar_w + page_w) / 2 - 15 * mm   # centered in main content area
    logo_y = page_h - 22 * mm
    canv.setFillColor(NAVY)
    canv.setFont("Helvetica-Bold", 22)
    canv.drawCentredString(logo_cx, logo_y, "Just-Print.ie")

    # Tagline: PRINT.DESIGN.SIGNAGE.&MORE... — uppercase words navy, dots colored
    tagline_y = logo_y - 5 * mm
    canv.setFont("Helvetica-Bold", 7.5)
    parts = [
        ("PRINT", NAVY), (".", PINK),
        ("DESIGN", NAVY), (".", YELLOW),
        ("SIGNAGE", NAVY), (".", BLUE),
        ("&MORE...", NAVY),
    ]
    total_w = sum(canv.stringWidth(t, "Helvetica-Bold", 7.5) for t, _ in parts)
    x = logo_cx - total_w / 2
    for txt, color in parts:
        canv.setFillColor(color)
        canv.drawString(x, tagline_y, txt)
        x += canv.stringWidth(txt, "Helvetica-Bold", 7.5)

    # ── Bottom-left decorative triangles ───────────────────────────
    _tri(PINK,    sidebar_w,           0,   sidebar_w + 25 * mm, 0,   sidebar_w,           25 * mm)
    _tri(YELLOW,  sidebar_w + 15 * mm, 0,   sidebar_w + 40 * mm, 0,   sidebar_w + 15 * mm, 20 * mm)
    _tri(BLUE,    sidebar_w + 30 * mm, 0,   sidebar_w + 50 * mm, 0,   sidebar_w + 30 * mm, 15 * mm)

    # ── Footer block ──────────────────────────────────────────────
    # We draw from the bottom upward. The flowable frame stops above this.
    content_left = sidebar_w + 15 * mm

    # Contact line + address + reg/VAT (at the very bottom)
    canv.setFillColor(NAVY)
    canv.setFont("Helvetica-Bold", 10)
    canv.drawCentredString(page_w / 2 + sidebar_w / 2, 18 * mm,
                           f"T: {COMPANY_PHONE}    E: {COMPANY_EMAIL}    W: {COMPANY_WEB}")
    canv.setFont("Helvetica", 8.5)
    canv.drawCentredString(page_w / 2 + sidebar_w / 2, 13 * mm, COMPANY_ADDRESS)
    canv.setFont("Helvetica", 6.5)
    canv.setFillColor(MID_GREY)
    canv.drawCentredString(page_w / 2 + sidebar_w / 2, 9 * mm,
                           f"COMPANY REG. No. {COMPANY_REG}    VAT No. {COMPANY_VAT}")

    # Terms and Conditions block
    canv.setFillColor(NAVY)
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(content_left, 58 * mm, "TERMS AND CONDITIONS")
    canv.setFillColor(black)
    canv.setFont("Helvetica", 6.5)
    y = 55 * mm
    for bullet in TERMS_BULLETS:
        canv.drawString(content_left + 2 * mm, y, f"•  {bullet}")
        y -= 2.5 * mm

    # Retention of Title + Credit Accounts + Payment + IBAN block (above terms)
    canv.setFillColor(black)
    canv.setFont("Helvetica", 7.5)
    canv.drawString(content_left, 82 * mm,
                    "Retention of Title: The property of the goods shall not pass to the purchaser until payment is made in full")
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(content_left, 78 * mm,
                    "Credit Accounts Strictly 30 Days from Receipt of Invoice")
    canv.setFont("Helvetica", 7.5)
    canv.drawString(content_left, 72 * mm,
                    "We also accept payment via:  VISA   Mastercard   Laser")
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(content_left, 68 * mm,
                    f"IBAN:  {COMPANY_IBAN}      BIC:  {COMPANY_BIC}")


# ─────────────────── product description builder ───────────────────

def _build_description(quote) -> str:
    """Turn a Quote row into a human-readable product description for the table."""
    specs = quote.specs or {}
    product_key = quote.product_key or ""
    qty = specs.get("quantity")

    lines: list[str] = []

    if "business_cards" in product_key:
        lines.append("Business Cards")
        lines.append("85x55mm printed full colour")
        if specs.get("finish"):
            lines.append(f"{specs['finish'].replace('-', ' ').title()} finish, 400gsm silk")
        lines.append("Double-sided" if specs.get("double_sided") else "Single-sided")
    elif product_key.startswith("flyers_"):
        size = product_key.split("_")[1].upper()
        lines.append(f"{size} flyers")
        if specs.get("finish"):
            lines.append(f"170gsm {specs['finish'].replace('-', ' ')}")
        lines.append("Printed both sides" if specs.get("double_sided") else "Printed one side")
    elif "brochures" in product_key:
        lines.append("A4 Brochure (folds to A5/DL)")
        if specs.get("finish"):
            lines.append(f"170gsm {specs['finish'].replace('-', ' ')}, bi-fold")
    elif "compliment_slips" in product_key:
        lines.append("Compliment Slips")
        lines.append("DL (210x99mm), 120gsm uncoated")
        lines.append("Double-sided" if specs.get("double_sided") else "Single-sided")
    elif "letterheads" in product_key:
        lines.append("Letterheads")
        lines.append("A4, 120gsm uncoated bond")
        lines.append("Double-sided" if specs.get("double_sided") else "Single-sided")
    elif "ncr_pads" in product_key:
        size = "A5" if "a5" in product_key else "A4"
        lines.append(f"NCR Pads {size}")
        lines.append("Perforated & stitched, 50 sets per book")
        if specs.get("finish"):
            lines.append(specs["finish"].title())
    elif product_key.startswith("booklet"):
        fmt = specs.get("format", "A4").upper()
        binding = (specs.get("binding") or "").replace("_", " ").title()
        pages = specs.get("pages")
        cover = (specs.get("cover_type") or "").replace("_", " ").title()
        lines.append(f"{fmt} Booklet — {binding}")
        if pages:
            lines.append(f"{pages}pp")
        if cover:
            lines.append(f"{cover}")
    else:
        # Large-format or unknown — fall back to prettified key
        lines.append((product_key or "Item").replace("_", " ").title())

    return "<br/>".join(lines)


# ───────────────────────── main entry ─────────────────────────

def generate_quote_pdf(quote) -> bytes:
    """
    Generate a branded PDF quote from a Quote DB record.

    Matches Justin's canonical quote layout (see module docstring).
    """
    buf = io.BytesIO()
    page_w, page_h = A4

    # Frame geometry:
    # - left margin = sidebar (11mm) + breathing room (15mm)
    # - top margin leaves room for logo + tagline (~30mm)
    # - bottom margin leaves room for Terms + address + reg line (~95mm)
    sidebar_w = 11 * mm
    frame_left = sidebar_w + 15 * mm
    frame_right = 15 * mm
    frame_top = 30 * mm
    frame_bottom = 92 * mm

    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=frame_left, rightMargin=frame_right,
        topMargin=frame_top, bottomMargin=frame_bottom,
    )
    content_w = page_w - frame_left - frame_right
    content_h = page_h - frame_top - frame_bottom

    frame = Frame(
        frame_left, frame_bottom, content_w, content_h,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="quote", frames=[frame], onPage=_draw_page_frame)])

    # ── styles ──
    body_style = ParagraphStyle(
        "Body", fontName="Helvetica", fontSize=10, textColor=black, leading=13,
    )
    body_right = ParagraphStyle("BodyR", parent=body_style, alignment=TA_RIGHT)
    body_center = ParagraphStyle("BodyC", parent=body_style, alignment=TA_CENTER)
    cell_style = ParagraphStyle(
        "Cell", fontName="Helvetica", fontSize=9, textColor=black, leading=12,
    )
    cell_right = ParagraphStyle("CellR", parent=cell_style, alignment=TA_RIGHT)
    cell_center = ParagraphStyle("CellC", parent=cell_style, alignment=TA_CENTER)

    elements: list = []

    # ── customer info lookup from conversation ──
    specs = quote.specs or {}
    cust_name = ""
    cust_email = ""
    cust_phone = ""
    cust_company = ""
    if quote.conversation_id:
        # Use the quote's ORM session if any, else skip gracefully
        from sqlalchemy.orm import object_session
        from db.models import Conversation
        sess = object_session(quote)
        if sess is not None:
            conv = sess.query(Conversation).filter_by(id=quote.conversation_id).first()
            if conv is not None:
                cust_name = (conv.customer_name or "").strip()
                cust_email = (conv.customer_email or "").strip()
                cust_phone = (conv.customer_phone or "").strip()

    # ── Job reference line (top of the flowable area) ──
    product_name_short = (quote.product_key or "").replace("_", " ").title()
    job_ref_text = f"{specs.get('quantity', '')} {product_name_short}".strip()
    ref_str = str(quote.id)
    date_str = (
        quote.created_at.strftime("%d/%m/%Y") if quote.created_at
        else datetime.now().strftime("%d/%m/%Y")
    )

    # Header: left col has Job Ref / Date / To / Company, right col has Ref / Tel / Email
    def _line(label, value):
        if not value:
            value = ""
        return f"<b>{label}</b> {value}"

    header_rows = [
        [Paragraph(_line("Job Reference:", job_ref_text), body_style),
         Paragraph(_line("Ref:", ref_str), body_style)],
        [Paragraph(_line("Date:", date_str), body_style),
         Paragraph(_line("Tel:", cust_phone), body_style)],
        [Paragraph(_line("To:", cust_name), body_style),
         Paragraph(_line("Email:", cust_email), body_style)],
    ]
    if cust_company:
        header_rows.append([Paragraph(_line("Company:", cust_company), body_style), ""])

    header_tbl = Table(
        header_rows,
        colWidths=[content_w * 0.58, content_w * 0.42],
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 6 * mm))

    # ── Main pricing table ──
    qty = int(specs.get("quantity", 1)) if specs.get("quantity") else 1

    header_cell_style = ParagraphStyle(
        "TH", fontName="Helvetica-Bold", fontSize=9, textColor=white,
        alignment=TA_CENTER, leading=11,
    )
    th_left = ParagraphStyle("THL", parent=header_cell_style, alignment=TA_LEFT)
    th_right = ParagraphStyle("THR", parent=header_cell_style, alignment=TA_RIGHT)

    rows = [
        # Header row
        [
            Paragraph("DESCRIPTION", th_left),
            Paragraph("QTY", header_cell_style),
            Paragraph("PRICE", header_cell_style),
            Paragraph("VAT", header_cell_style),
            Paragraph("TOTAL", th_right),
        ],
        # Product row
        [
            Paragraph(_build_description(quote), cell_style),
            Paragraph(str(qty), cell_center),
            Paragraph(f"€{quote.final_price_ex_vat:.2f}", cell_right),
            Paragraph(f"€{quote.vat_amount:.2f}", cell_right),
            Paragraph(f"€{quote.final_price_inc_vat:.2f}", cell_right),
        ],
    ]

    # Artwork row — only when the customer opted in + Craig computed a cost.
    has_artwork = bool(quote.artwork_cost and float(quote.artwork_cost) > 0)
    if has_artwork:
        artwork_ex = float(quote.artwork_cost)
        # Artwork is a service → standard Irish VAT 23%
        artwork_vat = round(artwork_ex * 0.23, 2)
        artwork_inc = round(artwork_ex + artwork_vat, 2)
        # Artwork rate is €65/hr ex VAT — derive implied hours for display
        hours = artwork_ex / 65.0 if artwork_ex > 0 else 0
        hours_str = f"{hours:.1f}".rstrip("0").rstrip(".")
        artwork_desc = (
            "Artwork / design<br/>"
            f"({hours_str} hr @ €65 per hr ex VAT)"
        )
        rows.append([
            Paragraph(artwork_desc, cell_style),
            Paragraph("1", cell_center),
            Paragraph(f"€{artwork_ex:.2f}", cell_right),
            Paragraph(f"€{artwork_vat:.2f}", cell_right),
            Paragraph(f"€{artwork_inc:.2f}", cell_right),
        ])

    table = Table(
        rows,
        colWidths=[
            content_w * 0.46,   # DESCRIPTION
            content_w * 0.10,   # QTY
            content_w * 0.14,   # PRICE
            content_w * 0.12,   # VAT
            content_w * 0.18,   # TOTAL
        ],
    )
    table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BACKGROUND", (2, 0), (2, 0), PINK),   # pink pop on the PRICE column header
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3 * mm),
        # Body rows
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, -1), 3.5 * mm),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3.5 * mm),
        # Padding on both axes
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        # Thin grid to echo Justin's PDF
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER_GREY),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER_GREY),
    ]))
    elements.append(table)

    doc.build(elements)
    return buf.getvalue()
