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

# Tagline colors — use DARKER alternates for words that would be unreadable
# on white at small sizes (pure yellow vanishes). These are still brand-
# family but stay legible in print + PDF viewers.
TAGLINE_PINK = HexColor("#e30686")
TAGLINE_GOLD = HexColor("#c4a017")   # darker than #feea03 for contrast
TAGLINE_BLUE = HexColor("#3e8fcd")
TAGLINE_LIME = HexColor("#9ca700")   # darker lime

# Payment card brand colors
VISA_NAVY = HexColor("#1A1F71")
VISA_GOLD = HexColor("#F7B600")
MC_RED = HexColor("#EB001B")
MC_ORANGE = HexColor("#F79E1B")
LASER_RED = HexColor("#E50039")


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


# ───────────────────────── payment card icons ─────────────────────────

def _draw_payment_icons(canv, x, y):
    """
    Draw small VISA / Mastercard / Laser card icons starting at (x, y).
    Returns the x position after the last icon so caller can continue drawing.

    Each card is ~13mm wide × 8mm tall, drawn with reportlab primitives
    (no external image assets). Good enough to read at PDF viewer zoom.
    """
    icon_w = 13 * mm
    icon_h = 8 * mm
    gap = 2 * mm
    corner_r = 1 * mm

    # ── VISA ──────────────────────────────────────────────
    canv.setFillColor(VISA_NAVY)
    canv.roundRect(x, y, icon_w, icon_h, corner_r, fill=1, stroke=0)
    # Gold stripe
    canv.setFillColor(VISA_GOLD)
    canv.rect(x + icon_w - 2.5 * mm, y + 1 * mm, 1.5 * mm, icon_h - 2 * mm, fill=1, stroke=0)
    # "VISA" wordmark
    canv.setFillColor(white)
    canv.setFont("Helvetica-BoldOblique", 7)
    canv.drawString(x + 2 * mm, y + icon_h / 2 - 1.5 * mm, "VISA")

    # ── Mastercard ────────────────────────────────────────
    x2 = x + icon_w + gap
    canv.setFillColor(white)
    canv.setStrokeColor(BORDER_GREY)
    canv.setLineWidth(0.3)
    canv.roundRect(x2, y, icon_w, icon_h, corner_r, fill=1, stroke=1)
    canv.setLineWidth(1)
    # Two interlocking circles, red + orange
    cy = y + icon_h / 2 + 0.5 * mm
    r = 2.2 * mm
    canv.setFillColor(MC_RED)
    canv.circle(x2 + 5 * mm, cy, r, fill=1, stroke=0)
    canv.setFillColor(MC_ORANGE)
    canv.circle(x2 + 7.5 * mm, cy, r, fill=1, stroke=0)
    # "mastercard" mini-label under
    canv.setFillColor(HexColor("#222222"))
    canv.setFont("Helvetica-Bold", 3.5)
    canv.drawCentredString(x2 + icon_w / 2, y + 1 * mm, "mastercard")

    # ── Laser ──────────────────────────────────────────────
    x3 = x2 + icon_w + gap
    canv.setFillColor(white)
    canv.setStrokeColor(BORDER_GREY)
    canv.setLineWidth(0.3)
    canv.roundRect(x3, y, icon_w, icon_h, corner_r, fill=1, stroke=1)
    canv.setLineWidth(1)
    canv.setFillColor(LASER_RED)
    canv.setFont("Helvetica-BoldOblique", 7)
    canv.drawCentredString(x3 + icon_w / 2, y + icon_h / 2 - 1.2 * mm, "laser")

    return x3 + icon_w


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
    # Bigger, more dramatic, overlapping rainbow — matches Justin's PDF
    # where the triangles fill roughly the top-right quarter of the page.
    def _tri(color, x1, y1, x2, y2, x3, y3):
        canv.setFillColor(color)
        p = canv.beginPath()
        p.moveTo(x1, y1); p.lineTo(x2, y2); p.lineTo(x3, y3); p.close()
        canv.drawPath(p, fill=1, stroke=0)

    tr_right = page_w
    tr_top = page_h
    # Biggest: pink, from top-right corner down and left
    _tri(PINK,
         tr_right - 70 * mm, tr_top,
         tr_right,            tr_top - 70 * mm,
         tr_right,            tr_top)
    # Middle: blue, overlapping pink
    _tri(BLUE,
         tr_right - 55 * mm, tr_top,
         tr_right - 15 * mm, tr_top - 45 * mm,
         tr_right - 15 * mm, tr_top)
    # Smallest/front: yellow
    _tri(YELLOW,
         tr_right - 35 * mm, tr_top,
         tr_right - 5 * mm,  tr_top - 32 * mm,
         tr_right - 5 * mm,  tr_top)

    # ── Logo + tagline ──────────────────────────────────────────────
    logo_cx = (sidebar_w + page_w) / 2 - 20 * mm   # centered in main content area (a bit left of dead-center to balance the triangles)
    logo_y = page_h - 22 * mm
    canv.setFillColor(NAVY)
    canv.setFont("Helvetica-Bold", 24)
    canv.drawCentredString(logo_cx, logo_y, "Just-Print.ie")

    # Tagline: fully colored — each word in its own brand color.
    # PRINT pink . DESIGN gold . SIGNAGE blue . &MORE... lime
    tagline_y = logo_y - 5.5 * mm
    canv.setFont("Helvetica-Bold", 8.5)
    parts = [
        ("PRINT", TAGLINE_PINK),
        ("DESIGN", TAGLINE_GOLD),
        ("SIGNAGE", TAGLINE_BLUE),
        ("&MORE...", TAGLINE_LIME),
    ]
    dot_sep_w = 2.2 * mm   # visual space for the colored dot separator
    total_w = sum(canv.stringWidth(t, "Helvetica-Bold", 8.5) for t, _ in parts)
    total_w += dot_sep_w * (len(parts) - 1)
    x = logo_cx - total_w / 2
    for i, (txt, color) in enumerate(parts):
        canv.setFillColor(color)
        canv.drawString(x, tagline_y, txt)
        x += canv.stringWidth(txt, "Helvetica-Bold", 8.5)
        # Colored dot between words (use the next word's color as accent)
        if i < len(parts) - 1:
            next_color = parts[i + 1][1]
            canv.setFillColor(next_color)
            canv.circle(x + dot_sep_w / 2, tagline_y + 1.2 * mm, 0.7 * mm, fill=1, stroke=0)
            x += dot_sep_w

    # ── Bottom-left decorative triangles ───────────────────────────
    # Also bigger + more dramatic to mirror Justin's
    _tri(PINK,    sidebar_w,           0,   sidebar_w + 35 * mm, 0,   sidebar_w,           35 * mm)
    _tri(YELLOW,  sidebar_w + 20 * mm, 0,   sidebar_w + 50 * mm, 0,   sidebar_w + 20 * mm, 28 * mm)
    _tri(BLUE,    sidebar_w + 40 * mm, 0,   sidebar_w + 62 * mm, 0,   sidebar_w + 40 * mm, 20 * mm)

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

    # Retention of Title + Credit Accounts  (top of footer zone)
    canv.setFillColor(black)
    canv.setFont("Helvetica", 7.5)
    canv.drawString(content_left, 85 * mm,
                    "Retention of Title: The property of the goods shall not pass to the purchaser until payment is made in full")
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(content_left, 80 * mm,
                    "Credit Accounts Strictly 30 Days from Receipt of Invoice")

    # Payment methods line + real card icons (VISA / Mastercard / Laser)
    # Icons drawn at y=67 (height 8mm → top at 75mm), 5mm below the Credit
    # Accounts line at 80mm so they don't overlap.
    canv.setFillColor(black)
    canv.setFont("Helvetica", 7.5)
    canv.drawString(content_left, 72 * mm, "We also accept payment via:")
    _text_w = canv.stringWidth("We also accept payment via:", "Helvetica", 7.5)
    _draw_payment_icons(canv, content_left + _text_w + 3 * mm, 67 * mm)

    # IBAN / BIC below icons
    canv.setFillColor(black)
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(content_left, 59 * mm,
                    f"IBAN:  {COMPANY_IBAN}      BIC:  {COMPANY_BIC}")

    # Terms and Conditions block
    canv.setFillColor(NAVY)
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(content_left, 52 * mm, "TERMS AND CONDITIONS")
    canv.setFillColor(black)
    canv.setFont("Helvetica", 6.5)
    y = 49 * mm
    for bullet in TERMS_BULLETS:
        canv.drawString(content_left + 2 * mm, y, f"\u2022  {bullet}")
        y -= 2.5 * mm


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

    If the quote belongs to a Conversation that has OTHER quotes too
    (customer asked about multiple products in the same thread), all of
    them are rendered as separate line items in a single PDF — so the
    customer receives ONE document listing everything they discussed,
    with a single grand total at the bottom. Matches how Justin's
    canonical quote (Quote 1519487) lists multiple items.

    Matches Justin's canonical quote layout (see module docstring).
    """
    buf = io.BytesIO()
    page_w, page_h = A4

    # Collect all quotes in the same conversation, ordered oldest-first so
    # the table reads chronologically. Falls back to [quote] if no session
    # is attached (e.g. test fixture) or the quote has no conversation.
    from sqlalchemy.orm import object_session
    from db.models import Quote as _Quote

    all_quotes: list = [quote]
    try:
        sess = object_session(quote)
    except Exception:
        sess = None
    if sess is not None and getattr(quote, "conversation_id", None):
        fetched = (
            sess.query(_Quote)
            .filter_by(conversation_id=quote.conversation_id)
            .order_by(_Quote.created_at.asc(), _Quote.id.asc())
            .all()
        )
        if fetched:
            all_quotes = fetched

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

    # ── Main pricing table — iterates every quote in the conversation ──
    header_cell_style = ParagraphStyle(
        "TH", fontName="Helvetica-Bold", fontSize=9, textColor=white,
        alignment=TA_CENTER, leading=11,
    )
    th_left = ParagraphStyle("THL", parent=header_cell_style, alignment=TA_LEFT)
    th_right = ParagraphStyle("THR", parent=header_cell_style, alignment=TA_RIGHT)

    rows = [[
        Paragraph("DESCRIPTION", th_left),
        Paragraph("QTY", header_cell_style),
        Paragraph("PRICE", header_cell_style),
        Paragraph("VAT", header_cell_style),
        Paragraph("TOTAL", th_right),
    ]]

    # Running totals across all items
    total_ex = 0.0
    total_vat = 0.0
    total_inc = 0.0

    # One row per Quote row + one extra row if it carries artwork.
    for q in all_quotes:
        q_specs = q.specs or {}
        q_qty = int(q_specs.get("quantity", 1)) if q_specs.get("quantity") else 1

        rows.append([
            Paragraph(_build_description(q), cell_style),
            Paragraph(str(q_qty), cell_center),
            Paragraph(f"\u20ac{q.final_price_ex_vat:.2f}", cell_right),
            Paragraph(f"\u20ac{q.vat_amount:.2f}", cell_right),
            Paragraph(f"\u20ac{q.final_price_inc_vat:.2f}", cell_right),
        ])
        total_ex += float(q.final_price_ex_vat or 0)
        total_vat += float(q.vat_amount or 0)
        total_inc += float(q.final_price_inc_vat or 0)

        if q.artwork_cost and float(q.artwork_cost) > 0:
            artwork_ex = float(q.artwork_cost)
            # Artwork is a service → standard Irish VAT 23%
            artwork_vat_line = round(artwork_ex * 0.23, 2)
            artwork_inc_line = round(artwork_ex + artwork_vat_line, 2)
            hours = artwork_ex / 65.0 if artwork_ex > 0 else 0
            hours_str = f"{hours:.1f}".rstrip("0").rstrip(".")
            artwork_desc = (
                "Artwork / design<br/>"
                f"({hours_str} hr @ \u20ac65 per hr ex VAT)"
            )
            rows.append([
                Paragraph(artwork_desc, cell_style),
                Paragraph("1", cell_center),
                Paragraph(f"\u20ac{artwork_ex:.2f}", cell_right),
                Paragraph(f"\u20ac{artwork_vat_line:.2f}", cell_right),
                Paragraph(f"\u20ac{artwork_inc_line:.2f}", cell_right),
            ])
            total_ex += artwork_ex
            total_vat += artwork_vat_line
            total_inc += artwork_inc_line

    # Grand total row — only when there's more than one line item.
    has_multiple_lines = len(rows) > 2   # header + more than one body row
    if has_multiple_lines:
        total_label_style = ParagraphStyle(
            "Total", fontName="Helvetica-Bold", fontSize=10, textColor=NAVY,
            alignment=TA_RIGHT, leading=12,
        )
        total_val_style = ParagraphStyle(
            "TotalV", parent=total_label_style, alignment=TA_RIGHT, textColor=NAVY,
        )
        rows.append([
            Paragraph("TOTAL", total_label_style),
            Paragraph("", cell_style),
            Paragraph(f"\u20ac{total_ex:.2f}", total_val_style),
            Paragraph(f"\u20ac{total_vat:.2f}", total_val_style),
            Paragraph(f"\u20ac{total_inc:.2f}", total_val_style),
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
    style_cmds = [
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
        # Thin grid
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER_GREY),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER_GREY),
    ]
    if has_multiple_lines:
        # Subtle background on the grand-total row
        style_cmds.append(("BACKGROUND", (0, -1), (-1, -1), HexColor("#f5f5f5")))
        style_cmds.append(("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY))
    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    doc.build(elements)
    return buf.getvalue()
