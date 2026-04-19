"""
Craig Spec Extractor — Extracts product specs from free-text customer messages.

Architecture:
  1. LLM reads the message and extracts structured fields
  2. Fuzzy matcher maps extracted values to valid catalog entries
  3. Pydantic validates the result
  4. Pricing engine calculates the price (or escalates)

The LLM NEVER touches prices. It only extracts what the customer wants.
The pricing engine is the single source of truth for all prices.
"""

import json
import os
from typing import Optional
from difflib import get_close_matches

from pricing_data import SMALL_FORMAT, LARGE_FORMAT, BOOKLETS


# =============================================================================
# PRODUCT CATALOG — Valid values the extractor can map to
# =============================================================================

# All valid product keys with their common aliases/synonyms
PRODUCT_ALIASES = {
    # Small format
    "business_cards": [
        "business cards", "business card", "biz cards", "biz card",
        "cards", "visiting cards", "name cards",
    ],
    "flyers_a6": [
        "a6 flyers", "a6 flyer", "a6 leaflets", "a6 leaflet",
        "flyers a6", "flyer a6", "leaflets a6", "leaflet a6",
    ],
    "flyers_a5": [
        "a5 flyers", "a5 flyer", "a5 leaflets", "a5 leaflet",
        "flyers a5", "flyer a5", "leaflets a5", "leaflet a5",
        "a5 fliers", "fliers a5",
    ],
    "flyers_a4": [
        "a4 flyers", "a4 flyer", "a4 leaflets", "a4 leaflet",
        "flyers a4", "flyer a4", "leaflets a4", "leaflet a4",
        "a4 fliers", "fliers a4",
    ],
    "flyers_dl": [
        "dl flyers", "dl flyer", "dl leaflets", "dl leaflet",
        "flyers dl", "flyer dl", "leaflets dl", "leaflet dl",
        "dl fliers", "fliers dl", "1/3 a4 flyers",
    ],
    "brochures_a4": [
        "brochures", "brochure", "a4 brochures", "a4 brochure",
        "bi-fold", "bifold", "tri-fold", "trifold",
        "folded a4", "a4 folded",
    ],
    "compliment_slips": [
        "compliment slips", "compliment slip", "comp slips", "comp slip",
        "complimentary slips", "with compliments",
    ],
    "letterheads": [
        "letterheads", "letterhead", "letter heads", "letter head",
        "headed paper", "letter paper",
    ],
    "ncr_pads_a5": [
        "a5 ncr", "ncr a5", "a5 ncr pads", "ncr pads a5",
        "a5 duplicate pads", "a5 triplicate pads",
        "a5 invoice pads", "a5 receipt pads", "a5 docket pads",
    ],
    "ncr_pads_a4": [
        "a4 ncr", "ncr a4", "a4 ncr pads", "ncr pads a4",
        "a4 duplicate pads", "a4 triplicate pads",
        "a4 invoice pads", "a4 receipt pads", "a4 docket pads",
    ],
    # Large format
    "roller_banners": [
        "roller banner", "roller banners", "pull up banner", "pull up banners",
        "pull-up banner", "pull-up banners", "rollup", "rollups",
        "roll up banner", "roll up banners", "retractable banner",
        "popup banner", "pop up banner", "pop-up banner",
    ],
    "foamex_boards": [
        "foamex", "foamex board", "foamex boards", "foam board",
        "foam boards", "pvc board", "pvc boards", "foamex sign",
    ],
    "dibond_boards": [
        "dibond", "dibond board", "dibond boards", "aluminium board",
        "aluminum board", "aluminium composite", "alu board",
        "composite board", "composite boards",
    ],
    "corri_boards": [
        "corri board", "corri boards", "corriboard", "correx",
        "correx board", "correx boards", "fluted board",
        "coroplast", "site board", "site boards",
    ],
    "pvc_banners": [
        "pvc banner", "pvc banners", "vinyl banner", "vinyl banners",
        "outdoor banner", "outdoor banners", "banner",
    ],
    "canvas_prints": [
        "canvas", "canvas print", "canvas prints",
        "canvas wrap", "gallery wrap",
    ],
    "window_graphics": [
        "window graphics", "window graphic", "window vinyl",
        "window sticker", "window stickers", "frosted vinyl",
        "frosted glass", "window film", "window decal",
    ],
    "floor_graphics": [
        "floor graphic", "floor graphics", "floor sticker",
        "floor stickers", "floor decal", "floor vinyl",
    ],
    "mesh_banners": [
        "mesh banner", "mesh banners", "mesh vinyl",
        "scaffolding banner", "wind banner",
    ],
    "fabric_displays": [
        "fabric display", "fabric displays", "fabric banner",
        "fabric backdrop", "textile banner", "tension fabric",
    ],
    "vehicle_magnetics": [
        "vehicle magnetics", "vehicle magnetic", "car magnets",
        "car magnet", "van magnets", "van magnet", "magnetic sign",
        "magnetic signs", "vehicle magnet",
    ],
    "vinyl_labels": [
        "vinyl labels", "vinyl label", "vinyl stickers",
        "vinyl sticker", "labels", "stickers",
    ],
}

