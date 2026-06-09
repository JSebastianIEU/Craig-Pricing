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
import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Table, TableStyle, Spacer, Paragraph,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER


# v36 — brand asset paths. When these PNGs exist, the page-frame draws
# them via canvas.drawImage() instead of falling back to drawn
# primitives. Lets Justin (or whoever owns the brand) drop in the
# real artwork and get a pixel-perfect quote PDF without code edits.
_ASSET_DIR = os.path.join(os.path.dirname(__file__), "static", "images")
_ASSET_PATHS = {
    "corner_top_right":   os.path.join(_ASSET_DIR, "quote-corner-top-right.png"),
    "corner_bottom_left": os.path.join(_ASSET_DIR, "quote-corner-bottom-left.png"),
    "payment_cards":      os.path.join(_ASSET_DIR, "quote-payment-cards.png"),
    "logo":               os.path.join(_ASSET_DIR, "quote-logo.png"),
    # v36c — operator-supplied QUOTATION sidebar rectangle (orange tile
    # with chat-bubble icon + rotated 'QUOTATION' text). When present
    # we drop it as a discrete rectangle in the top-left instead of
    # painting a full-page orange strip + drawn primitives.
    "quotation_sidebar":  os.path.join(_ASSET_DIR, "quotation-sidebar-chatbubble.png"),
}


def _asset_or_none(name: str):
    """Return an ImageReader if the asset exists, else None.
    Cached the first time so repeated page renders don't re-read disk."""
    path = _ASSET_PATHS.get(name)
    if not path or not os.path.isfile(path):
        return None
    try:
        return ImageReader(path)
    except Exception:
        return None


# ───────────────────────── brand ─────────────────────────
NAVY = HexColor("#040f2a")
PINK = HexColor("#e30686")
YELLOW = HexColor("#feea03")
BLUE = HexColor("#3e8fcd")
LIME = HexColor("#c4cf00")
ORANGE = HexColor("#f37021")
MID_GREY = HexColor("#888888")
BORDER_GREY = HexColor("#cccccc")