# Flatten for reverse lookup: alias -> product_key
_ALIAS_TO_KEY = {}
for key, aliases in PRODUCT_ALIASES.items():
    for alias in aliases:
        _ALIAS_TO_KEY[alias.lower()] = key


# Valid finishes with aliases
FINISH_ALIASES = {
    "gloss": ["gloss", "glossy", "shiny"],
    "matte": ["matte", "matt", "mat", "satin"],
    "soft_touch": ["soft touch", "soft-touch", "softouch", "velvet", "velvet touch", "soft feel"],
    "uncoated": ["uncoated", "plain", "bond", "natural"],
    "duplicate": ["duplicate", "dup", "2-part", "2 part", "two part"],
    "triplicate": ["triplicate", "trip", "3-part", "3 part", "three part"],
}

_FINISH_TO_KEY = {}
for key, aliases in FINISH_ALIASES.items():
    for alias in aliases:
        _FINISH_TO_KEY[alias.lower()] = key


# Double-sided aliases
DOUBLE_SIDED_SIGNALS = [
    "double sided", "double-sided", "both sides", "two sides",
    "2 sided", "2-sided", "dbl sided", "dbl-sided",
    "front and back", "front & back", "printed both sides",
    "ds", "d/s",
]

SINGLE_SIDED_SIGNALS = [
    "single sided", "single-sided", "one side", "1 sided",
    "1-sided", "one sided", "front only", "ss", "s/s",
]


# Booklet-specific aliases
BINDING_ALIASES = {
    "saddle_stitch": ["saddle stitch", "saddle stitched", "stapled", "stitched"],
    "perfect_bound": ["perfect bound", "perfect binding", "glued", "glue bound", "paperback"],
}

COVER_TYPE_ALIASES = {
    "self_cover": ["self cover", "self-cover", "same cover", "no separate cover"],
    "card_cover": ["card cover", "card-cover", "thick cover", "heavy cover", "300gsm cover"],
    "card_cover_lam": [
        "card cover lam", "card cover laminated", "laminated cover",
        "lam cover", "cover with lam", "card cover + lam",
        "matt lam cover", "gloss lam cover", "laminated",
    ],
}


# =============================================================================
# FUZZY MATCHING
# =============================================================================


def match_product(text: str) -> Optional[str]:
    """Match free text to a product key. Returns None if no confident match."""
    text_lower = text.lower().strip()

    # 1. Exact alias match
    if text_lower in _ALIAS_TO_KEY:
        return _ALIAS_TO_KEY[text_lower]

    # 2. Check if any alias is contained in the text
    best_match = None
    best_length = 0
    for alias, key in _ALIAS_TO_KEY.items():
        if alias in text_lower and len(alias) > best_length:
            best_match = key
            best_length = len(alias)

    if best_match:
        return best_match

    # 3. Fuzzy match against all aliases
    all_aliases = list(_ALIAS_TO_KEY.keys())
    close = get_close_matches(text_lower, all_aliases, n=1, cutoff=0.7)
    if close:
        return _ALIAS_TO_KEY[close[0]]

    return None


def match_finish(text: str) -> Optional[str]:
    """Match free text to a finish key."""
    text_lower = text.lower().strip()

    if text_lower in _FINISH_TO_KEY:
        return _FINISH_TO_KEY[text_lower]

    for alias, key in _FINISH_TO_KEY.items():
        if alias in text_lower:
            return key

    close = get_close_matches(text_lower, list(_FINISH_TO_KEY.keys()), n=1, cutoff=0.7)
    if close:
        return _FINISH_TO_KEY[close[0]]

    return None


def detect_double_sided(text: str) -> Optional[bool]:
    """Detect if text mentions double or single sided."""
    text_lower = text.lower()
    for signal in DOUBLE_SIDED_SIGNALS:
        if signal in text_lower:
            return True
    for signal in SINGLE_SIDED_SIGNALS:
        if signal in text_lower:
            return False
    return None  # Not mentioned — Craig should ask


def match_binding(text: str) -> Optional[str]:
    """Match binding type from text."""
    text_lower = text.lower()
    for key, aliases in BINDING_ALIASES.items():
        for alias in aliases:
            if alias in text_lower:
                return key
    return None


def match_cover_type(text: str) -> Optional[str]:
    """Match cover type from text."""
    text_lower = text.lower()
    # Check longest aliases first to avoid partial matches
    sorted_items = []
    for key, aliases in COVER_TYPE_ALIASES.items():
        for alias in aliases:
            sorted_items.append((alias, key))
    sorted_items.sort(key=lambda x: -len(x[0]))

    for alias, key in sorted_items:
        if alias in text_lower:
            return key
    return None


def extract_quantity(text: str) -> Optional[int]:
    """Extract quantity from text. Handles numbers and common word forms."""
    import re

    text_lower = text.lower()

    # Step 1: Try digit patterns FIRST (most reliable, no ambiguity)

    # Pattern: "x500", "x 500", "×500"
    match = re.search(r'[x×]\s*(\d[\d,]*)', text_lower)
    if match:
        return int(match.group(1).replace(",", ""))

    # Pattern: "500 x", "500x"
    match = re.search(r'(\d[\d,]*)\s*[x×]', text_lower)
    if match:
        return int(match.group(1).replace(",", ""))

    # Pattern: any standalone number
    numbers = re.findall(r'\b(\d[\d,]*)\b', text_lower)
    if numbers:
        candidates = []
        for n in numbers:
            val = int(n.replace(",", ""))
            if 1 <= val <= 100000:  # Reasonable quantity range
                candidates.append(val)
        if candidates:
            return candidates[0]

    # Step 2: Word-to-number ONLY with word boundaries (avoids "done" → "one")
    word_numbers = {
        "twenty five hundred": 2500, "two thousand five hundred": 2500,
        "fifteen hundred": 1500, "two thousand": 2000,
        "one thousand": 1000, "two hundred and fifty": 250,
        "five hundred": 500, "three hundred": 300, "two hundred": 200,
        "two fifty": 250, "thousand": 1000, "hundred": 100,
        "twenty five": 25, "twenty": 20, "thirty": 30, "fifty": 50,
        "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "two": 2, "one": 1,
    }

    for word, num in word_numbers.items():
        # Use regex word boundaries to avoid "done" matching "one"
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            return num

    return None


def extract_pages(text: str) -> Optional[int]:
    """Extract page count from text (for booklets)."""
    import re
    text_lower = text.lower()

    # "24pp", "24 pp", "24 pages", "24 page"
    match = re.search(r'(\d+)\s*(?:pp|pages?)\b', text_lower)
    if match:
        return int(match.group(1))

    return None