# v36c — table header colour. Justin's canonical template uses a
# uniform dark-grey header bar (no per-column accent), white text.
# We keep TABLE_HEADER_PRICE_BG defined for backwards compat but
# point it at the same grey so the visual ends up uniform.
TABLE_HEADER_BG = HexColor("#3a3a3a")        # dark charcoal grey
TABLE_HEADER_PRICE_BG = HexColor("#3a3a3a")  # same — no PRICE accent

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
    Draw VISA / Mastercard / Laser card icons starting at (x, y).
    Returns the x position after the last icon so caller can continue drawing.

    v36 — when static/images/quote-payment-cards.png exists, draw the
    operator-supplied image instead of the primitive icons. The image
    is sized so the row of three cards spans roughly the same width
    as the original (~45mm) and 8mm tall.

    Falls back to drawn primitives (rounded rects + brand-coloured marks)
    when no asset is present, so dev environments still produce a
    legible, branded preview.
    """
    pay_asset = _asset_or_none("payment_cards")
    if pay_asset is not None:
        # Operator-supplied 'three cards in a row' artwork.
        target_w = 47 * mm
        iw, ih = pay_asset.getSize()
        target_h = target_w * (ih / float(iw)) if iw else 9 * mm
        canv.drawImage(
            pay_asset, x, y,
            width=target_w, height=target_h,
            mask="auto", preserveAspectRatio=True,
        )
        return x + target_w

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

    # ── Orange vertical sidebar with chat-bubble icon at top ──────────
    # v36 — wider strip + chat-bubble icon to match Justin's canonical
    # Quote Template. The bubble is drawn with primitives (white circle
    # with three dark dots inside, in the same style as a chat-message
    # icon). The QUOTATION text is rotated 90° and centered vertically.
    # v36d — sidebar bumped to 18mm wide so the QUOTATION rectangle
    # has the visual presence of Justin's canonical template (taller +
    # wider). Aspect ratio of the supplied PNG drives the height.
    sidebar_w = 18 * mm

    # v36c — operator-supplied PNG drops the discrete QUOTATION
    # rectangle into the top-left. We anchor at the top edge and let
    # the image's natural height determine how far down it extends
    # (typically ~150mm for the supplied artwork at 18mm width).
    # When no asset exists, fall through to the legacy full-page
    # primitives below.
    sidebar_asset = _asset_or_none("quotation_sidebar")
    if sidebar_asset is not None:
        iw, ih = sidebar_asset.getSize()
        sidebar_h = sidebar_w * (ih / float(iw)) if iw else 100 * mm
        canv.drawImage(
            sidebar_asset,
            0, page_h - sidebar_h,
            width=sidebar_w, height=sidebar_h,
            mask="auto", preserveAspectRatio=True,
        )
        # Skip the primitive sidebar drawing below — we're done.
        _SIDEBAR_DRAWN_FROM_ASSET = True
    else:
        _SIDEBAR_DRAWN_FROM_ASSET = False

    if not _SIDEBAR_DRAWN_FROM_ASSET:
        canv.setFillColor(ORANGE)
        canv.rect(0, 0, sidebar_w, page_h, fill=1, stroke=0)

    if not _SIDEBAR_DRAWN_FROM_ASSET:
        # Chat-bubble icon at top of sidebar — only used when the
        # asset PNG is absent. White speech-bubble with three dots.
        bubble_cx = sidebar_w / 2
        bubble_cy = page_h - 12 * mm
        bubble_r = 4.5 * mm
        canv.setFillColor(white)
        canv.circle(bubble_cx, bubble_cy, bubble_r, fill=1, stroke=0)
        p = canv.beginPath()
        p.moveTo(bubble_cx + 1.5 * mm, bubble_cy - bubble_r + 0.2 * mm)
        p.lineTo(bubble_cx + 4.5 * mm, bubble_cy - bubble_r - 1.5 * mm)
        p.lineTo(bubble_cx + 3.0 * mm, bubble_cy - bubble_r - 0.5 * mm)
        p.close()
        canv.drawPath(p, fill=1, stroke=0)
        canv.setFillColor(NAVY)
        dot_r = 0.7 * mm
        canv.circle(bubble_cx - 2 * mm, bubble_cy, dot_r, fill=1, stroke=0)
        canv.circle(bubble_cx,           bubble_cy, dot_r, fill=1, stroke=0)
        canv.circle(bubble_cx + 2 * mm, bubble_cy, dot_r, fill=1, stroke=0)

        canv.setFillColor(white)
        canv.setFont("Helvetica-Bold", 22)
        canv.saveState()
        canv.translate(sidebar_w / 2 + 3 * mm, page_h * 0.50)
        canv.rotate(90)
        canv.drawString(0, 0, "QUOTATION")
        canv.restoreState()

    # ── Top-right decorative artwork ──────────────────────────────────
    # v36 — if the operator dropped the real brand PNG at
    # static/images/quote-corner-top-right.png, use that. Otherwise
    # fall back to drawn primitive triangles so dev / fresh deploys
    # still produce a recognisable layout.
    def _tri(color, x1, y1, x2, y2, x3, y3):
        canv.setFillColor(color)
        p = canv.beginPath()
        p.moveTo(x1, y1); p.lineTo(x2, y2); p.lineTo(x3, y3); p.close()
        canv.drawPath(p, fill=1, stroke=0)

    tr_asset = _asset_or_none("corner_top_right")
    if tr_asset is not None:
        # v36e — 65mm wide. The 85mm version was too dominant and
        # crowded the logo. 65mm sits cleanly in the corner, leaves
        # room for a slightly narrower logo to the left, and
        # matches the visual weight of the sidebar QUOTATION.
        tr_w = 65 * mm
        iw, ih = tr_asset.getSize()
        tr_h = tr_w * (ih / float(iw)) if iw else tr_w
        canv.drawImage(
            tr_asset,
            page_w - tr_w, page_h - tr_h,
            width=tr_w, height=tr_h,
            mask="auto", preserveAspectRatio=True,
        )
    else:
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
    # v36e — logo width 75mm (was 90mm — slightly tighter so it
    # doesn't fight with the corner triangles). Centre point pulled
    # ~35mm left of mid-content so the logo sits LEFT of where the
    # top-right triangles begin (~145mm from page-left).
    logo_cx = (sidebar_w + page_w) / 2 - 35 * mm
    logo_y = page_h - 22 * mm

    logo_asset = _asset_or_none("logo")
    if logo_asset is not None:
        lw = 85 * mm
        iw, ih = logo_asset.getSize()
        lh = lw * (ih / float(iw)) if iw else 14 * mm
        canv.drawImage(
            logo_asset,
            logo_cx - lw / 2, logo_y - lh / 2,
            width=lw, height=lh,
            mask="auto", preserveAspectRatio=True,
        )
    else:
        canv.setFillColor(NAVY)
        canv.setFont("Helvetica-Bold", 24)
        canv.drawCentredString(logo_cx, logo_y, "Just-Print.ie")

        # Tagline: each word in its own brand colour separated by dots.
        # PRINT-pink . DESIGN-gold . SIGNAGE-blue . &MORE-lime
        tagline_y = logo_y - 5.5 * mm
        canv.setFont("Helvetica-Bold", 8.5)
        parts = [
            ("PRINT", TAGLINE_PINK),
            ("DESIGN", TAGLINE_GOLD),
            ("SIGNAGE", TAGLINE_BLUE),
            ("&MORE...", TAGLINE_LIME),
        ]
        dot_sep_w = 2.2 * mm
        total_w = sum(canv.stringWidth(t, "Helvetica-Bold", 8.5) for t, _ in parts)
        total_w += dot_sep_w * (len(parts) - 1)
        x = logo_cx - total_w / 2
        for i, (txt, color) in enumerate(parts):
            canv.setFillColor(color)
            canv.drawString(x, tagline_y, txt)
            x += canv.stringWidth(txt, "Helvetica-Bold", 8.5)
            if i < len(parts) - 1:
                next_color = parts[i + 1][1]
                canv.setFillColor(next_color)
                canv.circle(x + dot_sep_w / 2, tagline_y + 1.2 * mm, 0.7 * mm, fill=1, stroke=0)
                x += dot_sep_w

    # ── Bottom-left decorative artwork ────────────────────────────
    # v36 — use the operator-supplied PNG if present, else fall back
    # to drawn triangles. Anchored to bottom-left, just right of the
    # orange sidebar.
    bl_asset = _asset_or_none("corner_bottom_left")
    if bl_asset is not None:
        # v36c — anchored at the very LEFT edge (x=0) so the triangles
        # extend all the way to the page border without leaving a
        # white margin between them and the edge. The QUOTATION
        # sidebar PNG only covers the top ~110mm so the triangles at
        # y=0 don't overlap it. 50mm wide for a slightly more
        # dramatic visual presence (matches Justin's template).
        bl_w = 50 * mm
        iw, ih = bl_asset.getSize()
        bl_h = bl_w * (ih / float(iw)) if iw else bl_w
        canv.drawImage(
            bl_asset,
            0, 0,
            width=bl_w, height=bl_h,
            mask="auto", preserveAspectRatio=True,
        )
    else:
        _tri(PINK,    sidebar_w,           0,   sidebar_w + 35 * mm, 0,   sidebar_w,           35 * mm)
        _tri(YELLOW,  sidebar_w + 20 * mm, 0,   sidebar_w + 50 * mm, 0,   sidebar_w + 20 * mm, 28 * mm)
        _tri(BLUE,    sidebar_w + 40 * mm, 0,   sidebar_w + 62 * mm, 0,   sidebar_w + 40 * mm, 20 * mm)

    # ── Footer block ──────────────────────────────────────────────
    # We draw from the bottom upward. The flowable frame stops above this.
    # v36e — content_left tightened (5mm gap past sidebar, was 15mm) so
    # the main content block reads as visually centred on the page
    # rather than shifted right by the orange QUOTATION sidebar.
    content_left = sidebar_w + 5 * mm

    # Contact line + address bar + reg/VAT (bottom of page)
    # v36c — three layers, matching Justin's canonical Quote Template:
    #   1. Contact line (y=22mm) with PINK T:/E:/W: labels and NAVY values
    #   2. Black bar (y=10-19mm) with white address text
    #   3. COMPANY REG / VAT line (y=5mm) tiny grey on white
    contact_cx = (sidebar_w + page_w) / 2
    # v36e — footer_left ONLY applies to the TERMS AND CONDITIONS
    # block (modest shift so it clears the bottom-left triangle but
    # the long bullet lines don't run off the page). All other footer
    # items (Retention, Credit, payment row, IBAN/BIC) sit at
    # content_left; T/E/W + address are centred; COMPANY REG is
    # right-aligned — matching Justin's canonical template.
    footer_left = content_left + 18 * mm

    # 1. Contact line — T pink / E blue / W lime, matching Justin's
    # canonical template. The three labels are clearly distinct at
    # small sizes and align with the brand-tagline colour palette.
    contact_y = 38 * mm
    parts = [
        ("T: ",                  PINK),    # magenta
        (COMPANY_PHONE,           NAVY),
        ("    E: ",              BLUE),    # cyan-blue
        (COMPANY_EMAIL,           NAVY),
        ("    W: ",              TAGLINE_LIME),  # yellow-green / lime
        (COMPANY_WEB,             NAVY),
    ]
    canv.setFont("Helvetica-Bold", 10)
    total_w = sum(canv.stringWidth(t, "Helvetica-Bold", 10) for t, _ in parts)
    # v36e — contact line RIGHT-aligned to the right margin (matches
    # the canonical template where T/E/W, address and COMPANY REG all
    # end at the same right edge — the end of the divider line).
    contact_right = page_w - 15 * mm
    x = contact_right - total_w
    for txt, color in parts:
        canv.setFillColor(color)
        canv.drawString(x, contact_y, txt)
        x += canv.stringWidth(txt, "Helvetica-Bold", 10)

    # 2. Address line — plain text on white, centred. Width measured
    # below so the separator above it can align with where the
    # address actually starts (not the page margin).
    addr_font_size = 10

    # 3. Thin separator line above the address — RIGHT-aligned, with
    # its right edge at the same right margin as the contact line so
    # everything in the bottom-right block reads as one column.
    addr_w = canv.stringWidth(COMPANY_ADDRESS, "Helvetica", addr_font_size)
    canv.setStrokeColor(black)
    canv.setLineWidth(0.5)
    canv.line(contact_right - total_w, 33 * mm, contact_right, 33 * mm)

    # Address text — RIGHT-aligned at the right margin.
    canv.setFillColor(NAVY)
    canv.setFont("Helvetica", addr_font_size)
    canv.drawRightString(contact_right, 27 * mm, COMPANY_ADDRESS)

    # 4. COMPANY REG / VAT — RIGHT-aligned at the right margin
    # (matches the canonical placement and the rest of the column).
    canv.setFillColor(MID_GREY)
    canv.setFont("Helvetica", 6.5)
    canv.drawRightString(contact_right, 21 * mm,
                         f"COMPANY REG. No. {COMPANY_REG}    VAT No. {COMPANY_VAT}")

    # v36b — footer block pushed up ~20mm so the bottom-left triangle
    # asset (anchored at y=0, ~45mm tall) doesn't overlap the T&Cs.

    # Retention of Title + Credit Accounts  (top of footer zone),
    # pushed down so the table can use more of the page.
    canv.setFillColor(black)
    canv.setFont("Helvetica", 7.5)
    canv.drawString(content_left, 98 * mm,
                    "Retention of Title: The property of the goods shall not pass to the purchaser until payment is made in full")
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(content_left, 93 * mm,
                    "Credit Accounts Strictly 30 Days from Receipt of Invoice")

    # Payment cards row — at content_left, BELOW the labels above.
    _draw_payment_icons(canv, content_left, 80 * mm)

    # IBAN / BIC stacked to the right of the payment block — IBAN
    # aligned with the top of the cards row, BIC underneath.
    iban_x = content_left + 70 * mm
    canv.setFont("Helvetica-Bold", 7.5)
    canv.setFillColor(NAVY)
    canv.drawString(iban_x, 87 * mm, "IBAN:")
    canv.drawString(iban_x, 82 * mm, "BIC:")
    canv.setFillColor(black)
    canv.setFont("Helvetica", 7.5)
    canv.drawString(iban_x + 12 * mm, 87 * mm, COMPANY_IBAN)
    canv.drawString(iban_x + 12 * mm, 82 * mm, COMPANY_BIC)

    # Terms and Conditions — tight 3.3mm line height to look like
    # Justin's template (was 2.5mm, felt cramped).
    canv.setFillColor(NAVY)
    canv.setFont("Helvetica-Bold", 7.5)
    canv.drawString(footer_left, 68 * mm, "TERMS AND CONDITIONS")
    canv.setFillColor(black)
    canv.setFont("Helvetica", 7)
    y = 64 * mm
    for bullet in TERMS_BULLETS:
        canv.drawString(footer_left + 2 * mm, y, f"\u2022  {bullet}")
        y -= 3.0 * mm


# ─────────────────── product description builder ───────────────────

def _build_description(quote, product=None) -> str:
    """Turn a Quote row into a human-readable product description for the table.

    v36 — when `product` (the Product DB row) is supplied AND has a
    non-empty `description`, that description is used as the spec
    line under the bold product name. This is Justin's "knowledge
    base" surface — what he edits in Catalog → Products → Description
    is what appears verbatim on the customer's quote PDF, matching
    his canonical Quote 1519487 template ('85x55mm printed full
    colour both sides on 350gsm silk').

    Falls back to the legacy hardcoded specs when:
      - no Product row is passed, or
      - Product.description is null/empty
    so test fixtures + tenants who haven't filled in descriptions
    still get a sensible PDF.

    The product NAME (first line) is always rendered bold via <b>...</b>.
    Subsequent lines are plain. Dimensions in the request specs (v36
    width_mm/height_mm/area_sqm for per-sq/m + per-sheet products)
    get appended on their own line so the operator + customer see
    exactly what was priced.
    """
    specs = quote.specs or {}
    product_key = quote.product_key or ""

    # v36 — prefer the operator-edited description from the catalog.
    if product is not None:
        name = (getattr(product, "name", None) or product_key.replace("_", " ").title()).strip()
        desc = (getattr(product, "description", None) or "").strip()
        if desc:
            lines = [f"<b>{name}</b>", desc]
            # Append per-quote spec details (sides, finish, dimensions)
            # below the catalog description so the customer sees the
            # specifics of THIS order without hand-editing the catalog
            # description on every variation.
            spec_extras: list[str] = []
            if specs.get("double_sided") is True:
                spec_extras.append("Printed both sides")
            elif specs.get("double_sided") is False and "double_sided" in specs:
                spec_extras.append("Printed one side")
            finish = specs.get("finish")
            if finish:
                spec_extras.append(str(finish).replace("-", " ").replace("_", " ").title())
            # v36 — dimensions captured by the per-sqm / per-sheet engine
            w = specs.get("width_mm")
            h = specs.get("height_mm")
            area = specs.get("area_sqm")
            if w and h:
                spec_extras.append(f"Size: {w} × {h} mm")
            elif area:
                spec_extras.append(f"Area: {area} m²")
            if spec_extras:
                lines.append(" · ".join(spec_extras))
            return "<br/>".join(lines)

    # ── Legacy fallback path ────────────────────────────────────────
    # Used when no Product row is available (test fixtures) OR when
    # the Product row exists but description is empty.
    lines: list[str] = []

    if "business_cards" in product_key:
        lines.append("<b>Business Cards</b>")
        lines.append("85x55mm printed full colour")
        if specs.get("finish"):
            lines.append(f"{specs['finish'].replace('-', ' ').title()} finish, 400gsm silk")
        lines.append("Double-sided" if specs.get("double_sided") else "Single-sided")
    elif product_key.startswith("flyers_"):
        size = product_key.split("_")[1].upper()
        lines.append(f"<b>{size} flyers</b>")
        if specs.get("finish"):
            lines.append(f"170gsm {specs['finish'].replace('-', ' ')}")
        lines.append("Printed both sides" if specs.get("double_sided") else "Printed one side")
    elif "brochures" in product_key:
        lines.append("<b>A4 Brochure (folds to A5/DL)</b>")
        if specs.get("finish"):
            lines.append(f"170gsm {specs['finish'].replace('-', ' ')}, bi-fold")
    elif "compliment_slips" in product_key:
        lines.append("<b>Compliment Slips</b>")
        lines.append("DL (210x99mm), 120gsm uncoated")
        lines.append("Double-sided" if specs.get("double_sided") else "Single-sided")
    elif "letterheads" in product_key:
        lines.append("<b>Letterheads</b>")
        lines.append("A4, 120gsm uncoated bond")
        lines.append("Double-sided" if specs.get("double_sided") else "Single-sided")
    elif "ncr_books" in product_key:
        size = "A5" if "a5" in product_key else "A4"
        lines.append(f"<b>NCR Books {size}</b>")
        lines.append("Perforated & stitched, 50 sets per book")
        if specs.get("finish"):
            lines.append(specs["finish"].title())
    elif product_key.startswith("booklet"):
        fmt = specs.get("format", "A4").upper()
        binding = (specs.get("binding") or "").replace("_", " ").title()
        pages = specs.get("pages")
        cover = (specs.get("cover_type") or "").replace("_", " ").title()
        lines.append(f"<b>{fmt} Booklet — {binding}</b>")
        if pages:
            lines.append(f"{pages}pp")
        if cover:
            lines.append(f"{cover}")
    else:
        # Large-format or unknown — fall back to prettified key
        lines.append(f"<b>{(product_key or 'Item').replace('_', ' ').title()}</b>")

    # v36 — append dimensions on the legacy path too, when present
    w = specs.get("width_mm")
    h = specs.get("height_mm")
    area = specs.get("area_sqm")
    if w and h:
        lines.append(f"Size: {w} × {h} mm")
    elif area:
        lines.append(f"Area: {area} m²")

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
    # v36f — frame_top 65mm: Job Reference row pulled higher up the
    # page (was 80, still felt too far below the logo). With the
    # 65mm triangles + 85mm logo, content can comfortably start
    # at y=page_h-65mm without overlap.
    sidebar_w = 18 * mm
    # v36e — frame_left tightened to 5mm past the sidebar (was 15mm)
    # so the main content block sits visually centred on the page
    # rather than shifted right by the orange QUOTATION sidebar.
    frame_left = sidebar_w + 5 * mm
    frame_right = 15 * mm
    frame_top = 65 * mm
    frame_bottom = 105 * mm

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

    # v36e — fall back to fields on the Quote row itself (some sources,
    # like the Missive integration + form quotes, set these directly
    # without a Conversation). Only fill the slots conversation lookup
    # left empty.
    if not cust_name:
        cust_name = (getattr(quote, "customer_name", None) or "").strip()
    if not cust_email:
        cust_email = (getattr(quote, "customer_email", None) or "").strip()
    if not cust_phone:
        cust_phone = (getattr(quote, "customer_phone", None) or "").strip()
    if not cust_company:
        cust_company = (getattr(quote, "customer_company", None) or "").strip()

    # ── Job reference line (top of the flowable area) ──
    product_name_short = (quote.product_key or "").replace("_", " ").title()
    job_ref_text = f"{specs.get('quantity', '')} {product_name_short}".strip()
    ref_str = str(quote.id)
    date_str = (
        quote.created_at.strftime("%d/%m/%Y") if quote.created_at
        else datetime.now().strftime("%d/%m/%Y")
    )

    # Header: split into TWO tables to match Justin's canonical layout.
    # First table is just the Job Reference row (full-width, with Ref
    # right-aligned). Spacer. Second table has Date/To/Company on
    # the left and Tel/Email on the right.
    def _line(label, value):
        if not value:
            value = ""
        return f"<b>{label}</b> {value}"

    body_right_style = ParagraphStyle(
        "BodyR", parent=body_style, alignment=TA_RIGHT,
    )

    # ── 1. Job Reference row ─────────────────────────────────────────
    job_ref_table = Table(
        [[
            Paragraph(_line("Job Reference:", job_ref_text), body_style),
            Paragraph(_line("Ref:", ref_str), body_right_style),
        ]],
        colWidths=[content_w * 0.7, content_w * 0.3],
    )
    job_ref_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    elements.append(job_ref_table)
    elements.append(Spacer(1, 8 * mm))

    # ── 2. Date / To / Company  +  Tel / Email ──────────────────────
    # v36f — 4-column layout (label | value | label | value) so the
    # VALUES align vertically regardless of label width. With the
    # old 2-column "<b>label</b> value" approach, "Company:" pushed
    # its value further right than "Date:", so each row's value
    # started at a different x.
    label_style = ParagraphStyle(
        "Label", parent=body_style, fontName="Helvetica-Bold",
    )
    value_style = body_style

    def _lbl(t):
        return Paragraph(t, label_style)

    def _val(t):
        return Paragraph(t or "", value_style)

    contact_rows = [
        [_lbl("Date:"),  _val(date_str),  _lbl("Tel:"),   _val(cust_phone)],
        [_lbl("To:"),    _val(cust_name), _lbl("Email:"), _val(cust_email)],
    ]
    if cust_company:
        contact_rows.append([
            _lbl("Company:"), _val(cust_company), "", "",
        ])

    contact_tbl = Table(
        contact_rows,
        # Left label fixed wide enough for "Company:" so values align;
        # right label fixed wide enough for "Email:". Remaining width
        # split between the two value columns.
        colWidths=[
            22 * mm,                                    # left label
            content_w * 0.58 - 22 * mm,                 # left value
            18 * mm,                                    # right label
            content_w * 0.42 - 18 * mm,                 # right value
        ],
    )
    contact_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    elements.append(contact_tbl)
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

    # v36 \u2014 preload products by key so each row can show the operator-
    # edited Catalog description verbatim. We keep this in a small
    # dict keyed by product_key to avoid an N+1 SELECT inside the loop.
    from db.models import Product as _Product
    products_by_key: dict = {}
    if sess is not None:
        keys = sorted({q.product_key for q in all_quotes if q.product_key})
        if keys:
            org = quote.organization_slug
            for p in (
                sess.query(_Product)
                .filter(_Product.organization_slug == org)
                .filter(_Product.key.in_(keys))
                .all()
            ):
                products_by_key[p.key] = p

    # One row per Quote row + one extra row if it carries artwork.
    for q in all_quotes:
        q_specs = q.specs or {}
        q_qty = int(q_specs.get("quantity", 1)) if q_specs.get("quantity") else 1
        prod = products_by_key.get(q.product_key)

        rows.append([
            Paragraph(_build_description(q, product=prod), cell_style),
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
    # v36d — table styling per user feedback:
    #   - body cells get a soft light-grey background
    #   - borders set to WHITE with a wider line so the cells appear
    #     to "float" with white gaps between them (modern card-table
    #     look, matching Justin's canonical layout).
    BODY_CELL_BG = HexColor("#f1f1f1")
    style_cmds = [
        # Header row — uniform dark grey, white text (v36c).
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_BG),
        ("BACKGROUND", (2, 0), (2, 0), TABLE_HEADER_PRICE_BG),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3 * mm),
        # Body rows: soft light-grey fill so the WHITE borders read as
        # visual gaps between cells. Without the fill, white borders
        # on a white page would be invisible.
        ("BACKGROUND", (0, 1), (-1, -1), BODY_CELL_BG),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, -1), 4.5 * mm),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4.5 * mm),
        # Padding
        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
        # WHITE borders, wider line — creates the "floating card"
        # effect between cells the user asked for.
        ("BOX", (0, 0), (-1, -1), 2.0, white),
        ("INNERGRID", (0, 0), (-1, -1), 2.0, white),
    ]
    if has_multiple_lines:
        # Subtle background on the grand-total row
        style_cmds.append(("BACKGROUND", (0, -1), (-1, -1), HexColor("#f5f5f5")))
        style_cmds.append(("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY))
    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    doc.build(elements)
    return buf.getvalue()