def detect_artwork_needed(text: str) -> bool:
    """Detect if customer needs design/artwork."""
    text_lower = text.lower()
    signals = [
        "need design", "need artwork", "no artwork", "don't have artwork",
        "dont have artwork", "no design", "need a design",
        "can you design", "design work", "need it designed",
        "no print ready", "not print ready",
    ]
    for signal in signals:
        if signal in text_lower:
            return True
    return False


def detect_poa_item(text: str) -> Optional[str]:
    """Detect if the request contains a POA item that must be escalated."""
    text_lower = text.lower()
    poa_checks = {
        "z-fold": ["z-fold", "z fold", "zfold", "z-folded"],
        "die-cut": ["die cut", "die-cut", "diecut", "custom cut label", "custom shape"],
        "installation": ["installation", "install", "fitting", "mounted", "put up"],
        "rush job": ["rush", "urgent", "asap", "tomorrow", "next day", "same day", "express"],
    }
    for item, signals in poa_checks.items():
        for signal in signals:
            if signal in text_lower:
                return item
    return None


# =============================================================================
# MAIN EXTRACTION FUNCTION
# =============================================================================


def extract_specs_from_text(message: str) -> dict:
    """
    Extract product specs from a free-text customer message.

    Returns a dict with:
    - extracted: the raw extracted values
    - mapped: values mapped to valid catalog keys
    - missing: list of fields that couldn't be determined (Craig should ask)
    - poa_detected: if a POA item was found (escalate immediately)
    - confidence: "high" | "medium" | "low"
    - request_payload: ready-to-send payload for the pricing endpoint (if enough info)
    """

    result = {
        "original_message": message,
        "extracted": {},
        "mapped": {},
        "missing": [],
        "poa_detected": None,
        "confidence": "low",
        "request_payload": None,
        "endpoint": None,
        "clarification_needed": [],
    }

    # Check for POA items first
    poa = detect_poa_item(message)
    if poa:
        result["poa_detected"] = poa
        result["confidence"] = "high"
        return result

    # Extract product
    product_key = match_product(message)
    if product_key:
        result["extracted"]["product"] = message  # raw
        result["mapped"]["product"] = product_key
    else:
        result["missing"].append("product")
        result["clarification_needed"].append(
            "What product are you looking for? (e.g., business cards, flyers, banners, booklets)"
        )

    # Extract quantity
    qty = extract_quantity(message)
    if qty:
        result["extracted"]["quantity"] = qty
        result["mapped"]["quantity"] = qty
    else:
        result["missing"].append("quantity")
        result["clarification_needed"].append("How many do you need?")

    # Detect double-sided
    ds = detect_double_sided(message)
    if ds is not None:
        result["extracted"]["double_sided"] = ds
        result["mapped"]["double_sided"] = ds

    # Extract finish
    finish = match_finish(message)
    if finish:
        result["extracted"]["finish"] = finish
        result["mapped"]["finish"] = finish

    # Artwork
    if detect_artwork_needed(message):
        result["extracted"]["needs_artwork"] = True
        result["mapped"]["needs_artwork"] = True

    # Determine category and build payload
    if product_key:
        category = _get_category(product_key)
        result["mapped"]["category"] = category

        if category == "small_format":
            result["endpoint"] = "/quote/small-format"
            # Check if we have enough to quote
            if qty:
                payload = {
                    "product": product_key,
                    "quantity": qty,
                }
                if ds is not None:
                    payload["double_sided"] = ds
                else:
                    # For products where it matters, ask
                    product_data = SMALL_FORMAT.get(product_key, {})
                    if product_data.get("double_sided_surcharge", False):
                        result["missing"].append("double_sided")
                        result["clarification_needed"].append(
                            "Single-sided or double-sided?"
                        )

                if finish:
                    payload["finish"] = finish
                else:
                    result["missing"].append("finish")
                    product_data = SMALL_FORMAT.get(product_key, {})
                    available = product_data.get("finishes", [])
                    result["clarification_needed"].append(
                        f"What finish? Available: {', '.join(available)}"
                    )

                if detect_artwork_needed(message):
                    payload["needs_artwork"] = True

                result["request_payload"] = payload

        elif category == "large_format":
            result["endpoint"] = "/quote/large-format"
            if qty:
                payload = {
                    "product": product_key,
                    "quantity": qty,
                }
                if detect_artwork_needed(message):
                    payload["needs_artwork"] = True
                result["request_payload"] = payload

        elif category == "booklet":
            result["endpoint"] = "/quote/booklet"
            # Booklets need more fields
            pages = extract_pages(message)
            binding = match_binding(message)
            cover = match_cover_type(message)
            fmt = _detect_booklet_format(message)

            if pages:
                result["extracted"]["pages"] = pages
                result["mapped"]["pages"] = pages
            else:
                result["missing"].append("pages")
                result["clarification_needed"].append("How many pages?")

            if binding:
                result["extracted"]["binding"] = binding
                result["mapped"]["binding"] = binding
            else:
                result["missing"].append("binding")
                result["clarification_needed"].append(
                    "Saddle stitched (stapled) or perfect bound (glued spine)?"
                )

            if cover:
                result["extracted"]["cover_type"] = cover
                result["mapped"]["cover_type"] = cover
            else:
                result["missing"].append("cover_type")
                result["clarification_needed"].append(
                    "Cover type: self cover (same paper throughout), card cover (thicker cover), or card cover with lamination?"
                )

            if fmt:
                result["extracted"]["format"] = fmt
                result["mapped"]["format"] = fmt
            else:
                result["missing"].append("format")
                result["clarification_needed"].append("A5 or A4 booklet?")

            if qty and pages and binding and cover and fmt:
                result["request_payload"] = {
                    "format": fmt,
                    "binding": binding,
                    "pages": pages,
                    "cover_type": cover,
                    "quantity": qty,
                }

    # Calculate confidence
    if result["poa_detected"]:
        result["confidence"] = "high"
    elif result["request_payload"] and not result["missing"]:
        result["confidence"] = "high"
    elif result["request_payload"] and len(result["missing"]) <= 1:
        result["confidence"] = "medium"
    else:
        result["confidence"] = "low"

    return result


def _get_category(product_key: str) -> str:
    """Determine which category a product belongs to."""
    if product_key in SMALL_FORMAT:
        return "small_format"
    elif product_key in LARGE_FORMAT:
        return "large_format"
    else:
        # Check if the product key hints at booklet
        booklet_signals = ["booklet", "brochure_booklet"]
        return "booklet" if product_key in booklet_signals else "unknown"


def _detect_booklet_format(text: str) -> Optional[str]:
    """Detect A5 or A4 format from text."""
    text_lower = text.lower()
    if "a5" in text_lower:
        return "a5"
    elif "a4" in text_lower:
        return "a4"
    return None


# =============================================================================
# BOOKLET DETECTION — needs special handling since booklets aren't in PRODUCT_ALIASES
# =============================================================================

# Add booklet detection to match_product
_BOOKLET_SIGNALS = [
    "booklet", "booklets", "catalogue", "catalog",
    "magazine", "manual", "handbook", "program", "programme",
    "brochure booklet", "book", "saddle stitch", "perfect bound",
]


# Patch match_product to handle booklets
_original_match_product = match_product

def match_product(text: str) -> Optional[str]:
    """Extended match_product that also detects booklets."""
    # Check booklet signals first
    text_lower = text.lower()
    for signal in _BOOKLET_SIGNALS:
        if signal in text_lower:
            # Check if it also has pages mentioned — strong booklet signal
            if extract_pages(text) or "pp" in text_lower:
                return "booklet"
            # Still might be a booklet
            return "booklet"

    # Fall back to original matching
    return _original_match_product(text)


# Override _get_category to handle "booklet" key
_original_get_category = _get_category

def _get_category(product_key: str) -> str:
    if product_key == "booklet":
        return "booklet"
    return _original_get_category(product_key)
