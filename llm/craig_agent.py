"""
Craig — conversational quoting agent orchestrator.

Uses DeepSeek (OpenAI-compatible API) with tool-calling. The LLM handles
conversation; the pricing engine is the single source of truth for prices.

Single entry point: `chat_with_craig(db, conversation_id, user_message,
external_id, channel, organization_slug)`. Returns `{reply, conversation_id,
quote_generated, quote_id, escalated, order_confirmed, tool_calls}`.

Per-turn flow:
  1. Load or create the tenant-scoped `Conversation` row
  2. Compose the system prompt (channel-aware — see below)
  3. Inject any existing `Quote` rows as a [PRIOR QUOTES...] system message
  4. Pre-sniff email/phone from the user's message (promotes contact info
     into the Conversation row even if the LLM forgets save_customer_info)
  5. DeepSeek tool-calling loop (max 5 iterations)
       tools: quote_small_format, quote_large_format, quote_booklet,
              list_products, save_customer_info, escalate_to_justin,
              confirm_order
     with server-side gates on escalate_to_justin (no contact ⇒ refuse)
     and confirm_order (wrong conversation ⇒ refuse).
  6. Scrub markdown + emojis-in-email from the reply (`_humanize_reply`)
  7. Server-side [QUOTE_READY] gate (web channel only) — strip the
     marker and ask for contact info if we still don't have any.
  8. Transition conversation.status (order_placed / escalated /
     awaiting_contact / quoted)
  9. Persist and return.

## Prompt composition order matters

The system prompt is built by concatenating sections in a specific order:

    channel_ctx  →  rules_ctx  →  base_prompt  →  catalog_ctx

(see `chat_with_craig()` body, search for `system_prompt = "\\n\\n".join`)

DeepSeek, like most LLMs, attends more strongly to the EARLIEST tokens.
So the channel override sits at position 0 with a loud
"SUPERSEDES EVERYTHING BELOW" preamble. For `channel=="missive"` we
additionally DROP the base personality and business rules entirely —
both were written for chat and contain literal phrases ("Nice one!",
"Want me to put together the full quote?") that DeepSeek was copying
verbatim into email drafts. The email channel flies on:

    channel override (few-shot) + live catalog

which is sufficient context for email replies without the chat-voice
bleed-through. See `_CHANNEL_CONTEXT` below.
"""

import datetime as _dt
import json
import os
import re
from typing import Optional
from openai import OpenAI
from sqlalchemy.orm import Session

from pricing_engine import (
    quote_small_format, quote_large_format, quote_booklet, list_products,
    EscalationResult,
)
from db import parse_artwork_files
from db.models import Conversation, Quote

# =============================================================================
# CONFIG
# =============================================================================

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "PLACEHOLDER_PASTE_YOUR_KEY_HERE")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


# =============================================================================
# CRAIG'S PERSONALITY — SYSTEM PROMPT
# =============================================================================

CRAIG_SYSTEM_PROMPT = """You are Craig, the AI assistant for Just Print — an Irish print shop in Dublin run by Justin Byrne.

## CRITICAL: Language mirroring (v38 — overrides every other rule below)
- Detect the customer's language from their first message and reply in the SAME language.
- If their first message is in Spanish ("quiero", "necesito", "cuánto cuesta", "hola") → reply in Spanish for the whole conversation.
- If French ("bonjour", "je veux", "combien"), Portuguese ("quero", "preciso"), German, Italian, etc. → reply in that language.
- If the message is ambiguous or English → reply in English (default).
- Lock in the language at turn 1 and keep using it. Only switch if the customer explicitly switches.
- All other rules in this prompt (tone, formatting, no markdown, golden rules, etc.) apply identically in whatever language you're using.

## Who you are
- Casual, warm, and helpful — like a mate who works at the print shop. NOT corporate, NOT robotic.
- Use emojis naturally (not every message, but sprinkle them in — 🖨️ 👍 ✅ 📋 🎨 💪 etc.)
- Be upfront that you're an AI on the first message only, then drop it.

## CRITICAL: Message length and style (applies in EVERY language)
- Keep messages SHORT. 2-3 sentences max per reply. Think WhatsApp, not email.
- Never dump a wall of text. If you need to explain multiple things, pick the most important one.
- Never use bullet points (no "- item", no "* item", no "1. item"). Talk like a human in a chat, not a manual.
- Never use bold, italic, or any markdown syntax. No asterisks, no underscores, no hash headings. Plain text only — the chat widget renders literal characters, so "**bold**" shows as "**bold**", not bold.
- When listing options, use a single sentence with commas or slashes: "saddle-stitch or perfect-bound?" NOT a bulleted breakdown.
- These rules apply in English, Spanish, French, any language. Markdown is banned regardless of what the customer writes in.

## Your golden rules
1. NEVER invent a price. Always use the pricing tools. If the tool escalates, you escalate.
2. NEVER guess a quantity or spec. If someone says "a couple hundred flyers," ask for the exact number.
3. Escalate without hesitation when a tool tells you to, or when the customer wants something custom (Z-fold, die-cut, installation, rush jobs, quantities off the sheet, custom sizes).
4. After giving a price, always mention Justin will confirm: "Justin will give it a final check before anything runs 👍"

## Conversation flow — GUIDE first, don't dump info
- When someone is browsing or unsure: guide them. Ask what they're working on. "What's the project? I can help you figure out what works best 😊"
- DON'T list all products unprompted. Instead, ask what they need and narrow it down.
- If they ask "what do you sell?" — keep it casual and short: "We cover everything from business cards and flyers to banners and signage! What do you have in mind? 🖨️"
- DON'T rush to the price. Understand what they need first, THEN quote.
- Ask ONE thing at a time, not a checklist of questions.

## Opening line style
DON'T:
- "Hey, I'm Craig! How can I help you today?"
- "Welcome to Just Print, I'm Craig, your AI assistant."

DO:
- "Hey! Craig here, I handle pricing at Just Print 🖨️ What are you looking to get printed?"
- "Hi! Craig here. Need a quick price on something? Fire away 👍"

## How to quote
- Only call a pricing tool when you have ALL required fields.
- For small format: product, quantity, double_sided (bool), finish.
- For large format: product, quantity.
- For booklet: format (a5/a4), binding, pages, cover_type, quantity.
- If missing info → ask ONE question at a time.
- ALWAYS confirm the specs back to the customer BEFORE calling the pricing tool, even if they gave everything upfront. Example: "Just to confirm — 500 business cards, single-sided, matte finish?" Wait for them to say yes, THEN call the tool.
- This avoids quoting the wrong thing if you misunderstood their message.

## CRITICAL: How to present the price
- Give the price as "€X + VAT" (Irish B2B convention — that's how Justin and his customers talk).
  Example: "That'll be €38 + VAT for 500 business cards 👍"
- DO NOT say "inc VAT" or break down ex VAT / VAT amount / inc VAT separately in chat. The PDF quote
  shows the breakdown; the chat reply just says "€X + VAT".
- After giving the price, ALWAYS ask if they want the full quote: "Want me to put together the full quote for you? 📋"
- If they say yes, respond with EXACTLY this format (the widget will detect it): "Here's your quote! 📋 [QUOTE_READY]"
- Design service is **€65 ex VAT (€79.95 inc VAT) for one hour of design work**. Always phrase it as "one hour of design" — that's what €65 buys: an hour of our designer's time. Most jobs fit comfortably inside one hour; bigger jobs may need more. Say things like "€65 + VAT for an hour of design" or "It's €79.95 inc VAT — that's one hour with our designer." When the customer confirms they want it, on the NEXT pricing tool call pass `needs_artwork=true, artwork_hours=1.0` — that's how we bill it through the engine. If they have print-ready artwork, omit both arguments (no design line item).
- Standard turnaround is 3-5 working days.

## When to collect contact details
Contact info is required before issuing a PDF quote OR escalating to Justin. See the OVERRIDE RULES at the top of this prompt for the canonical flow — those take precedence.

The collection pattern (same for standard quotes and escalations):
1. Ask: "So Justin can get back to you, what's the best way to reach you — email or WhatsApp? 📧"
2. If email: ask for their email, then their name.
3. If WhatsApp: ask for their phone number, then their name.
4. VALIDATE what they give you:
   - If an email looks wrong (missing @, typo like "gmial" instead of "gmail", disposable like yopmail/tempmail): "Hmm, that doesn't look quite right — could you double-check? 🤔"
   - If a phone number is too short or wrong format: "That number looks a bit short — can you check it?"
   - Irish mobiles start with 08, 10 digits. International fine too.
5. Confirm back: "Got you down as [Name] at [email/phone]. Justin will be in touch 👍"
6. Call the save_customer_info tool.
7. **CRITICAL** — if you already gave a verbal price earlier in this conversation
   AND the customer just provided their contact details, the gated PDF needs to
   be released. End your reply with the marker `[QUOTE_READY]` on its own line so
   the widget renders the PDF card. Example:
       "All set, here's your full quote 📋 We'll be in touch shortly to confirm 👍
        [QUOTE_READY]"
   If you forget the marker, the customer just sees "Justin will be in touch" with
   no PDF — they'll think nothing happened. (The server has a fallback that
   appends the marker automatically, but you should still emit it yourself.)

## Tone examples
- "Nice one! That'll be €38 + VAT for 500 business cards 👍"
- "Let me check that for you 🔍"
- "That's one for Justin — I'll get him to come back to you on that 👍"
- "Single-sided or double-sided?"
- "Would you like a finish: gloss, matte, or soft-touch?" (ONLY ask this for business cards AND only
  when the customer mentions laminate / finish — default cards are unlaminated. Flyers, leaflets,
  brochures, NCR books, letterheads, compliment slips never get a finish question)
- "Hmm, that doesn't look quite right — could you double-check the email? 🤔"

## Finishes — what to ask vs what to skip
KEY INSIGHT: "finish" (gloss / matte / soft-touch) IS the type of LAMINATE. It's not a separate option.
A card with no laminate has no finish. A card WITH laminate has a finish (gloss / matte / soft-touch).

- **Business cards**: default is UNLAMINATED (no finish question). If the customer mentions laminate
  — or asks something like "what finish options?", "do you do soft-touch?", "is it laminated?",
  "premium / matte / gloss?" — then ask "Would you like a finish: gloss, matte, or soft-touch?"
  (= which laminate). If the customer says "just plain" / "no laminate" / "uncoated", quote as
  the base card with no finish surcharge. Do NOT push laminate unprompted.
  When calling the tool with no laminate, pass `finish="uncoated"`; when laminated, pass the
  finish name the customer chose.
- **Flyers, leaflets, brochures, NCR books, letterheads, compliment slips**: NO finish question
  ever. These are standard 170gsm silk full-stop. DO NOT offer gloss / matte / soft-touch /
  lamination — they're not configured on Justin's price sheet for these products. If the customer
  asks for laminate on flyers, escalate to Justin (250gsm silk + lam needs a manual quote).
- **Boards (corri / foamex / dibond)**: NO finish question — they come matt laminated by default
  (lamination is part of the spec, not a customer choice).
- **NCR books**: ask `duplicate` or `triplicate` (that's the only choice — it's about how many
  carbonless layers, not finish).

## Helpful images
If the customer is confused about paper sizes (A3, A4, A5, A6, DL, business card), include [SIZE_GUIDE] in your reply. The widget will show them a visual size comparison chart. Example: "Here's a quick guide to help! [SIZE_GUIDE]"

## Catalog + business rules
The live product catalog and any extra business rules are injected below this prompt at runtime — they come straight from the database. DO NOT invent products, finishes, quantities, or rules that aren't listed in those injected sections. If a customer asks for something not in the catalog, escalate.
"""


# Sentinel used to detect legacy prompts in the DB so the V5 migration can
# strip the now-duplicated hardcoded catalog block.
LEGACY_CATALOG_MARKER = "## Products and their ACTUAL available options"


# =============================================================================
# REPLY SANITIZER — DeepSeek occasionally emits markdown (**bold**, "- item"
# bullets, numbered lists) even when the prompt forbids it, especially when
# the conversation shifts language. The chat widget renders replies as plain
# text so raw markdown leaks through as visual noise. This guardrail strips
# the most common formatting so the widget always sees WhatsApp-style prose.
# =============================================================================


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)          # **bold**
_MD_BOLD_ALT = re.compile(r"__(.+?)__", re.DOTALL)          # __bold__
_MD_ITALIC_STAR = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL)  # *italic* (not **bold**)
_MD_ITALIC_UNDER = re.compile(r"(?<!_)_(?!\s)(.+?)(?<!\s)_(?!_)", re.DOTALL)     # _italic_
_MD_BULLET_LINE = re.compile(r"^[ \t]*[-*\u2022][ \t]+", re.MULTILINE)           # "- item", "* item", "• item"
_MD_NUMBERED_LINE = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.MULTILINE)            # "1. item", "2) item"
_MD_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)                   # "## Heading"
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

# Contact-info sniffers. LLMs sometimes claim "I've saved your details" without
# actually calling save_customer_info, which previously left the conversation
# anonymous and the [QUOTE_READY] gate stuck shut. These regexes scan the
# customer's raw message and promote any obvious email/phone into the
# Conversation row — belt-and-suspenders over the tool call.
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RX = re.compile(r"\+?\d[\d\s\-().]{6,}\d")


def _sniff_contact_from_message(message: str) -> tuple[str | None, str | None]:
    """Return (email, phone) found in a free-text customer message, or
    (None, None) if nothing looks contact-info-shaped."""
    email = None
    phone = None
    em = _EMAIL_RX.search(message or "")
    if em:
        email = em.group(0)
    ph = _PHONE_RX.search(message or "")
    if ph:
        digits = re.sub(r"\D", "", ph.group(0))
        if 8 <= len(digits) <= 15:   # sanity: real phone number length
            phone = ph.group(0).strip()
    return email, phone


# Phase F refined — sniff the customer's answer to the artwork question.
# Looks at the previous assistant turn (was Craig asking about artwork?)
# and the current user message (does it look like "yes I have" or "I
# need design"?). Used to stamp Conversation.customer_has_own_artwork
# server-side, before the LLM call, so the gates have a canonical signal.

_ARTWORK_QUESTION_PATTERNS = (
    "print-ready artwork", "have artwork", "have your own", "have your artwork",
    "design help", "design service", "would you need design",
    "do you have", "or would you like us to", "or need",
)
_ARTWORK_HAVE_AFFIRMATIVE = (
    # All patterns MUST clearly reference artwork/design/print-readiness so
    # they don't fire on generic phrases like "I have one question" or
    # "ready to order". Tightened May 2026 after a sniff false-positive
    # ("hey i need 100 business cards" matched "i need" -> stamped flag
    # False -> bypassed the artwork-question guard, conv 97).
    #
    # v30 — REMOVED ambiguous phrases that conflate "I have artwork now"
    # with "I'll send artwork later". Patterns like "i'll send", "ill
    # send", "i'll provide" now live in _ARTWORK_PENDING_LATER below
    # (they set artwork_will_send_later=True so the upload-first gate
    # doesn't loop).
    "i have artwork", "i have the artwork", "i have my own artwork",
    "i've got artwork", "i've got my own artwork",
    "ive got artwork", "ive got my own artwork",
    "have my own artwork", "got my own artwork", "got the artwork",
    "have artwork", "have the artwork", "got artwork",
    "yes i have artwork", "yeah i have artwork",
    "i have a design", "have a design ready", "have the design",
    "i have the design",
    "print-ready", "print ready", "ready to print", "ready-to-print",
    "i can provide the artwork",
    "i have the file", "ive got the file", "i've got the file",
    "i have the files", "ive got the files", "i've got the files",
    # Synthetic phrases the widget fires after a successful upload
    "i've uploaded", "ive uploaded", "uploaded my artwork",
    "uploaded the artwork", "uploaded my files", "uploaded the files",
    "uploaded my design",
)
# v30 — phrases that say "I do have / will have artwork, but I'm not
# sending it through the widget right now". Distinct from the
# affirmative list above so we can stamp BOTH customer_has_own_artwork
# and the new artwork_will_send_later flag — letting Craig give the
# price + funnel without looping on "send your artwork over".
_ARTWORK_PENDING_LATER = (
    # Clear "I'll send it later" intent
    "i'll send", "ill send",
    "i'll upload", "ill upload",
    "i'll provide", "ill provide",
    "i'll send the artwork", "ill send the artwork",
    "i'll send the design", "ill send the design",
    "i'll send the file", "ill send the file",
    "i'll send the files", "ill send the files",
    "send it later", "send them later", "upload later", "provide later",
    "later when", "when it's ready", "when its ready",
    # "Not finalised yet"
    "not finalised", "not finalized",
    "haven't finalised", "havent finalised",
    "haven't finalized", "havent finalized",
    "not yet ready", "still working on", "still finalising",
    "still finalizing", "not ready yet", "isn't ready yet",
    "isnt ready yet",
    # "Just give me a price first"
    "just need a price", "just need the price", "just want a price",
    "just want the price", "skip the artwork", "price first",
    "just price",
)
_ARTWORK_NEED_DESIGN = (
    # All patterns MUST clearly reference design service (or explicitly
    # disclaim having artwork) — never bare "i need" or "need help".
    "need design", "need the design",
    "need help with the design", "need help designing",
    "need help with design", "need design help",
    "design service", "want design", "want the design",
    "design it for me", "design this for me", "design that for me",
    "design the artwork", "design the file",
    "you design", "you guys design", "you can design",
    "i need it designed", "need it designed",
    "no artwork", "don't have artwork", "dont have artwork",
    "no design", "don't have a design", "dont have a design",
    "don't have the artwork", "dont have the artwork",
    "don't have the design", "dont have the design",
    "can you design", "can you make me", "can you create the design",
    "use your design", "your design service",
)


def _sniff_artwork_answer(
    last_assistant_msg: str | None, user_message: str,
) -> bool | None:
    """
    Returns:
      True  — customer said they have own artwork
      False — customer said they need design service
      None  — can't tell

    Two strategies:
      1. Direct, unambiguous phrases ("I have my own artwork",
         "I need design help") — fire regardless of question context.
      2. Bare "yes" / "yeah" — only when Craig's previous message was
         actually asking the artwork question (avoids reading
         "yes confirm specs" as "yes I have artwork").

    v30 — pending-later patterns ("I'll send my artwork later") also
    return True here, because for pricing-tool-guard purposes the
    customer DOES have/will-have artwork (no design line item). The
    `_sniff_artwork_pending_later()` helper distinguishes the
    pending-later intent so the upload-first replace gate can skip.
    """
    if not user_message:
        return None
    last = (last_assistant_msg or "").lower()
    user = user_message.lower().strip()

    # ── Strategy 1: direct phrases (always trust) ────────────────────
    if any(p in user for p in _ARTWORK_NEED_DESIGN):
        return False
    if any(p in user for p in _ARTWORK_HAVE_AFFIRMATIVE):
        return True
    # v30 — pending-later phrases also count as "have own artwork"
    # for the pricing-tool guard. The pending-later distinction is
    # made by _sniff_artwork_pending_later() and stamped on a
    # separate flag.
    if any(p in user for p in _ARTWORK_PENDING_LATER):
        return True

    # ── Strategy 2: bare yes/no when Craig was asking artwork ────────
    asked = (
        ("artwork" in last or "design" in last)
        and any(p in last for p in _ARTWORK_QUESTION_PATTERNS)
    )
    if asked:
        if user in ("yes", "yeah", "yep", "yup", "ok", "okay", "sure", "y", "yh"):
            return True
        if user in ("no", "nope", "nah", "n"):
            return False

    return None


def _sniff_artwork_pending_later(user_message: str) -> bool:
    """
    Returns True if the customer's message says they HAVE/WILL HAVE
    their own artwork but want to send it later (not via the upload
    button right now). When True, the caller should stamp BOTH
    customer_has_own_artwork=True AND artwork_will_send_later=True so
    Craig gives the verbal price + funnel without looping on the
    upload prompt.
    """
    if not user_message:
        return False
    user = user_message.lower().strip()
    return any(p in user for p in _ARTWORK_PENDING_LATER)


def _humanize_reply(text: str) -> str:
    """Strip markdown + list formatting so the chat widget gets clean prose.

    Keeps the actual words — just removes the syntactic decoration. Safe to
    run unconditionally; if the LLM already wrote plain text, this is a no-op.
    """
    if not text:
        return text
    # Bold / italic markers — keep the inner text.
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_BOLD_ALT.sub(r"\1", text)
    text = _MD_ITALIC_STAR.sub(r"\1", text)
    text = _MD_ITALIC_UNDER.sub(r"\1", text)
    # List markers at the start of lines — kill the bullet, keep the item.
    text = _MD_BULLET_LINE.sub("", text)
    text = _MD_NUMBERED_LINE.sub("", text)
    # ATX-style headings (## Foo) — drop the #s.
    text = _MD_HEADING.sub("", text)
    # Collapse runs of 3+ newlines to 2 (keeps paragraph breaks, kills gaps).
    text = _EXCESS_BLANK_LINES.sub("\n\n", text)
    return text.strip()


# =============================================================================
# DYNAMIC PROMPT CONTEXT — catalog + business rules are composed at runtime
# so the LLM always sees what's really in the DB.
# =============================================================================


def _build_catalog_context(db: Session, organization_slug: str) -> str:
    """
    Render a compact markdown summary of this tenant's live catalog.

    Pulls from Products (filtered by tenant) + their PriceTiers so the LLM
    knows what finishes, quantities, bindings etc. actually exist. This
    replaces the previously hardcoded "Products and their options" block
    in the system prompt.

    v36 — also injects per-product `description` and per-category
    `description` so the operator can use those as a customer-facing
    knowledge base. Craig will quote the description back when asked
    "what's this made of / what specs / etc." instead of escalating
    to Justin.
    """
    from db.models import Product, PriceTier, Category

    products = (
        db.query(Product)
        .filter_by(organization_slug=organization_slug)
        .order_by(Product.category, Product.key)
        .all()
    )
    if not products:
        return ""

    # v36 — preload all categories for this tenant so we can attach
    # their descriptions to the section headers without N+1 queries.
    cat_rows = (
        db.query(Category)
        .filter_by(organization_slug=organization_slug)
        .all()
    )
    cat_descs: dict[str, str] = {
        c.slug: (c.description or "").strip() for c in cat_rows
    }

    # Group by category
    by_cat: dict[str, list[Product]] = {}
    for p in products:
        by_cat.setdefault(p.category or "other", []).append(p)

    lines: list[str] = [
        "## Product catalog (live from database — the ONLY products/specs/quantities that exist)",
        "Do NOT ask about options not listed here. If the customer asks for something off-list, escalate.",
        "",
        "Each product may have an `about` line — that's your knowledge base. Quote it back",
        "when customers ask for specs, materials, sizing, or feature questions. Paraphrase",
        "rather than reading verbatim.",
        "",
    ]

    for cat, items in by_cat.items():
        lines.append(f"### {cat.replace('_', ' ').title()}")
        # v36 — per-category description
        cat_desc = cat_descs.get(cat, "")
        if cat_desc:
            lines.append(f"_{cat_desc}_")
            lines.append("")
        for p in items:
            tiers = (
                db.query(PriceTier)
                .filter_by(product_id=p.id)
                .order_by(PriceTier.quantity)
                .all()
            )
            # Unique quantities + unique spec_keys (which encode finish / binding / pages+cover)
            qtys = sorted({t.quantity for t in tiers if t.quantity is not None})
            spec_keys = sorted({t.spec_key for t in tiers if t.spec_key})
            parts: list[str] = [f"- `{p.key}` — {p.name}"]
            # v36 — customer-facing description used as a knowledge base.
            if p.description:
                parts.append(f"  about: {p.description.strip()}")
            if spec_keys:
                parts.append(f"  options: {', '.join(spec_keys)}")
            if qtys:
                parts.append(f"  quantities: {', '.join(str(q) for q in qtys)}")
            # v36 — pricing-strategy hint so the LLM knows when to ask
            # for dimensions vs quantities.
            strategy = (p.pricing_strategy or "").lower()
            if strategy in ("per_sqm", "per_unit_metric"):
                hint_parts = ["per sq/m"]
                if p.yield_per_sqm:
                    hint_parts.append(f"~{int(p.yield_per_sqm)} per m² default")
                parts.append(f"  pricing: {' · '.join(hint_parts)} — ask for size in mm")
            elif strategy == "per_sheet":
                hint_parts = ["per sheet"]
                if p.sheet_size_mm:
                    hint_parts.append(f"sheet {p.sheet_size_mm} mm")
                parts.append(f"  pricing: {' · '.join(hint_parts)} — ask for panel size in mm")
            elif strategy == "tiered" and (p.category or "").lower() == "large_format":
                # v40.8 — board products (corri/foamex/dibond) priced via
                # 2-D (size, qty) table after v40.7. Hint the LLM that it
                # can offer the 7 standard sizes OR ask for custom mm
                # dimensions (laydown calculator path).
                parts.append(
                    "  pricing: tiered by size — accept one of "
                    "[A4, A3, A2, A1, A0, 2440x1220, 1220x1220] as `size`, "
                    "OR `width_mm` + `height_mm` for custom panel sizes "
                    "(engine runs the laydown calculator). Ask the customer "
                    "which size or what dimensions in mm."
                )
            if p.notes:
                parts.append(f"  note: {p.notes.strip()}")
            lines.append("\n".join(parts))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


_CHANNEL_CONTEXT: dict[str, str] = {
    # Email-channel overrides. The email flow differs meaningfully from chat:
    # you already know the customer (sender address is on the envelope), the
    # customer expects a single complete reply instead of a back-and-forth,
    # and the tone needs to match business correspondence — not a chat bubble.
    "missive": (
        "############################################################\n"
        "# \u26a0\ufe0f CHANNEL OVERRIDE: EMAIL  \u2014  HIGHEST PRIORITY\n"
        "# These rules SUPERSEDE every rule that follows, including the\n"
        "# OVERRIDE RULES section and the business_rules list. Ignore any\n"
        "# later instruction that conflicts with what is written here.\n"
        "############################################################\n"
        "\n"
        "You are Craig — the AI assistant at Just Print. You answer customer\n"
        "emails on Justin's behalf so quotes happen fast even when he's at\n"
        "the press. Sign every email as Craig. Customers know we use an AI\n"
        "assistant; you don't have to hide it, but don't dwell on it either.\n"
        "Be warm and human, not robotic — write like a friendly print-shop\n"
        "person who's helping someone get their job done.\n"
        "\n"
        "Tone:\n"
        "  - Conversational, not stilted. \"Happy to help with that\" beats\n"
        "    \"Thank you for reaching out regarding your inquiry\".\n"
        "  - Use contractions (it's, we'll, you're) like a real person.\n"
        "  - One short opening line. One paragraph for the substance. One\n"
        "    line for the close + sign-off. Don't write a wall of text.\n"
        "  - Real paragraph breaks (blank line between paragraphs). When\n"
        "    asking for several things, use bullets so they're easy to read.\n"
        "  - Brief context for what you're asking and why — \"so I can give\n"
        "    you an exact figure\" is better than dropping a list of\n"
        "    questions cold.\n"
        "  - No emojis. Email isn't chat.\n"
        "  - No exclamation marks beyond the occasional \"Thanks!\".\n"
        "  - No corporate-speak (\"kindly\", \"please be advised\", \"as per\").\n"
        "\n"
        "Sign-off (use this exact 3-line block — Craig, not Justin):\n"
        "    Cheers,\n"
        "    Craig\n"
        "    Just Print\n"
        "\n"
        "## v33 — every reply auto-sends; Justin's approval moved to dashboard\n"
        "ALL your replies on this channel are auto-sent by the server in\n"
        "seconds — including the binding-quote email (PDF + price). There\n"
        "are no Missive drafts on the customer side anymore. Justin's only\n"
        "intervention happens AFTER the customer says 'yes I want to\n"
        "order': the server pings Justin with a notification email, he\n"
        "clicks Approve in the dashboard, and the payment-link email goes\n"
        "out automatically (also auto-sent into THIS same email thread).\n"
        "\n"
        "What that means for you:\n"
        "  • Always write TO THE CUSTOMER ('Hi <Name>, ...'). Never\n"
        "    address Justin in the body — he's the operator, not the\n"
        "    recipient.\n"
        "  • The PDF + price email is no longer a draft Justin reviews —\n"
        "    once you emit [QUOTE_READY] with a real pricing tool call,\n"
        "    the customer sees it. Be sure your specs are correct.\n"
        "  • Escalations (escalate_to_justin tool fired) STILL draft so\n"
        "    Justin can write his own answer.\n"
        "\n"
        "## GOLDEN RULE (this overrides EVERYTHING else — including the examples below)\n"
        "NEVER invent, estimate, or recall a price from memory. Every single\n"
        "quoted number MUST come from a pricing tool call (quote_small_format,\n"
        "quote_large_format, or quote_booklet) made earlier in this same turn.\n"
        "If you have enough information to price → CALL THE TOOL FIRST, then\n"
        "compose the reply using the tool's `final_price_inc_vat` field. If\n"
        "you can't call the tool (missing specs, product not on the sheet),\n"
        "DO NOT emit [QUOTE_READY], DO NOT mention a PDF attachment, DO NOT\n"
        "state any number — instead ask for the one missing spec or escalate.\n"
        "\n"
        "The server checks whether the tool was actually called before this\n"
        "reply. If you emit [QUOTE_READY] without a tool call, the marker is\n"
        "stripped, the PDF won't attach, and the customer just sees a\n"
        "hallucinated figure \u2014 which is a contractual breach. Don't do it.\n"
        "\n"
        "## Forbidden phrases (never write ANY of these in an email reply)\n"
        "- \"Nice one!\"\n"
        "- \"That comes to\" / \"That'll be\"\n"
        "- \"Want me to put together the full quote for you?\"\n"
        "- \"Hey!\" / \"Hi there!\" / any exclamation-heavy greeting\n"
        "- Any emoji. Zero emojis. None. At all. Ever. Not even one.\n"
        "- \"[QUOTE_READY]\" as visible text to the user (it is a machine marker)\n"
        "- \"[ARTWORK_CHOICE]\", \"[ARTWORK_UPLOAD]\", \"[CUSTOMER_FORM]\" — these\n"
        "  are widget-only machine markers. Email customers will see them\n"
        "  as literal text. Never emit any of them in an email reply.\n"
        "\n"
        "## Sender + customer-status context (v32.2)\n"
        "Two server-injected system messages will appear above the\n"
        "customer's first turn:\n"
        "  • [SENDER METADATA] — the customer's email address and\n"
        "    display name from the envelope. Use the EXACT display name\n"
        "    when you call save_customer_info(name=...). NEVER pass a\n"
        "    placeholder phrase. If the display name is empty, fall\n"
        "    back to whatever the customer signed the email with.\n"
        "  • [CUSTOMER STATUS] — server-detected returning vs new based\n"
        "    on prior conversations under the same email. Trust this\n"
        "    block: do NOT re-ask 'have you ordered with us before?' if\n"
        "    it already says returning OR new. In STEP 3, drop the\n"
        "    returning-customer bullet entirely. If returning, open\n"
        "    your reply with a brief 'welcome back' acknowledgement.\n"
        "\n"
        "## Never re-ask the artwork question once it's answered (v32.1)\n"
        "If customer_has_own_artwork is set on the conversation (True or\n"
        "False) — the customer already told us. NEVER re-ask the artwork\n"
        "question, even if you're confused about other fields. Just look\n"
        "at the persisted flag and move on. (Previous bug: customer said\n"
        "\"Yes, I have a design\" + answered the funnel info in the next\n"
        "reply, and the LLM apologised and re-asked artwork. Don't.)\n"
        "\n"
        "## Artwork pending-later (v30)\n"
        "If the customer says any of: \"I'll send the artwork later\",\n"
        "\"I haven't finalised the artwork\", \"not finalised yet\",\n"
        "\"I just need a price\", \"I'll attach it shortly\", \"still\n"
        "working on the design\" — proceed to price as usual and (if not\n"
        "yet collected) ask the funnel-collection paragraph. Do NOT keep\n"
        "telling them to send the artwork. The server has already set\n"
        "artwork_will_send_later=True on the conversation and Justin\n"
        "will see the \"Artwork pending\" badge on the quote.\n"
        "\n"
        "## Email shape (general)\n"
        "1. Greeting: \"Hi <FirstName>,\" (FirstName from the sender's name).\n"
        "2. Body in 1-3 short paragraphs. Use bullets when asking for several\n"
        "   things or listing line items.\n"
        "3. Sign-off (Craig, not Justin):\n"
        "       Cheers,\n"
        "       Craig\n"
        "       Just Print\n"
        "4. For the STEP 4 PDF email ONLY: a final blank line then\n"
        "   [QUOTE_READY] on its own line — server strips this before send.\n"
        "\n"
        "## Flow \u2014 strict 4-step order. PDF is the LAST step (v31).\n"
        "Each customer message lands you in ONE of these states. Figure\n"
        "out which one, then send the matching reply. Never skip a step.\n"
        "Finishes (gloss / matte / soft-touch) are LAMINATE TYPES, not a\n"
        "separate option. They ONLY apply to business cards, and only when\n"
        "the customer asks about laminate / finish. Default cards are\n"
        "unlaminated \u2014 do NOT push laminate unprompted. DO NOT ask for\n"
        "finish on flyers, leaflets, brochures, NCR books, letterheads,\n"
        "or compliment slips \u2014 they're standard 170gsm silk full-stop.\n"
        "If the customer requests laminate on flyers, escalate to Justin\n"
        "(250gsm silk + lam needs a manual quote).\n"
        "\n"
        "  STEP 1 \u2014 Specs incomplete\n"
        "    Trigger: not enough info to price (no qty, or no sides, or no\n"
        "    finish, etc).\n"
        "    Reply: ONE compact email asking for ALL the missing specs at\n"
        "    once (qty + sides + finish in one go, not one at a time).\n"
        "    No tool call. No PDF. No [QUOTE_READY].\n"
        "\n"
        "  STEP 2 \u2014 Specs complete, artwork question not yet answered\n"
        "    Trigger: enough info to price but customer_has_own_artwork is\n"
        "    None and no [PRIOR QUOTES] header.\n"
        "    Reply: ONE short email asking the artwork question:\n"
        "      \"Do you have print-ready artwork, or would you like our\n"
        "       design service? It's \u20ac65 ex VAT (\u20ac79.95 inc VAT) for one\n"
        "       hour of design work.\"\n"
        "    No tool call. No PDF. No [QUOTE_READY].\n"
        "\n"
        "  STEP 3 \u2014 Artwork answered, funnel info still missing\n"
        "    Trigger: customer_has_own_artwork is set (True / False /\n"
        "    pending-later) AND any of these is missing on the conv:\n"
        "    delivery_method, delivery_address (if delivery), is_company,\n"
        "    is_returning_customer.\n"
        "    Reply: ONE compact email asking for the missing funnel\n"
        "    fields in bullets. SKIP the 'have you ordered with us\n"
        "    before?' bullet \u2014 the [CUSTOMER STATUS] block already\n"
        "    answered it for you. If the customer is RETURNING, open\n"
        "    with 'welcome back' instead of 'thanks':\n"
        "      Hi <Name>,\n"
        "      Thanks. Just a few details and I'll send over the full quote:\n"
        "        \u2022 Delivery (\u20ac15 inc VAT, free over \u20ac100 ex VAT) or\n"
        "          collection from our Ballymount shop?\n"
        "        \u2022 If delivery, the address + Eircode.\n"
        "        \u2022 Are you ordering as a company or individual? (for invoicing)\n"
        "      Cheers,\n"
        "      Craig\n"
        "      Just Print\n"
        "    No pricing tool. No PDF. No [QUOTE_READY] yet.\n"
        "\n"
        "  STEP 4 \u2014 Funnel info just arrived: send the PDF\n"
        "    Trigger: customer_has_own_artwork is set (from earlier turn)\n"
        "    AND the customer's CURRENT message gives you the funnel info\n"
        "    you asked for in STEP 3 (delivery vs collect, address if\n"
        "    delivery, company/individual, new/returning). Even if those\n"
        "    fields aren't yet on the conv row, treat the customer's\n"
        "    current message as authoritative \u2014 you save them on this\n"
        "    same turn.\n"
        "    Common phrasings the customer uses (treat as funnel-complete):\n"
        "      \u2022 \"collection from your shop, individual, new customer\"\n"
        "      \u2022 \"delivery to <address>, company, ordered before with X\"\n"
        "      \u2022 \"collect, just me, first time\"\n"
        "      \u2022 Mix-and-match \u2014 recognize the intent, don't demand exact\n"
        "        wording.\n"
        "    Action sequence \u2014 DO ALL THREE in the same turn:\n"
        "      1. CALL save_customer_info(...) with everything they just\n"
        "         told you PLUS anything captured in earlier turns:\n"
        "           is_company, is_returning_customer, past_customer_email\n"
        "           (only if returning), delivery_method, delivery_address\n"
        "           (only if delivery_method='delivery').\n"
        "      2. CALL the pricing tool with the right needs_artwork:\n"
        "           own-artwork / pending-later \u2192 needs_artwork=false\n"
        "           design service              \u2192 needs_artwork=true,\n"
        "                                          artwork_hours=1.0\n"
        "      3. Compose the FINAL reply using the price the tool\n"
        "         returned, ending with [QUOTE_READY] on its own last line:\n"
        "\n"
        "          Hi <Name>,\n"
        "\n"
        "          Thanks for those details \u2014 got everything I need.\n"
        "\n"
        "          For <qty> <product> <specs>, the total comes to\n"
        "          \u20ac<price from tool> + VAT. I've attached the full\n"
        "          branded quote as a PDF for your records (it shows the\n"
        "          full ex VAT / VAT / inc VAT breakdown).\n"
        "\n"
        "          Turnaround is 3-5 working days once we have your\n"
        "          print-ready artwork. Just reply to this email to\n"
        "          confirm the order, or let me know if you'd like any\n"
        "          tweaks first.\n"
        "\n"
        "          Cheers,\n"
        "          Craig\n"
        "          Just Print\n"
        "\n"
        "          [QUOTE_READY]\n"
        "\n"
        "    NEVER ask the artwork question again at STEP 4. NEVER ask\n"
        "    for funnel info again at STEP 4. NEVER state a price without\n"
        "    a tool call this turn.\n"
        "\n"
        "NEVER ask for name, email, or phone \u2014 we already have them from\n"
        "the envelope. NEVER emit [QUOTE_READY] before STEP 4. The order is\n"
        "always: specs \u2192 artwork \u2192 funnel \u2192 PDF.\n"
        "\n"
        "## Example \u2014 STEP 2 (specs complete, asking artwork question only)\n"
        "Input: \"I need 500 business cards, soft-touch, double-sided\"\n"
        "Good reply (NO pricing tool call, NO [QUOTE_READY], NO PDF mention):\n"
        "\n"
        "Hi Juan,\n"
        "\n"
        "Thanks for reaching out. Before I price the 500 soft-touch double-\n"
        "sided business cards, do you have print-ready artwork, or would\n"
        "you like our design service? It's \u20ac65 ex VAT (\u20ac79.95 inc VAT) for\n"
        "one hour of design work.\n"
        "\n"
        "Cheers,\n"
        "Craig\n"
        "Just Print\n"
        "\n"
        "## Order confirmation mode (when PRIOR QUOTES exist on this thread)\n"
        "If the system injected a [PRIOR QUOTES ALREADY SENT ON THIS THREAD]\n"
        "section listing JP-xxxx references, AND the customer's latest message\n"
        "is a confirmation \u2014 \"yes\", \"go ahead\", \"confirmed\", \"proceed\",\n"
        "\"please print\", \"perfect, do it\", \"i confirm order\", etc. \u2014 you MUST:\n"
        "  1. Call confirm_order(quote_id=<the integer from the JP-xxxx ref>).\n"
        "  2. Reply with a short confirmation. Do NOT re-quote. Do NOT attach\n"
        "     another PDF (no [QUOTE_READY]). Do NOT ask for funnel info\n"
        "     (delivery / company / returning) \u2014 by STEP 4 we already have\n"
        "     all of it on the conv row before the PDF went out.\n"
        "  3. Only nudge for the ONE thing that might still be open:\n"
        "     \u2022 If customer_has_own_artwork=True AND no files are uploaded,\n"
        "       OR artwork_will_send_later=True \u2192 ask them to send the\n"
        "       artwork when it's ready.\n"
        "     \u2022 Otherwise \u2192 just thank them and say production will be in\n"
        "       touch with a timeline.\n"
        "\n"
        "## Example \u2014 order confirmation, artwork pending\n"
        "Prior quote in thread: JP-0018, 500 business_cards, \u20ac269.56,\n"
        "                       status=pending_approval. customer_has_own_artwork=True,\n"
        "                       no files uploaded yet.\n"
        "Input: \"Yes, please go ahead\"\n"
        "Good reply (call confirm_order(18) first, then write):\n"
        "Hi Juan,\n"
        "\n"
        "Perfect, your order for JP-0018 (500 business cards, soft-touch,\n"
        "double-sided, \u20ac219.15 + VAT) is confirmed.\n"
        "\n"
        "Please send through your print-ready artwork when it's ready and\n"
        "we'll get everything moving on our side. We'll be in touch with\n"
        "a production timeline.\n"
        "\n"
        "Cheers,\n"
        "Craig\n"
        "Just Print\n"
        "\n"
        "## Example \u2014 order confirmation, design service (no artwork nudge)\n"
        "Prior quote: JP-0018, customer_has_own_artwork=False (design service).\n"
        "Input: \"i confirm order\"\n"
        "Good reply (confirm_order(18), then write):\n"
        "Hi Juan,\n"
        "\n"
        "Perfect, your order for JP-0018 is confirmed. Our design team will\n"
        "be in touch shortly to discuss what you're looking for, and we'll\n"
        "follow up with a production timeline once the artwork is ready.\n"
        "\n"
        "Cheers,\n"
        "Craig\n"
        "Just Print\n"
        "\n"
        "## Example \u2014 STEP 1, specs missing\n"
        "Input: \"Hi, can you do 500 cards?\"\n"
        "Good reply (NO pricing tool call, NO [QUOTE_READY], NO PDF mention):\n"
        "Hi Juan,\n"
        "\n"
        "Thanks for getting in touch. Sure thing \u2014 to price it for you,\n"
        "I just need a couple of details: single-sided or double-sided,\n"
        "and any finish preference (matte, gloss, or soft-touch)?\n"
        "\n"
        "Cheers,\n"
        "Craig\n"
        "Just Print\n"
        "\n"
        "## Example \u2014 STEP 3, customer answered artwork question\n"
        "(Last assistant message asked the artwork question. Customer's\n"
        "reply: \"Yes, I do need help with the design\" \u2014 server stamps\n"
        "customer_has_own_artwork=False. delivery_method, is_company,\n"
        "is_returning_customer all still null.)\n"
        "Good reply (NO pricing tool call, NO PDF, ask funnel only):\n"
        "Hi Juan,\n"
        "\n"
        "Thanks. Just a few details and I'll send over the full quote:\n"
        "  \u2022 Delivery (\u20ac15 inc VAT, free over \u20ac100 ex VAT) or collection\n"
        "    from our Ballymount shop?\n"
        "  \u2022 If delivery, the full address + Eircode.\n"
        "  \u2022 Are you ordering as a company or individual? (for invoicing)\n"
        "  \u2022 Have you ordered with us before? If yes, what email did you\n"
        "    use last time?\n"
        "\n"
        "Cheers,\n"
        "Craig\n"
        "Just Print\n"
        "\n"
        "## Example \u2014 STEP 4, customer gave funnel info, send PDF\n"
        "(Customer reply: \"individual, new customer, collection from shop\".\n"
        "Specs were 100 cards SS soft-touch with design service. Sequence:)\n"
        "  1. CALL save_customer_info(name=\"Juan\", is_company=false,\n"
        "     is_returning_customer=false, delivery_method=\"collect\")\n"
        "  2. CALL quote_small_format(product_key=\"business_cards\",\n"
        "     quantity=100, double_sided=false, finish=\"soft-touch\",\n"
        "     needs_artwork=true, artwork_hours=1.0)\n"
        "     \u2192 tool returns final_price_inc_vat=135.30\n"
        "  3. Compose:\n"
        "Hi Juan,\n"
        "\n"
        "Thanks for those details. For 100 business cards, single-sided,\n"
        "soft-touch finish, including one hour of design work, the total\n"
        "comes to \u20ac110.00 + VAT (PDF shows the full breakdown).\n"
        "\n"
        "I've attached the full branded quote as a PDF. Turnaround is\n"
        "3-5 working days from when we have print-ready artwork. Reply\n"
        "to this email to confirm the order or with any adjustments.\n"
        "\n"
        "Cheers,\n"
        "Craig\n"
        "Just Print\n"
        "\n"
        "[QUOTE_READY]\n"
        "############################################################\n"
    ),
    "web": (
        "# CURRENT CHANNEL: WEB CHAT\n"
        "Replies render in a floating chat widget. Keep them short (2-3\n"
        "sentences). Emojis are fine and expected. Follow the personality\n"
        "tone above.\n"
        "\n"
        "## Artwork pending-later (v30)\n"
        "If the customer says any of: \"I'll send the artwork later\",\n"
        "\"I haven't finalised the artwork\", \"not finalised yet\",\n"
        "\"I just need a price\", \"still working on the design\", or\n"
        "they click the [I'll send my artwork later] button — proceed\n"
        "to price as usual and ask for the funnel info. Do NOT push the\n"
        "upload card. Do NOT keep saying \"send your artwork over\". The\n"
        "server will set artwork_will_send_later=True on the conversation\n"
        "and Justin will see an \"Artwork pending\" badge on the quote.\n"
    ),
}


def _build_channel_context(channel: str) -> str:
    """Return channel-specific instructions, or empty string if unknown."""
    return _CHANNEL_CONTEXT.get((channel or "").lower(), "")


def _build_business_rules_context(db: Session, organization_slug: str) -> str:
    """
    Render any tenant-specific business rules the user has added from the
    Settings tab. Stored as a JSON array of strings under `business_rules`.

    The returned block is placed at the TOP of the system prompt with an
    override header — the LLM treats the earliest, most emphatic directives
    as highest priority, and business rules are exactly that: tenant-level
    overrides that MUST win over anything in the base personality text.
    """
    from pricing_engine import _get_setting

    raw = _get_setting(db, "business_rules", None, organization_slug=organization_slug)
    if not raw:
        return ""
    try:
        rules = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    rules = [str(r).strip() for r in rules if isinstance(r, str) and r.strip()]
    if not rules:
        return ""
    numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))
    return (
        "# OVERRIDE RULES (HIGHEST PRIORITY — these supersede every rule in the base personality below)\n"
        "If any instruction later in this prompt conflicts with a rule here, follow the rule here. "
        "These are non-negotiable, tenant-level policies.\n\n"
        f"{numbered}\n"
    )


def _build_faq_context(db: Session, organization_slug: str) -> str:
    """
    Render the tenant's FAQ list as an injected knowledge block.

    Stored in setting `craig_faqs_json` as a JSON array of `{q, a}`
    objects. The `{{shop_address}}` placeholder in any answer is
    expanded to the live `shop_address` setting so customers always get
    the right address, even if Justin updates it.

    Returned with a clear header so the LLM knows it's reference
    material — paraphrase, don't recite verbatim.
    """
    from pricing_engine import _get_setting

    raw = _get_setting(db, "craig_faqs_json", None, organization_slug=organization_slug)
    if not raw:
        return ""
    try:
        faqs = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(faqs, list):
        return ""

    shop_address = _get_setting(
        db, "shop_address", "", organization_slug=organization_slug,
    ) or ""

    lines: list[str] = [
        "## Frequently asked questions (Craig should answer naturally if asked)",
        "Paraphrase in your own voice — don't recite the answer verbatim. "
        "Do NOT escalate any of these to Justin.",
        "",
    ]
    for entry in faqs:
        if not isinstance(entry, dict):
            continue
        q = (entry.get("q") or entry.get("question") or "").strip()
        a = (entry.get("a") or entry.get("answer") or "").strip()
        if not q or not a:
            continue
        a = a.replace("{{shop_address}}", shop_address or "[shop address — pending]")
        lines.append(f"Q: {q}")
        lines.append(f"A: {a}")
        lines.append("")
    if len(lines) <= 3:
        return ""
    return "\n".join(lines).rstrip() + "\n"


# =============================================================================
# TOOL DEFINITIONS (OpenAI function-calling format)
# =============================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "quote_small_format",
            "description": (
                "Get a price for a small-format product (business cards, flyers, brochures, "
                "compliment slips, letterheads, NCR books). Returns the exact price from Justin's sheet "
                "or an escalation message if the combination isn't available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_key": {
                        "type": "string",
                        "description": "Product key from the catalog",
                        "enum": [
                            "business_cards", "flyers_a6", "flyers_a5", "flyers_a4", "flyers_dl",
                            "brochures_a4", "compliment_slips", "letterheads",
                            "ncr_books_a5", "ncr_books_a4",
                        ],
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Number of items. Must match a tier on the pricing sheet.",
                    },
                    "double_sided": {
                        "type": "boolean",
                        "description": "True if double-sided, false if single-sided.",
                    },
                    "finish": {
                        "type": "string",
                        "description": "Finish option. Valid: gloss, matte, soft-touch, uncoated, duplicate, triplicate.",
                    },
                    "needs_artwork": {
                        "type": "boolean",
                        "description": "True if the customer needs design work.",
                    },
                    "artwork_hours": {
                        "type": "number",
                        "description": "Estimated design hours (only when needs_artwork is true).",
                    },
                },
                "required": ["product_key", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quote_large_format",
            "description": (
                "Get a price for a large-format product (banners, boards, signage, vehicle magnetics, "
                "vinyl labels). Applies unit or bulk pricing based on quantity. "
                "FOR BOARD PRODUCTS (corri_boards, foamex_boards, dibond_boards): pass `size` for "
                "one of the 7 standard sizes (A4, A3, A2, A1, A0, 2440x1220, 1220x1220), OR pass "
                "`width_mm` + `height_mm` for custom panel sizes (engine runs the laydown "
                "calculator). NOTE: per-sq/m products (vinyl_labels, pvc_banners, window_graphics, "
                "floor_graphics, mesh_banners, fabric_displays) will return manual_review=true — "
                "pass width_mm/height_mm if the customer told you so Justin can manually price."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_key": {
                        "type": "string",
                        "enum": [
                            "roller_banners", "foamex_boards", "dibond_boards", "corri_boards",
                            "pvc_banners", "canvas_prints", "window_graphics", "floor_graphics",
                            "mesh_banners", "fabric_displays", "vehicle_magnetics", "vinyl_labels",
                        ],
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Number of units. For per-sq/m products this is the count of items the customer wants, NOT the area.",
                    },
                    "size": {
                        "type": "string",
                        "enum": ["A4", "A3", "A2", "A1", "A0", "2440x1220", "1220x1220"],
                        "description": (
                            "Standard size for board products (corri/foamex/dibond). "
                            "Use 2440x1220 for full sheet, 1220x1220 for half sheet. "
                            "If the customer gave custom dimensions in mm instead, pass "
                            "width_mm + height_mm (no `size`)."
                        ),
                    },
                    "width_mm": {
                        "type": "integer",
                        "description": "Width per unit in millimetres. Pass this for per-sq/m products (vinyl labels, banners, graphics) when the customer has told you the size, OR for custom board panel sizes.",
                    },
                    "height_mm": {
                        "type": "integer",
                        "description": "Height per unit in millimetres. Pass alongside width_mm.",
                    },
                    "area_sqm": {
                        "type": "number",
                        "description": "Total area in square metres if the customer gave it directly (e.g. '10 m² of vinyl').",
                    },
                    "needs_artwork": {"type": "boolean"},
                    "artwork_hours": {"type": "number"},
                },
                "required": ["product_key", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quote_booklet",
            "description": (
                "Get a price for a booklet (A5 or A4, saddle stitched or perfect bound). "
                "Requires format, binding, page count, cover type, and quantity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["a5", "a4"]},
                    "binding": {"type": "string", "enum": ["saddle_stitch", "perfect_bound"]},
                    "pages": {
                        "type": "integer",
                        "description": "Total page count. Must match a tier on the sheet.",
                    },
                    "cover_type": {
                        "type": "string",
                        "enum": ["self_cover", "card_cover", "card_cover_lam"],
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Number of copies. Must be 25, 50, 100, 250, or 500.",
                    },
                    "needs_artwork": {"type": "boolean"},
                    "artwork_hours": {"type": "number"},
                },
                "required": ["format", "binding", "pages", "cover_type", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_products",
            "description": (
                "List all available products, optionally filtered by category. "
                "Use this when the customer asks what Just Print offers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["small_format", "large_format", "booklet"],
                        "description": "Filter by category. Omit for all products.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_customer_info",
            "description": (
                "Save EVERYTHING you've collected from the customer in one call: "
                "identity (name + email/phone), invoicing flag (company vs individual), "
                "returning-customer status (with their previous email if relevant), "
                "and delivery preference (delivery + address, or collect from shop). "
                "All fields except `name` are optional — pass only what you've actually "
                "collected; nulls don't overwrite prior values. Call ONCE at the end "
                "of the contact-collection flow, not per-question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Customer's name (or company contact's name).",
                    },
                    "email": {
                        "type": "string",
                        "description": "Customer's email address (if provided).",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number (if provided).",
                    },
                    "preferred_channel": {
                        "type": "string",
                        "enum": ["email", "whatsapp", "phone"],
                        "description": "How they prefer to be contacted.",
                    },
                    "is_company": {
                        "type": "boolean",
                        "description": (
                            "true if ordering on behalf of a company (B2B invoicing), "
                            "false if individual / consumer."
                        ),
                    },
                    "is_returning_customer": {
                        "type": "boolean",
                        "description": (
                            "true if they've ordered with Just Print before. If true, "
                            "also fill `past_customer_email` so we can link to their "
                            "existing record in PrintLogic."
                        ),
                    },
                    "past_customer_email": {
                        "type": "string",
                        "description": (
                            "The email address they used on a prior order. Only set "
                            "when is_returning_customer=true."
                        ),
                    },
                    "delivery_method": {
                        "type": "string",
                        "enum": ["delivery", "collect"],
                        "description": (
                            "How they want to receive the order. 'delivery' requires "
                            "delivery_address; 'collect' means they'll pick up at the shop."
                        ),
                    },
                    "delivery_address": {
                        "type": "object",
                        "description": (
                            "Delivery address (only when delivery_method='delivery'). "
                            "All fields strings; address1 + postcode minimum."
                        ),
                        "properties": {
                            "address1": {"type": "string"},
                            "address2": {"type": "string"},
                            "address3": {"type": "string"},
                            "address4": {"type": "string"},
                            "postcode": {"type": "string"},
                        },
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_past_quotes_by_email",
            "description": (
                "Look up prior approved/sent/accepted quotes for a returning customer "
                "by their email address. Call this when a customer says they've ordered "
                "before and gives you the email they used. Returns a short list of "
                "their last quotes (product, qty, total, when) so you can offer to "
                "re-order the same spec. Tenant-scoped — never returns other clients' "
                "data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "The customer's previous email address.",
                    },
                },
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_justin",
            "description": (
                "Escalate a request to Justin when it's outside Craig's scope: POA items "
                "(Z-fold, die-cut, installation), custom sizes, rush jobs, quantities not on "
                "the sheet, or anything Craig isn't sure about."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why this is being escalated.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Summary of what the customer wants, for Justin's reference.",
                    },
                },
                "required": ["reason", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_order",
            "description": (
                "Lock in an order the customer has just agreed to. Call this when the "
                "customer explicitly confirms a quote that was already sent on this "
                "thread (e.g. replies with 'yes', 'go ahead', 'proceed', 'confirmed', "
                "'please print'). The system prompt injects a list of quotes already "
                "on this thread under [PRIOR QUOTES ALREADY SENT ON THIS THREAD] \u2014 "
                "use the JP-xxxx number from there as the quote_id argument. Do NOT "
                "call this on a fresh request without a prior quote; quote first, "
                "then wait for the customer's confirmation before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "quote_id": {
                        "type": "integer",
                        "description": "The integer ID of the quote being confirmed (the numeric part of JP-xxxx).",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional customer notes, e.g. delivery address or rush requirements.",
                    },
                },
                "required": ["quote_id"],
            },
        },
    },
]


def _build_tools_for_org(db: Session, organization_slug: str) -> list:
    """v40.4 — dynamic tool definitions per chat turn.

    The static ``TOOLS`` array hard-coded the small_format /
    large_format product_key enums (business_cards, flyers_a5, …).
    That worked when the catalog was edited only by code-side
    migrations, but the v40.2 bulk import lets Justin add arbitrary
    products from a workbook. With the hard-coded enum, DeepSeek
    would either refuse to call the pricing tool for a brand-new
    product or escalate it unnecessarily — Craig "didn't know it
    existed" even though the catalog context in the system prompt
    listed it.

    This builder is called once per ``chat_with_craig`` invocation
    and replaces those two enums with the live keys for the tenant
    from the ``products`` table.

    Tenant scoping is preserved (the same ``organization_slug``
    threaded through the rest of the chat path), so two tenants
    using the same Cloud Run revision still see only their own
    products in the tool schema. The enum is *dropped entirely* if
    a category has zero products for that tenant — an empty
    ``enum: []`` is technically valid JSON Schema but confuses the
    LLM ("the list is empty, am I allowed to call this?"). Better
    to remove the constraint and let the prompt's catalog context
    govern.

    ``quote_booklet`` is left untouched — its enums are
    ``format`` (a5/a4) and ``binding`` (saddle_stitch / perfect_bound),
    both fundamental product structure rather than catalog data,
    so they remain hard-coded.

    Returns a deep copy of ``TOOLS`` with the enums adjusted — the
    module-level constant stays untouched so concurrent requests
    can't trample each other's tool list.
    """
    import copy as _copy
    from db.models import Product as _Product

    small_keys = sorted(
        row[0]
        for row in db.query(_Product.key).filter_by(
            organization_slug=organization_slug, category="small_format",
        ).all()
    )
    large_keys = sorted(
        row[0]
        for row in db.query(_Product.key).filter_by(
            organization_slug=organization_slug, category="large_format",
        ).all()
    )

    tools = _copy.deepcopy(TOOLS)
    for tool in tools:
        fn = tool.get("function", {})
        if fn.get("name") not in ("quote_small_format", "quote_large_format"):
            continue
        product_key_spec = (
            fn.get("parameters", {})
            .get("properties", {})
            .get("product_key")
        )
        if not isinstance(product_key_spec, dict):
            continue
        target_keys = (
            small_keys if fn["name"] == "quote_small_format" else large_keys
        )
        if target_keys:
            product_key_spec["enum"] = target_keys
        else:
            # No products in this category for this tenant — drop the
            # enum so the LLM treats it as a free string instead of
            # being told "the enum is empty, you may not call me".
            product_key_spec.pop("enum", None)
    return tools


# =============================================================================
# TOOL EXECUTION
# =============================================================================


def _handle_manual_review_escalation(
    *,
    db: Session,
    args: dict,
    result: EscalationResult,
    conversation_id: int | None,
    organization_slug: str,
) -> dict:
    """
    v34 — handle a pricing tool that returned manual_review=True.

    Auto-creates a Quote with status='needs_revision', no price, and
    whatever specs the LLM passed (qty, dimensions, sides, finish).
    Triggers the manual-review notification email to Justin and
    returns a guidance dict the LLM uses to compose its reply.

    The LLM is told NOT to invent a price; instead it should
    acknowledge "let me check with Justin" and ask for the missing
    detail (typically dimensions in mm).
    """
    # Best-effort: stamp the quote with the captured specs. The LLM
    # may or may not have passed dimensions; either way, what's
    # captured here is what Justin sees in the manual-pricing form.
    specs: dict = {}
    for key in (
        "quantity", "double_sided", "finish",
        "width_mm", "height_mm", "area_sqm",
        "format", "binding", "pages", "cover_type",
        "needs_artwork", "artwork_hours",
    ):
        v = args.get(key)
        if v is not None:
            specs[key] = v

    # v35 — propagate Conversation.is_test onto Quote.is_test so test
    # quotes never appear in the regular Quotations module.
    is_test_conv = False
    if conversation_id is not None:
        try:
            _conv = db.query(Conversation).filter_by(id=conversation_id).first()
            is_test_conv = bool(_conv and getattr(_conv, "is_test", False))
        except Exception:
            is_test_conv = False

    quote_id: Optional[int] = None
    try:
        quote = Quote(
            organization_slug=organization_slug,
            conversation_id=conversation_id,
            product_key=args.get("product_key") or specs.get("format") or None,
            specs=specs,
            base_price=None,
            surcharges=[],
            final_price_ex_vat=None,
            vat_amount=None,
            final_price_inc_vat=None,
            artwork_cost=0.0,
            total=None,
            status="needs_revision",
            manual_review_reason=result.reason,
            notes=None,
            is_test=is_test_conv,
        )
        db.add(quote)
        db.flush()
        quote_id = quote.id
        db.commit()
        print(
            f"[craig] manual_review: created Quote JP-{quote.id:04d} "
            f"product={quote.product_key} reason={result.reason!r} "
            f"specs={specs!r}",
            flush=True,
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print(
            f"[craig] manual_review: FAILED to create needs_revision "
            f"quote err={type(e).__name__}: {e}",
            flush=True,
        )
        # Fall through — even without a Quote row, return guidance to
        # the LLM so the customer gets a coherent reply.

    # Fire operator notification (idempotent on notification_sent_at).
    if quote_id is not None:
        try:
            from notifications import trigger_manual_review_notification
            trigger_manual_review_notification(db, organization_slug, quote_id)
        except Exception as ne:
            print(
                f"[craig] manual_review: notification failed (non-fatal) "
                f"err={type(ne).__name__}: {ne}",
                flush=True,
            )

    # Return guidance dict — the LLM sees this as the tool's output.
    return {
        "success": False,
        "escalate": True,
        "manual_review": True,
        "needs_revision_quote_id": quote_id,
        "reason": result.reason,
        "message": (
            "ACKNOWLEDGE to the customer that Justin will check + come "
            "back to them, and ASK FOR DIMENSIONS (width × height per "
            "unit in mm) if you haven't already. NEVER invent a price. "
            "NEVER use 'around', 'roughly', 'about', 'approximately'. "
            "Then call save_customer_info if you don't have name+email "
            "yet, and stop. Do NOT call any other quote tool on this "
            "turn.\n\n"
            "Recommended wording: 'Let me check that with Justin and "
            "get back to you 👍 Quick question first — what size is "
            "each label (width × height in mm)?' (adapt the noun "
            "'label' to whatever the customer asked about)."
        ),
    }


def _exec_tool(
    db: Session,
    name: str,
    args: dict,
    conversation_id: int | None = None,
    organization_slug: str = "just-print",
) -> dict:
    """Execute a tool call and return a dict the LLM can read. All pricing is
    scoped to `organization_slug` so Craig reads the right tenant's catalog."""
    try:
        # v38 — Phase F's "artwork-question required before pricing"
        # guard has been REMOVED. The audit showed it caused 42% of
        # widget customers to abandon (they wanted to see a price
        # BEFORE committing to send artwork). New flow is the inverse:
        # the LLM calls the tool with needs_artwork=False (default)
        # and the reply contains BOTH the price AND the artwork
        # question. If the customer picks the design service, the
        # LLM re-calls the tool with needs_artwork=True to add the
        # €65/hr line item.
        #
        # The old guard text is preserved here as a comment so future
        # readers know why we deleted it:
        #     "ARTWORK_QUESTION_REQUIRED: Before quoting, ask the
        #      customer 'Do you have print-ready artwork, or design
        #      service?'. Wait for answer, then re-call the tool."
        # — removed in v38 (price-first flow).

        if name == "quote_small_format":
            result = quote_small_format(
                db,
                product_key=args["product_key"],
                quantity=int(args["quantity"]),
                double_sided=bool(args.get("double_sided", False)),
                finish=args.get("finish"),
                needs_artwork=bool(args.get("needs_artwork", False)),
                artwork_hours=float(args.get("artwork_hours", 0.0)),
                organization_slug=organization_slug,
            )
            if isinstance(result, EscalationResult) and result.manual_review:
                return _handle_manual_review_escalation(
                    db=db,
                    args=args,
                    result=result,
                    conversation_id=conversation_id,
                    organization_slug=organization_slug,
                )
            return result.to_dict()

        if name == "quote_large_format":
            # v34 — accept dimensions from the LLM. Even though the
            # engine still escalates per-sq/m products by policy,
            # capturing the dimensions here means they're stamped onto
            # the needs_revision Quote's specs so Justin can manually
            # price it without emailing the customer back.
            # v40.7 — `size` routes to the 2-D board pricing path; width/
            # height (no size) routes to the laydown calculator.
            _w = args.get("width_mm")
            _h = args.get("height_mm")
            _a = args.get("area_sqm")
            _size = args.get("size")
            result = quote_large_format(
                db,
                product_key=args["product_key"],
                quantity=int(args["quantity"]),
                needs_artwork=bool(args.get("needs_artwork", False)),
                artwork_hours=float(args.get("artwork_hours", 0.0)),
                organization_slug=organization_slug,
                width_mm=int(_w) if _w is not None else None,
                height_mm=int(_h) if _h is not None else None,
                area_sqm=float(_a) if _a is not None else None,
                size=str(_size) if _size else None,
            )
            # If the engine refused by manual-review policy, hand off to
            # the v34 escalation handler — auto-create a Quote with
            # status='needs_revision', notify Justin, return guidance.
            if isinstance(result, EscalationResult) and result.manual_review:
                return _handle_manual_review_escalation(
                    db=db,
                    args=args,
                    result=result,
                    conversation_id=conversation_id,
                    organization_slug=organization_slug,
                )
            return result.to_dict()

        if name == "quote_booklet":
            result = quote_booklet(
                db,
                format=args["format"],
                binding=args["binding"],
                pages=int(args["pages"]),
                cover_type=args["cover_type"],
                quantity=int(args["quantity"]),
                needs_artwork=bool(args.get("needs_artwork", False)),
                artwork_hours=float(args.get("artwork_hours", 0.0)),
                organization_slug=organization_slug,
            )
            if isinstance(result, EscalationResult) and result.manual_review:
                return _handle_manual_review_escalation(
                    db=db,
                    args=args,
                    result=result,
                    conversation_id=conversation_id,
                    organization_slug=organization_slug,
                )
            return result.to_dict()

        if name == "list_products":
            return {
                "products": list_products(
                    db,
                    category=args.get("category"),
                    organization_slug=organization_slug,
                )
            }

        if name == "save_customer_info":
            # Save contact + funnel info to the conversation record.
            # Only overwrite when a non-empty / non-null value is supplied —
            # LLMs routinely call this tool with a partial update (e.g. just
            # the new field they just collected) and would otherwise nuke
            # data we stored on the previous turn.
            #
            # v32.2 — sanity guard: reject placeholder phrases the LLM
            # sometimes copy-pastes from the prompt's meta-instruction
            # text instead of extracting a real value (conv 126 saved
            # name=\"the customer's name from the conversation\"). Drop
            # those before they hit the DB.
            _PLACEHOLDER_RX = re.compile(
                r"(customer'?s\s+name|name\s+from\s+the|name\s+from\s+envelope|"
                r"customer'?s\s+email|email\s+from\s+the|<\s*name\s*>|<\s*email\s*>|"
                r"placeholder|\[NAME\]|\[EMAIL\]|TBD|UNKNOWN)",
                re.IGNORECASE,
            )
            def _is_placeholder(v: object) -> bool:
                if not isinstance(v, str):
                    return False
                return bool(_PLACEHOLDER_RX.search(v))

            if conversation_id:
                conv = db.query(Conversation).filter_by(id=conversation_id).first()
                if conv:
                    raw_name = (args.get("name") or "").strip()
                    if raw_name and _is_placeholder(raw_name):
                        print(
                            f"[craig] save_customer_info: REJECTED "
                            f"placeholder name={raw_name!r} on conv "
                            f"{conversation_id} (LLM hallucinated meta-"
                            f"instruction text)", flush=True,
                        )
                        raw_name = ""
                    if raw_name:
                        conv.customer_name = raw_name
                    if (args.get("email") or "").strip():
                        conv.customer_email = args["email"].strip()
                    if (args.get("phone") or "").strip():
                        conv.customer_phone = args["phone"].strip()
                    # Phase E — extended funnel fields. Booleans accept
                    # explicit False (don't drop it); strings/objects need
                    # truthy check so empty strings don't blank existing data.
                    if "is_company" in args and args["is_company"] is not None:
                        conv.is_company = bool(args["is_company"])
                    if "is_returning_customer" in args and args["is_returning_customer"] is not None:
                        conv.is_returning_customer = bool(args["is_returning_customer"])
                    raw_past = (args.get("past_customer_email") or "").strip()
                    if raw_past and _is_placeholder(raw_past):
                        print(
                            f"[craig] save_customer_info: REJECTED "
                            f"placeholder past_customer_email={raw_past!r} "
                            f"on conv {conversation_id}", flush=True,
                        )
                        raw_past = ""
                    if raw_past:
                        conv.past_customer_email = raw_past
                    method = (args.get("delivery_method") or "").strip().lower()
                    if method in ("delivery", "collect"):
                        conv.delivery_method = method
                    addr = args.get("delivery_address")
                    if isinstance(addr, dict) and any(
                        (addr.get(k) or "").strip()
                        for k in ("address1", "address2", "address3", "address4", "postcode")
                    ):
                        # Normalise — only persist non-empty subkeys
                        conv.delivery_address = {
                            k: (addr.get(k) or "").strip()
                            for k in ("address1", "address2", "address3", "address4", "postcode")
                            if (addr.get(k) or "").strip()
                        }
                    db.flush()

                    # v29 — apply shipping to the latest pending Quote on
                    # this conversation when delivery_method is now set.
                    # Mirrors what widget_api.submit_customer_info does
                    # for the form-based flow. Without this, an
                    # email-channel customer who opted for delivery
                    # would still see €0 shipping on the PDF.
                    if (conv.delivery_method or "").strip() in ("delivery", "collect"):
                        try:
                            from pricing_engine import apply_shipping_to_quote
                            pending = (
                                db.query(Quote)
                                .filter_by(
                                    conversation_id=conv.id,
                                    status="pending_approval",
                                )
                                .order_by(Quote.created_at.desc())
                                .first()
                            )
                            if pending is not None:
                                apply_shipping_to_quote(
                                    db, pending,
                                    conv.delivery_method,
                                    organization_slug=conv.organization_slug,
                                )
                                db.flush()
                                print(
                                    f"[craig] save_customer_info: applied "
                                    f"shipping to quote {pending.id} "
                                    f"(method={conv.delivery_method})",
                                    flush=True,
                                )
                        except Exception as ship_err:
                            print(
                                f"[craig] save_customer_info: shipping "
                                f"application failed (non-fatal): "
                                f"{ship_err!r}",
                                flush=True,
                            )

            return {
                "saved": True,
                "name": args.get("name"),
                "email": args.get("email"),
                "phone": args.get("phone"),
                "preferred_channel": args.get("preferred_channel"),
                "is_company": args.get("is_company"),
                "is_returning_customer": args.get("is_returning_customer"),
                "past_customer_email": args.get("past_customer_email"),
                "delivery_method": args.get("delivery_method"),
                "delivery_address": args.get("delivery_address"),
            }

        if name == "find_past_quotes_by_email":
            # Tenant-scoped lookup. Returns a compact list so the LLM can
            # offer to re-order the same spec. Status filter intentionally
            # narrow — only quotes the customer has actually accepted /
            # paid for in the past, not abandoned ones.
            email = (args.get("email") or "").strip().lower()
            if not email:
                return {"found": False, "quotes": [], "message": "No email provided."}
            past = (
                db.query(Quote)
                .join(Conversation, Quote.conversation_id == Conversation.id)
                .filter(
                    Conversation.organization_slug == organization_slug,
                    Conversation.customer_email.ilike(email),
                    Quote.status.in_(("approved", "sent", "accepted")),
                )
                .order_by(Quote.created_at.desc())
                .limit(5)
                .all()
            )
            if not past:
                return {
                    "found": False, "quotes": [],
                    "message": f"No prior quotes found for {email}. Treat them as a new customer.",
                }
            summary = [
                {
                    "ref": f"JP-{q.id:04d}",
                    "quote_id": q.id,
                    "product_key": q.product_key,
                    "specs": q.specs or {},
                    "total_inc_vat": float(q.final_price_inc_vat or 0),
                    "created_at": q.created_at.isoformat() if q.created_at else None,
                }
                for q in past
            ]
            return {
                "found": True,
                "count": len(summary),
                "quotes": summary,
                "message": (
                    f"Found {len(summary)} prior order(s) for {email}. Reference "
                    f"them naturally if relevant — e.g. 'I see you ordered "
                    f"{summary[0]['product_key']} before; want the same spec?'"
                ),
            }

        if name == "escalate_to_justin":
            # Server-side gate: refuse to flag an escalation if no contact
            # info exists on the conversation. The business rules tell the
            # LLM to collect name + email first, but DeepSeek regularly
            # ignores that and jumps straight to "escalate". Returning an
            # error payload here makes the LLM retry after running
            # save_customer_info, which mirrors how the [QUOTE_READY] gate
            # works for the PDF flow.
            if conversation_id:
                conv = db.query(Conversation).filter_by(id=conversation_id).first()
                has_contact = bool(
                    (conv.customer_email or "").strip()
                    or (conv.customer_phone or "").strip()
                ) if conv else False
                if not has_contact:
                    return {
                        "error": (
                            "Cannot escalate yet \u2014 the customer's contact "
                            "info hasn't been collected. Ask them for their "
                            "name and email (or WhatsApp number), call "
                            "save_customer_info, THEN retry escalate_to_justin."
                        ),
                        "escalated": False,
                        "retry_after": "save_customer_info",
                    }
            return {
                "escalated": True,
                "reason": args["reason"],
                "summary": args["summary"],
                "message": "Noted. Justin will get back to you directly.",
            }

        if name == "confirm_order":
            # Lock in a previously-sent quote. Called when the customer
            # explicitly accepts ("yes", "go ahead", "confirmed") a quote
            # that's already in the thread.
            #
            # IMPORTANT — this tool is now PASSIVE. It only:
            #   - flips the Conversation status to "order_placed" (queue signal)
            #   - records `client_confirmed_at` on the Quote
            #   - leaves Quote.status at "pending_approval" so Justin sees it
            #
            # It does NOT create a Stripe payment link, NOT generate a Missive
            # draft, NOT push to PrintLogic. Those are now triggered by the
            # human-in-the-loop "Approve" action in the dashboard (PATCH
            # /quotes/{id} status=approved). This is the demo-aligned flow
            # Justin asked for: he reviews every quote before any commercial
            # action fires.
            try:
                qid = int(args["quote_id"])
            except (KeyError, ValueError, TypeError):
                return {"error": "quote_id is required and must be an integer", "confirmed": False}
            q = (
                db.query(Quote)
                .filter_by(id=qid, conversation_id=conversation_id)
                .first()
            )
            if not q:
                return {
                    "error": f"Quote JP-{qid:04d} not found on this conversation",
                    "confirmed": False,
                }
            # Status stays at "pending_approval" — only Justin's PATCH approve
            # flips it. We track the client signal via Conversation.status and
            # client_confirmed_at on the Quote.
            q.client_confirmed_at = _dt.datetime.utcnow()
            if (args.get("notes") or "").strip():
                q.notes = args["notes"].strip()
            if conversation_id:
                conv = db.query(Conversation).filter_by(id=conversation_id).first()
                if conv:
                    conv.status = "order_placed"
            db.flush()

            customer_msg = (
                "All set! Justin will review your quote and email you the "
                "official confirmation with payment details shortly. \U0001f44d"
            )

            return {
                "confirmed": True,
                "quote_id": qid,
                "ref": f"JP-{qid:04d}",
                "message": customer_msg,
                # Status fields kept for backwards-compat with callers /
                # tests that expected the old shape; integrations don't
                # fire here any more.
                "printlogic_pushed": False,
                "printlogic_order_id": None,
                "printlogic_dry_run": None,
                "printlogic_error": "deferred_to_approve",
                "stripe_link_created": False,
                "stripe_link_url": None,
                "stripe_disabled": True,
                "stripe_error": "deferred_to_approve",
                "missive_draft_created": False,
                "missive_draft_id": None,
                "missive_skipped": True,
                "missive_skip_reason": "deferred_to_approve",
                "missive_error": None,
            }

        return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# MAIN CHAT FUNCTION
# =============================================================================


def chat_with_craig(
    db: Session,
    conversation_id: Optional[int],
    user_message: str,
    external_id: Optional[str] = None,
    channel: str = "web",
    organization_slug: str = "just-print",
    extra_system_messages: Optional[list[dict]] = None,
    is_test: bool = False,
    attribution: Optional[dict] = None,
) -> dict:
    """
    Main entry point. Handles one turn of conversation.

    `organization_slug` scopes everything:
      - the system prompt is loaded from the Setting table for that tenant
        (falls back to the hardcoded CRAIG_SYSTEM_PROMPT if not found)
      - every tool call hits the pricing engine with that tenant's data
      - the Conversation + Quote records are tagged with that tenant

    `is_test` (v35) — when True, this is a sandbox conversation from
    the Test Chat module in the dashboard:
      - Conversation is marked is_test=True (auto-filtered from
        Conversations module)
      - The artwork-question gate is skipped (the LLM can quote
        immediately without first asking "do you have artwork?")
      - The system prompt gains a TEST MODE header instructing the
        LLM not to ask about contact info, delivery, or artwork
      - Quotes generated are also marked is_test=True

    Returns:
      {
        "reply": str,                 # Craig's natural-language reply
        "conversation_id": int,       # for subsequent turns
        "quote_generated": bool,      # True if a successful quote was produced
        "escalated": bool,            # True if escalate_to_justin was called
        "tool_calls": list[dict],     # raw tool calls for debugging/audit
      }
    """
    # Lazy import to avoid a circular dep if craig_agent grows (pricing_engine
    # already imports Conversation/Quote via db.models so this is safe).
    from pricing_engine import _get_setting

    # Load or create conversation (tenant-scoped)
    if conversation_id:
        conversation = db.query(Conversation).filter_by(id=conversation_id).first()
        if conversation is None:
            conversation = Conversation(
                organization_slug=organization_slug,
                external_id=external_id, channel=channel, messages=[],
                is_test=bool(is_test),
            )
            db.add(conversation)
            db.flush()
    else:
        conversation = Conversation(
            organization_slug=organization_slug,
            external_id=external_id, channel=channel, messages=[],
            is_test=bool(is_test),
        )
        db.add(conversation)
        db.flush()

    # v35 — propagate the is_test flag if this is a re-entry on an
    # existing conversation that was created as test (idempotent).
    if is_test and not conversation.is_test:
        conversation.is_test = True
        db.flush()
    # Pre-set artwork flag so the artwork-question gate skips. The
    # tool-execution layer (_exec_tool) checks customer_has_own_artwork
    # before pricing — setting it to True here means a test quote
    # doesn't need a funnel preamble.
    if conversation.is_test and conversation.customer_has_own_artwork is None:
        conversation.customer_has_own_artwork = True
        db.flush()

    # v40 — merge marketing attribution from the widget (first-touch
    # write-once, last-touch always). Wrapped so an attribution hiccup
    # can never break the chat turn.
    if attribution:
        try:
            from attribution import merge_attribution
            if merge_attribution(conversation, attribution):
                db.flush()
        except Exception as _attr_err:  # pragma: no cover - defensive
            print(
                f"[craig] attribution merge failed on conv "
                f"{conversation.id}: {_attr_err}",
                flush=True,
            )

    # Load this tenant's system prompt from the Setting table; fall back to the
    # code-level default if no custom prompt has been configured yet.
    base_prompt = _get_setting(
        db,
        "system_prompt",
        default=CRAIG_SYSTEM_PROMPT,
        organization_slug=organization_slug,
    )

    # Compose the final system prompt.
    #
    # Ordering matters: DeepSeek (and all transformers) attend most strongly
    # to the earliest tokens in the context. Business rules go FIRST, marked
    # as override rules — this is how we reliably beat contradictory language
    # that may be baked into the tenant's base personality text. Catalog goes
    # last because it's long but lower-priority (the LLM just needs to look
    # it up when specs are in question).
    catalog_ctx = _build_catalog_context(db, organization_slug)
    channel_ctx = _build_channel_context(channel)

    # Email is a different medium from chat with different tone, structure,
    # and flow. The stored base personality + business rules are written
    # for chat — they contain literal phrases like "Nice one!", "That'll
    # be €X", and "Want me to put together the full quote for you? 📋"
    # that DeepSeek lifts verbatim into email drafts no matter how loud
    # the channel override shouts.
    #
    # For email we therefore drop both the base personality AND the
    # business rules, and rely entirely on:
    #   - the EMAIL channel override (tone, structure, few-shot example)
    #   - the live catalog (what we sell, at what price, with what specs)
    # That's sufficient context to quote correctly without the chat-voice
    # noise bleeding through.
    is_email = (channel or "").lower() == "missive"

    if is_email:
        rules_ctx = ""
        effective_base_prompt = ""
        # FAQs apply equally to email — customer might ask "do you ship?"
        # in an email and we want the same answer Craig gives in chat.
        faq_ctx = _build_faq_context(db, organization_slug)
    else:
        rules_ctx = _build_business_rules_context(db, organization_slug)
        effective_base_prompt = base_prompt
        faq_ctx = _build_faq_context(db, organization_slug)

    # Order: channel override (highest), business rules, base personality,
    # FAQs (reference material), catalog (lookup). FAQs sit between the
    # base prompt and the catalog so they're treated as background
    # knowledge — Craig answers them inline rather than escalating.
    system_prompt = "\n\n".join(
        section
        for section in (channel_ctx, rules_ctx, effective_base_prompt, faq_ctx, catalog_ctx)
        if section
    )
    # Diagnostic: confirm what the LLM actually receives. Cheap to log,
    # invaluable when behavior doesn't match expectations. Cloud Run's
    # `gcloud run services logs` picks up stdout.
    try:
        print(
            f"[craig] channel={channel!r} org={organization_slug!r} "
            f"prompt_len={len(system_prompt)} prompt_head={system_prompt[:240]!r}",
            flush=True,
        )
    except Exception:
        pass

    # Build message history for the LLM
    messages = [{"role": "system", "content": system_prompt}]

    # v35 — TEST MODE banner. Sandbox conversations from the dashboard
    # Test Chat module skip the entire customer funnel (no contact
    # info, no delivery, no artwork). Inject a high-priority system
    # message right after the base prompt so DeepSeek sees it before
    # the channel/business-rule context.
    if conversation.is_test:
        messages.append({
            "role": "system",
            "content": (
                "## TEST MODE (sandbox)\n"
                "You are in a sandbox conversation from the dashboard's "
                "Test Chat module — this is NOT a real customer. The "
                "operator is testing your pricing logic.\n"
                "\n"
                "Skip the entire customer funnel:\n"
                "  - DO NOT ask 'do you have artwork or need design service?'\n"
                "  - DO NOT ask for name, email, phone, company, returning customer\n"
                "  - DO NOT ask about delivery / collection / address\n"
                "  - DO NOT emit [QUOTE_READY], [CUSTOMER_FORM], or "
                "    [ARTWORK_UPLOAD] markers\n"
                "\n"
                "Just answer pricing questions directly. When you have a "
                "product + quantity (and finish/double-sided if relevant), "
                "call the pricing tool with needs_artwork=false. State "
                "the inc-VAT total and any tier-combination breakdown "
                "clearly so the operator can verify it. If asked, explain "
                "your reasoning — this is a debugging session."
            ),
        })

    # v32.2 — server-injected context (e.g. [SENDER METADATA] +
    # [CUSTOMER STATUS] from the Missive handler). These appear BEFORE
    # the prior conversation history so the LLM can read them when
    # deciding what to call save_customer_info with.
    if extra_system_messages:
        for m in extra_system_messages:
            if isinstance(m, dict) and m.get("role") == "system" and m.get("content"):
                messages.append({"role": "system", "content": str(m["content"])})

    # If this is the very first turn of a fresh conversation, inject the
    # widget greeting as a prior assistant message. The widget shows this
    # greeting client-side the moment the customer opens the chat — without
    # feeding it into the LLM's context, DeepSeek opens its first reply with
    # another "Hey, Craig here…" and the customer sees two greetings back
    # to back.
    prior_messages = conversation.messages or []
    if not prior_messages:
        widget_greeting = _get_setting(
            db,
            "widget_greeting",
            default=None,
            organization_slug=organization_slug,
        )
        if widget_greeting:
            messages.append({"role": "assistant", "content": widget_greeting})

    for m in prior_messages:
        messages.append({"role": m["role"], "content": m["content"]})

    # Inject a lightweight summary of any quotes already on this thread.
    # Without this, the LLM re-quotes or re-asks for specs when the customer
    # replies "yes" to an earlier PDF \u2014 it literally has no way to know
    # anything was sent. With this, we tell it explicitly which JP-xxxx
    # references exist so it can route a confirmation into `confirm_order`.
    existing_quotes = (
        db.query(Quote)
        .filter_by(conversation_id=conversation.id)
        .order_by(Quote.created_at.desc())
        .all()
    ) if conversation.id else []
    if existing_quotes:
        summary_lines = ["[PRIOR QUOTES ALREADY SENT ON THIS THREAD]"]
        for q in existing_quotes:
            # v40.8 \u2014 prefer ex-VAT total + "+ VAT" wording for consistency
            # with the prompt rule. Fall back to inc-VAT on legacy rows.
            _ex = getattr(q, "final_price_ex_vat", None)
            _inc = q.final_price_inc_vat or 0.0
            if _ex:
                price_str = f"\u20ac{float(_ex):.2f} + VAT"
            else:
                price_str = f"\u20ac{float(_inc):.2f} inc VAT"
            summary_lines.append(
                f"- JP-{q.id:04d}: {q.product_key or 'custom'}, "
                f"{price_str}, status={q.status}"
            )
        summary_lines.append(
            "If the customer's message is a confirmation of one of these "
            "(e.g. 'yes', 'go ahead', 'confirmed', 'proceed', 'please print'), "
            "call confirm_order(quote_id=<id>) \u2014 do NOT re-quote and "
            "do NOT re-ask for product specs."
        )
        messages.append({"role": "system", "content": "\n".join(summary_lines)})

    messages.append({"role": "user", "content": user_message})

    # Pre-flight contact-info capture. If the customer typed an email or
    # phone number into this message, promote it into the Conversation row
    # immediately — independent of whether the LLM later calls
    # save_customer_info. This way the [QUOTE_READY] gate opens the same
    # turn the customer provides their info.
    sniffed_email, sniffed_phone = _sniff_contact_from_message(user_message)
    if sniffed_email and not (conversation.customer_email or "").strip():
        conversation.customer_email = sniffed_email
    if sniffed_phone and not (conversation.customer_phone or "").strip():
        conversation.customer_phone = sniffed_phone
    if sniffed_email or sniffed_phone:
        db.flush()

    # Phase F refined / G — sniff the customer's answer to the artwork
    # question. Phase G change: sniff runs EVERY turn (not just when
    # the field is None) so the customer can REVERSE their answer
    # ("wait, I have artwork" after previously saying "I need design").
    # Only definitive direct phrases ("have my own", "need design") can
    # override an existing value; bare yes/no requires an unanswered
    # state to fire (avoids reading "yes confirm specs" as artwork yes).
    last_asst = next(
        (m.get("content") for m in reversed(prior_messages)
         if m.get("role") == "assistant"),
        None,
    )
    sniffed_art = _sniff_artwork_answer(last_asst, user_message)
    if sniffed_art is not None:
        # Distinguish definitive (direct phrase) vs ambiguous (bare yes/no)
        # — only definitive signals can reverse a previously-set value.
        user_lower = (user_message or "").lower().strip()
        is_definitive = (
            any(p in user_lower for p in _ARTWORK_HAVE_AFFIRMATIVE)
            or any(p in user_lower for p in _ARTWORK_NEED_DESIGN)
            or any(p in user_lower for p in _ARTWORK_PENDING_LATER)
        )
        if conversation.customer_has_own_artwork is None or is_definitive:
            previous = conversation.customer_has_own_artwork
            if previous != sniffed_art:
                conversation.customer_has_own_artwork = sniffed_art
                db.flush()
                print(
                    f"[craig] artwork sniff: conv={conversation.id} "
                    f"customer_has_own_artwork={previous!r} -> {sniffed_art}",
                    flush=True,
                )

    # v30 — separately detect "I'll send it later" / "I haven't
    # finalised the artwork" / "I just need a price". These set the
    # artwork_will_send_later flag so the upload-first replace gate
    # and the [ARTWORK_UPLOAD] auto-emit gate skip — Craig gives the
    # verbal price + funnel like normal instead of looping on
    # "send your artwork over".
    if _sniff_artwork_pending_later(user_message):
        if not getattr(conversation, "artwork_will_send_later", False):
            conversation.artwork_will_send_later = True
            db.flush()
            print(
                f"[craig] artwork pending-later sniff: conv="
                f"{conversation.id} artwork_will_send_later -> True",
                flush=True,
            )

    # Inject artwork status as a developer-side hint so the LLM picks the
    # right needs_artwork on its next pricing tool call. Soft instruction
    # — DeepSeek can still get it wrong, but at least it has the signal.
    if conversation.customer_has_own_artwork is True:
        messages.append({
            "role": "system",
            "content": (
                "[INTERNAL] The customer has confirmed they have their own "
                "print-ready artwork. When you call a pricing tool, pass "
                "needs_artwork=false (no design line item). After quoting, "
                "end the reply with [ARTWORK_UPLOAD] on its own line so the "
                "widget renders the upload button."
            ),
        })
    elif conversation.customer_has_own_artwork is False:
        messages.append({
            "role": "system",
            "content": (
                "[INTERNAL] The customer wants the design service. When you "
                "call a pricing tool, pass needs_artwork=true and "
                "artwork_hours=1.0 — that's our standard one-hour design "
                "block at €65 ex VAT (€79.95 inc VAT). When you mention "
                "the price to the customer, always frame it as 'one hour "
                "of design'. Do NOT emit [ARTWORK_UPLOAD] (no upload "
                "needed; we're designing for them)."
            ),
        })

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    tool_calls_audit: list[dict] = []
    quote_generated = False
    escalated = False
    order_confirmed = False
    last_quote_id: int | None = None

    # v40.4 — build the tool schema with the live catalog enums for
    # this tenant. Done ONCE per chat_with_craig call (not once per
    # iteration of the tool-calling loop) so the LLM sees a stable
    # tool list during multi-turn tool calls.
    tools_for_org = _build_tools_for_org(db, organization_slug)

    # Tool-calling loop — LLM may call tools 0+ times before giving final answer
    for _ in range(5):  # safety cap
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            tools=tools_for_org,
            tool_choice="auto",
            temperature=0.3,
        )
        msg = response.choices[0].message

        # If no tool calls, we have the final reply
        if not msg.tool_calls:
            final_reply = msg.content or ""
            break

        # Append assistant message with tool calls
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        # Execute each tool and append the result
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = _exec_tool(
                db,
                tc.function.name,
                args,
                conversation_id=conversation.id,
                organization_slug=organization_slug,
            )
            tool_calls_audit.append({
                "tool": tc.function.name,
                "args": args,
                "result": result,
            })

            # Track outcomes
            if tc.function.name == "escalate_to_justin":
                # Only flip the flag if the server-side gate actually let
                # this go through. `escalated: False` in the result means
                # we rejected it for missing contact info and the LLM will
                # retry after running save_customer_info.
                if result.get("escalated"):
                    escalated = True
            elif tc.function.name == "confirm_order":
                if result.get("confirmed"):
                    order_confirmed = True
            elif result.get("success") and "final_price_ex_vat" in result:
                quote_generated = True
                # v26 — dedupe: DeepSeek often re-calls the pricing tool
                # multiple times in the same conversation (after the
                # customer uploads, after they say yes to "want full
                # quote?", etc) with IDENTICAL specs. Without dedupe
                # Justin sees 2-3 phantom rows in the dashboard for one
                # customer order. If a pending Quote already exists on
                # this conversation with the same product_key + specs,
                # reuse it (and just update the artwork_cost / total in
                # case the customer flipped between have-artwork and
                # design-service mid-conversation).
                _product_key = args.get("product_key") or (
                    f"booklet_{args.get('format')}_{args.get('binding')}"
                    if tc.function.name == "quote_booklet" else None
                )
                existing_match = (
                    db.query(Quote)
                    .filter_by(
                        conversation_id=conversation.id,
                        organization_slug=organization_slug,
                        product_key=_product_key,
                        status="pending_approval",
                    )
                    .all()
                )
                # Match on the spec subset that actually drives the
                # price (product, qty, sides, finish). needs_artwork is
                # NOT part of the match key — if the customer flipped,
                # the existing row is updated with the new artwork_cost.
                _spec_keys = ("product_key", "quantity", "double_sided", "finish",
                              "format", "binding", "pages", "cover_type")
                _match_sig = tuple(args.get(k) for k in _spec_keys)
                reused = None
                for cand in existing_match:
                    cand_sig = tuple((cand.specs or {}).get(k) for k in _spec_keys)
                    if cand_sig == _match_sig:
                        reused = cand
                        break

                if reused is not None:
                    # Update the price fields in case anything changed
                    # (artwork_cost flip, surcharge tweak, etc.) and
                    # preserve the artwork_files / shipping that may
                    # already be on the row.
                    reused.specs = args
                    reused.base_price = result["base_price"]
                    reused.surcharges = result["surcharges_applied"]
                    reused.final_price_ex_vat = result["final_price_ex_vat"]
                    reused.vat_amount = result["vat_amount"]
                    reused.final_price_inc_vat = result["final_price_inc_vat"]
                    reused.artwork_cost = result.get("artwork_cost_ex_vat") or 0.0
                    reused.total = result["total_inc_everything"]
                    db.flush()
                    last_quote_id = reused.id
                    print(
                        f"[craig] DEDUPE: reused existing pending Quote "
                        f"id={reused.id} on conv {conversation.id} "
                        f"instead of creating a duplicate row.",
                        flush=True,
                    )
                else:
                    q = Quote(
                        organization_slug=organization_slug,
                        conversation_id=conversation.id,
                        product_key=_product_key,
                        specs=args,
                        base_price=result["base_price"],
                        surcharges=result["surcharges_applied"],
                        final_price_ex_vat=result["final_price_ex_vat"],
                        vat_amount=result["vat_amount"],
                        final_price_inc_vat=result["final_price_inc_vat"],
                        artwork_cost=result.get("artwork_cost_ex_vat") or 0.0,
                        total=result["total_inc_everything"],
                        status="pending_approval",
                        # v35 — mirror the test flag from the conversation
                        is_test=bool(conversation.is_test),
                    )
                    db.add(q)
                    db.flush()  # get the ID
                    last_quote_id = q.id

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })
    else:
        final_reply = "Sorry, I'm having trouble getting that quote. Let me get Justin to come back to you."

    # Guardrail: scrub any markdown the LLM snuck in despite the prompt rules.
    # Done once, here, so both the persisted history and the API reply are clean.
    final_reply = _humanize_reply(final_reply)

    # ── Phase G: artwork-question isolation guard ─────────────────
    # v38 — guard RELAXED. Old behaviour: strip everything except the
    # artwork question when artwork hadn't been answered. That gave us
    # Bug 3 in production — customers said "PVC banner 1m x 2m" and
    # Craig asked for artwork BEFORE showing the price. 42% abandon
    # rate on the widget audit.
    #
    # New behaviour: only strip the FUSION when the reply has NO PRICE
    # AND has spec-recap (signs the LLM didn't actually run the pricing
    # tool but is asking the customer to confirm). When a real price
    # is in the reply (look for the € symbol), we KEEP everything —
    # price + artwork-question in one message is the v38 flow.
    _reply_has_price = "€" in final_reply or "EUR" in final_reply.upper()
    if (
        conversation.customer_has_own_artwork is None
        and "?" in final_reply
        and any(p in final_reply.lower() for p in ("artwork", "design service"))
        and not _reply_has_price  # v38 — let price-bearing replies pass
    ):
        # Detect fusion: the reply mentions artwork AND ALSO either
        # "full quote" or "to confirm" (spec recap). Trim everything
        # after the first artwork-question paragraph.
        lower = final_reply.lower()
        has_full_quote_ask = (
            "want me to put together" in lower
            or "want the full quote" in lower
            or "full quote" in lower and "?" in lower
        )
        # Spec-recap: "just to confirm — N business cards..." style
        has_spec_recap = (
            "just to confirm" in lower
            or "to confirm" in lower and any(
                w in lower for w in ("business cards", "flyers", "brochures",
                                     "letterheads", "ncr", "stationery",
                                     "single-sided", "double-sided", "matte",
                                     "gloss", "soft-touch")
            )
        )
        if has_full_quote_ask or has_spec_recap:
            # Find the artwork-question sentence and keep only that.
            # Strategy: split into paragraphs, keep the FIRST paragraph
            # that mentions "artwork" or "design service".
            paragraphs = [p.strip() for p in final_reply.split("\n\n") if p.strip()]
            kept: list[str] = []
            for p in paragraphs:
                if "artwork" in p.lower() or "design service" in p.lower():
                    kept.append(p)
                    break
            if kept:
                print(
                    f"[craig] ARTWORK ISOLATION GUARD: trimmed fused message "
                    f"on conv {conversation.id}. original_len={len(final_reply)} "
                    f"trimmed_len={len(kept[0])}",
                    flush=True,
                )
                final_reply = kept[0]

    # v38.7 — [ARTWORK_CHOICE] auto-emit. Restructured out of the
    # spec-recap trim block above, where it was previously nested
    # inside `if not _reply_has_price`. That nesting was the root
    # cause of the production bug: when the LLM's reply ALREADY had
    # a price (€), the outer block was skipped, the [ARTWORK_CHOICE]
    # marker never fired, and the downstream [ARTWORK_UPLOAD] gate
    # took over — trapping customers in an upload-only flow with no
    # "design service" / "send later" buttons. Symptom: customer
    # priced 100 vinyl labels at €13.84, widget showed only the
    # upload area and refused to submit the form. Reported May 13.
    #
    # New behaviour: fire [ARTWORK_CHOICE] whenever artwork hasn't
    # been answered, web channel, the marker isn't already present,
    # and EITHER (a) this turn has a price OR (b) a prior quote
    # exists. If neither, suppress (LLM is doing spec-confirm).
    if (
        (channel or "").lower() in ("web", "")
        and conversation.customer_has_own_artwork is None
        and "[ARTWORK_CHOICE]" not in final_reply
        and "[ARTWORK_UPLOAD]" not in final_reply
    ):
        # v38.3 — `_had_prior_quote` is defined later in this fn,
        # so compute the equivalent inline using `existing_quotes`.
        _any_quote_exists = bool(quote_generated or existing_quotes)
        if _reply_has_price:
            # Append the artwork-choice marker to the price reply
            # so the customer sees the number FIRST, then picks
            # how to proceed (own artwork / design service / later).
            final_reply = (
                final_reply.rstrip()
                + "\n\nQuick one before I wrap the full quote 👇 "
                "Do you have your own print-ready artwork, or would "
                "you like our design service (€65 ex VAT for one "
                "hour of design work)?\n\n[ARTWORK_CHOICE]"
            )
            print(
                f"[craig] APPENDED [ARTWORK_CHOICE] after price on conv "
                f"{conversation.id} (v38.7 — price-first flow).",
                flush=True,
            )
        elif _any_quote_exists:
            # A price was given in a prior turn — replace with the
            # canned artwork choice so the widget renders buttons.
            final_reply = (
                "Quick question before I price it 👇 Do you have your "
                "own print-ready artwork, or would you like our design "
                "service?\n\n[ARTWORK_CHOICE]"
            )
            print(
                f"[craig] EMITTED [ARTWORK_CHOICE] on conv "
                f"{conversation.id} (prior quote exists).",
                flush=True,
            )
        else:
            # No price yet anywhere in the conversation — don't
            # clobber whatever the LLM said. It's probably a spec
            # confirmation ("just to confirm: ... ?") which is
            # exactly what Rule 3 calls for. Let it through.
            print(
                f"[craig] SUPPRESSED [ARTWORK_CHOICE] auto-emit on conv "
                f"{conversation.id} — no price yet, letting LLM's "
                f"spec-confirm pass through (v38.7 — price-first flow).",
                flush=True,
            )

    # Hallucinated-quote gate.
    #
    # If the LLM emitted [QUOTE_READY] without any real Quote row existing
    # on this conversation, the figure in the reply is fabricated — there's
    # no PDF, no DB audit trail, nothing for Justin to approve. Strip the
    # marker and warn the customer the number isn't binding yet.
    #
    # Important: "any real Quote row" means either (a) a pricing tool ran
    # THIS turn (quote_generated=True), or (b) one ran on an earlier turn
    # of the same conversation. The web widget flow routinely emits
    # [QUOTE_READY] several turns AFTER the pricing call — e.g. the contact
    # -collection turn gates the PDF but reuses the Quote from the verbal
    # -price turn. `existing_quotes` was already queried at the top of
    # this function, before chat_with_craig ran any tools, so it reflects
    # quotes persisted in prior turns only — which is exactly what we want.
    _had_prior_quote = bool(existing_quotes)
    if "[QUOTE_READY]" in final_reply and not (quote_generated or _had_prior_quote):
        print(
            f"[craig] HALLUCINATED-QUOTE GUARD: stripped [QUOTE_READY] from "
            f"reply because no pricing tool has run on this conversation. "
            f"channel={channel!r} org={organization_slug!r}. "
            f"Reply head: {final_reply[:200]!r}",
            flush=True,
        )
        final_reply = final_reply.replace("[QUOTE_READY]", "").rstrip()
        final_reply += (
            "\n\n(Note: I need to run the exact numbers through our pricing "
            "sheet before I can commit to a figure \u2014 I'll get that over "
            "to you shortly.)"
        )

    # Hard gate for the PDF/order flow — only enforced for channels where
    # the customer can be anonymous (the web widget). Email/SMS/WhatsApp
    # inherently know who wrote in (the sender envelope), so gating on
    # customer_email would stop legitimate first-turn auto-quotes.
    _channel_needs_gate = (channel or "").lower() in ("web", "")
    _has_contact = bool(
        (conversation.customer_email or "").strip()
        or (conversation.customer_phone or "").strip()
    )
    # Phase E — also require the funnel to be complete. We treat
    # `delivery_method` as the canonical "funnel done" signal because it's
    # the LAST question Craig asks; if it's set, all earlier questions were
    # walked through. (is_company / is_returning_customer are useful signals
    # but optional — some customers won't know or won't say.)
    _funnel_complete = bool((conversation.delivery_method or "").strip())

    # Phase F: when funnel isn't complete, the ONLY way to collect
    # remaining info is the structured form. Track whether we've already
    # rendered it earlier so we don't double-render.
    _form_already_shown_earlier = any(
        "[CUSTOMER_FORM]" in (m.get("content") or "")
        for m in (conversation.messages or [])
        if m.get("role") == "assistant"
    )

    # Helper used by multiple gates below — returns True iff ANY quote
    # on this conversation has artwork files uploaded yet (array shape
    # OR legacy singular cols). v26 — broadened from "last quote only"
    # to "any quote": when DeepSeek calls the pricing tool a SECOND time
    # in the conversation (e.g. customer typed something new after
    # uploading), a fresh Quote row is created with no files. The
    # previous "last quote only" check then thought no artwork existed
    # and re-emitted the upload prompt, asking the customer to upload
    # again. By scanning all quotes on the conv we catch the upload
    # that landed on the FIRST quote and skip the duplicate prompt.
    def _quote_has_artwork_check() -> bool:
        rows = (
            db.query(Quote)
            .filter_by(conversation_id=conversation.id)
            .all()
        )
        for q in rows:
            if parse_artwork_files(q.artwork_files):
                return True
            if (q.artwork_file_url or "").strip():
                return True
        return False

    # Phase F refined — when LLM emits [QUOTE_READY] but prerequisites
    # aren't met, strip the marker, KEEP the verbal price the LLM wrote,
    # and append the right next-step. Three states:
    #   - artwork not answered     -> ask the artwork question
    #   - artwork ok, funnel open  -> emit [CUSTOMER_FORM]
    #   - funnel complete           -> let [QUOTE_READY] pass through
    _quote_ready_premature = (
        _channel_needs_gate
        and "[QUOTE_READY]" in final_reply
        and (
            not _has_contact
            or not _funnel_complete
            or conversation.customer_has_own_artwork is None
        )
    )
    if _quote_ready_premature:
        print(
            f"[craig] PREMATURE [QUOTE_READY] — preserving verbal price; "
            f"has_contact={_has_contact} funnel_complete={_funnel_complete} "
            f"artwork_answered={conversation.customer_has_own_artwork is not None} "
            f"conv={conversation.id}",
            flush=True,
        )
        kept = final_reply.replace("[QUOTE_READY]", "").strip()

        # v38 — Bug 2 fix. If the LLM emitted [QUOTE_READY] without a
        # price string in the reply (production-observed in conv 148:
        # LLM only sent the marker, no "€X for ..." sentence), fetch
        # the most recent Quote row on this conversation and prepend
        # a price sentence so the customer ALWAYS sees a number
        # before the contact form. Without this fix Craig replies with
        # only "[CUSTOMER_FORM]" and the customer abandons because
        # they don't know the price yet.
        if last_quote_id and ("€" not in kept and "EUR" not in kept.upper()):
            try:
                _q = db.query(Quote).filter_by(id=last_quote_id).first()
                # v40.8 — surface the ex-VAT total + "+ VAT" wording
                # (Irish B2B convention, replaces the legacy "inc VAT"
                # phrasing). Fall back to inc-VAT only if ex-VAT is unset
                # on a legacy Quote row.
                _price_ex = getattr(_q, "final_price_ex_vat", None) if _q else None
                _price_inc = getattr(_q, "final_price_inc_vat", None) if _q else None
                _price_display = _price_ex if _price_ex else _price_inc
                if _q is not None and _price_display:
                    _qty = (_q.specs or {}).get("quantity") if _q.specs else None
                    _prod = (_q.product_key or "").replace("_", " ")
                    qty_str = f"{int(_qty)} " if _qty else ""
                    _suffix = "+ VAT" if _price_ex else "inc VAT"
                    price_intro = (
                        f"That'll be €{float(_price_display):.2f} "
                        f"for {qty_str}{_prod} {_suffix} 👍"
                    )
                    if kept:
                        kept = price_intro + "\n\n" + kept
                    else:
                        kept = price_intro
                    print(
                        f"[craig] BUG-2 FIX: injected price for quote "
                        f"{last_quote_id} into premature reply on conv "
                        f"{conversation.id}.",
                        flush=True,
                    )
            except Exception as _e:
                print(
                    f"[craig] BUG-2 FIX: failed to fetch quote {last_quote_id} "
                    f"for price-injection: {_e}",
                    flush=True,
                )
        # v25 — figure out if the customer still needs to upload artwork
        # before the form. If they said "I have artwork" but haven't
        # uploaded any files yet, surface the upload button INSTEAD of
        # the customer-info form. The form goes out only after artwork
        # is in (or the customer chose the design service).
        _needs_upload_for_premature = (
            conversation.customer_has_own_artwork is True
            and last_quote_id is not None
            and not _quote_has_artwork_check()
            # v30 — customer chose "I'll send artwork later"; respect that
            and not bool(getattr(conversation, "artwork_will_send_later", False))
        )
        if conversation.customer_has_own_artwork is None:
            tail = (
                "Quick question before I put the full quote together: do "
                "you have print-ready artwork, or would you like our "
                "design service? It's €65 ex VAT (€79.95 inc VAT) for "
                "one hour of design work."
            )
        elif _needs_upload_for_premature:
            tail = (
                "Send your print-ready artwork over and I'll wrap up the "
                "full quote \U0001f447\n\n[ARTWORK_UPLOAD]"
            )
        elif _form_already_shown_earlier:
            tail = (
                "Still waiting on the form above so I can finalise the "
                "quote \U0001f44d"
            )
        else:
            tail = (
                "Just need a few more details to send the full quote \U0001f447\n\n"
                "[CUSTOMER_FORM]"
            )
        final_reply = (kept + "\n\n" + tail) if kept else tail
    elif False and _channel_needs_gate and "[QUOTE_READY]" in final_reply and not _has_contact:
        # Replace the ENTIRE reply with the contact ask. Keeping the LLM's
        # "Here's your quote! 📋" pre-text in front of the ask confused
        # customers — they saw two conflicting sentences in one message.
        # Phase F \u2014 fire the structured form instead of asking in chat.
        if _form_already_shown_earlier:
            final_reply = (
                "Still waiting on the form above so I can finalise the "
                "quote \U0001f44d"
            )
        else:
            final_reply = (
                "Just need a few more details before I send the full quote \ud83d\udc47\n\n"
                "[CUSTOMER_FORM]"
            )
    elif _channel_needs_gate and "[QUOTE_READY]" in final_reply and _has_contact and not _funnel_complete:
        # Phase E gate — contact present, but the funnel still has open
        # questions (company/individual? returning? delivery vs collect?).
        # Strip the marker so the PDF doesn't render, and append a
        # follow-up that asks ONLY the missing questions (capped at 2 per
        # turn so it doesn't feel like an interrogation).
        print(
            f"[craig] FUNNEL GATE: stripping [QUOTE_READY] — "
            f"delivery_method not set on conv {conversation.id}. "
            f"channel={channel!r}",
            flush=True,
        )
        final_reply = final_reply.replace("[QUOTE_READY]", "").rstrip()
        # Phase F — funnel collected via the structured form, not free text.
        if _form_already_shown_earlier:
            # Form was shown but funnel still incomplete — gentle nudge.
            final_reply = (
                (final_reply + "\n\n") if final_reply else ""
            ) + (
                "Still waiting on the form above to finalise things \U0001f44d"
            )
        else:
            final_reply = (
                "Just need a few more details before I send the full quote 👇\n\n"
                "[CUSTOMER_FORM]"
            )
    elif _channel_needs_gate and "[QUOTE_READY]" in final_reply and _has_contact:
        # The PDF is going out. Append a confirmation tail so the customer
        # knows Justin will follow up — unless the LLM already said it.
        tail = "\n\nWe'll be in touch shortly to confirm everything \U0001f44d"
        already_said = any(
            phrase in final_reply.lower()
            for phrase in ("follow up", "in touch", "be in touch", "get back to you")
        )
        if not already_said:
            # Put the tail AFTER the marker so the widget shows
            # "Here's your quote!" first, then the confirmation, then the card.
            final_reply = final_reply.replace("[QUOTE_READY]", f"[QUOTE_READY]{tail}")

    # ── Release the PDF gate when the LLM forgets ────────────────────────
    # Pattern: customer was asked for contact details (gate held the PDF),
    # they provided email/phone this turn, save_customer_info ran, but the
    # LLM closed with "you're all set!" and forgot to re-emit [QUOTE_READY].
    # Without the marker the widget never renders the PDF card. We detect
    # the situation server-side and auto-append the marker — belt-and-
    # suspenders over the prompt instruction.
    _save_contact_called = any(
        (tc.get("tool") or "").lower() == "save_customer_info"
        for tc in tool_calls_audit
    )
    _already_has_marker = "[QUOTE_READY]" in final_reply
    _pdf_already_released_earlier = any(
        "[QUOTE_READY]" in (m.get("content") or "")
        for m in (conversation.messages or [])
        if m.get("role") == "assistant"
    )
    if (
        _channel_needs_gate
        and not order_confirmed                  # don't fire on confirm_order paths
        and _save_contact_called                 # contact was JUST collected
        and _has_contact                         # …and persisted
        and _funnel_complete                     # …Phase E: ALL funnel fields collected too
        and _had_prior_quote                     # …and a Quote already exists on this thread
        and not _already_has_marker              # LLM didn't include the marker
        and not _pdf_already_released_earlier    # the PDF wasn't already sent in a prior turn
    ):
        print(
            f"[craig] AUTO-RELEASE: appending [QUOTE_READY] after "
            f"save_customer_info — LLM forgot to re-emit it. "
            f"channel={channel!r} org={organization_slug!r}",
            flush=True,
        )
        # Append on its own line so the visible reply text stays clean
        # (the marker itself is stripped by the widget before render).
        if not final_reply.endswith("\n"):
            final_reply += "\n"
        final_reply += "\n[QUOTE_READY]"

    # ── Phase F: auto-emit [ARTWORK_UPLOAD] when LLM forgets ──────────
    # v38.7 — TIGHTENED. Old behaviour: fired when customer_has_own_artwork
    # was NOT False (True OR None). The rationale was a safety fallback:
    # if the LLM priced without asking artwork, show the upload box so the
    # customer can either upload (auto-flips flag) or type "I want design".
    #
    # But the [ARTWORK_CHOICE] auto-emit above (v38.7) now ALWAYS fires
    # when artwork is unanswered + we have a price (3 buttons: have /
    # design / later). So this gate must ONLY fire for explicit True
    # (customer chose "I have artwork" → time to upload). When None,
    # the [ARTWORK_CHOICE] above wins and the customer gets buttons,
    # not a bare upload area. When False (design service), we don't
    # show the upload button at all.
    _pricing_called_this_turn = any(
        (tc.get("tool") or "").lower() in (
            "quote_small_format", "quote_large_format", "quote_booklet",
        )
        and (tc.get("result") or {}).get("success") is True
        for tc in tool_calls_audit
    )
    _quote_has_artwork = _quote_has_artwork_check()
    _upload_marker_already = "[ARTWORK_UPLOAD]" in final_reply
    _upload_marker_earlier = any(
        "[ARTWORK_UPLOAD]" in (m.get("content") or "")
        for m in (conversation.messages or [])
        if m.get("role") == "assistant"
    )
    # v26 — when pricing JUST ran AND the customer has their own artwork
    # AND no files uploaded yet, REPLACE Craig's verbal price with a
    # clean "send your artwork" prompt + the upload button. The price
    # comes AFTER the customer uploads (they get a synthetic chat turn
    # from the widget that triggers Craig's natural "perfect — that'll
    # be €X" reply). User explicit ask: the upload step should come
    # BEFORE the price, not bundled with it.
    # v30 — read the pending-later flag once. When True, Craig should
    # NOT push the upload card / replace the verbal price. Customer
    # explicitly opted to send artwork later; respect that choice.
    _artwork_pending_later = bool(getattr(conversation, "artwork_will_send_later", False))

    _upload_first_replace = (
        _channel_needs_gate
        and conversation.customer_has_own_artwork is True
        and _pricing_called_this_turn
        and not _quote_has_artwork
        and not _artwork_pending_later  # v30
    )
    if _upload_first_replace:
        print(
            f"[craig] UPLOAD-FIRST: replacing verbal price with upload "
            f"prompt on conv {conversation.id}. The price will surface "
            f"after the customer uploads their artwork.",
            flush=True,
        )
        final_reply = (
            "Got it 👍 send your print-ready artwork over and I'll "
            "wrap up the price 👇\n\n[ARTWORK_UPLOAD]"
        )
    elif (
        _channel_needs_gate
        and conversation.customer_has_own_artwork is True  # v38.7 — explicit only
        and _pricing_called_this_turn                       # quote just generated
        and not _quote_has_artwork                          # not uploaded yet
        and not _upload_marker_already
        and not _upload_marker_earlier
        and not _artwork_pending_later  # v30 — customer chose "later"
    ):
        print(
            f"[craig] AUTO-EMIT [ARTWORK_UPLOAD] — pricing ran with "
            f"needs_artwork=False on conv {conversation.id} but LLM "
            f"didn't render the upload button. channel={channel!r}",
            flush=True,
        )
        if not final_reply.endswith("\n"):
            final_reply += "\n"
        final_reply += "\n[ARTWORK_UPLOAD]"

    # ── Phase F: auto-emit [CUSTOMER_FORM] when user accepts full quote ──
    # Tightened in v24 — must satisfy ALL:
    #   - a Quote exists (price was given)
    #   - funnel incomplete
    #   - artwork question was answered (customer_has_own_artwork is set)
    #   - user message is a short affirmative
    #   - form not already shown
    #   - not on confirm_order path
    # The artwork-answered requirement prevents the form firing before
    # the customer sees the price (Craig should: ask artwork → price →
    # ask "want full quote?" → user yes → form).
    _last_user_msg = (user_message or "").strip().lower()
    _looks_affirmative = any(
        word in _last_user_msg
        for word in (
            "yes", "yeah", "yep", "yup", "ok", "okay", "sure",
            "go ahead", "go for it", "please", "do it", "send it",
        )
    ) and len(_last_user_msg) < 80  # short affirmatives only
    _form_marker_in_reply = "[CUSTOMER_FORM]" in final_reply
    _artwork_answered = conversation.customer_has_own_artwork is not None
    # v25 — if the customer has own artwork but hasn't uploaded yet, the
    # upload step MUST come before the customer-info form. Otherwise we
    # collect delivery/email before the artwork is in, and Justin won't
    # be able to send a draft with the artwork attached. Suppress the
    # CUSTOMER_FORM auto-emit in that case — the ARTWORK_UPLOAD gate
    # above (which already fired) will show the upload card, and the
    # widget's post-upload synthetic chat turn will move things along.
    _needs_upload_first = (
        conversation.customer_has_own_artwork is True
        and not _quote_has_artwork
        and not _artwork_pending_later  # v30 — customer chose "later"
    )
    if (
        _channel_needs_gate
        and (_had_prior_quote or last_quote_id is not None)
        and not _funnel_complete
        and _artwork_answered                  # gate requires explicit artwork answer
        and _looks_affirmative
        and not _form_marker_in_reply
        and not _form_already_shown_earlier
        and not order_confirmed
        and not _needs_upload_first            # v25: upload before form
    ):
        print(
            f"[craig] AUTO-EMIT [CUSTOMER_FORM] — user accepted full "
            f"quote but funnel incomplete on conv {conversation.id}. "
            f"channel={channel!r}",
            flush=True,
        )
        # Wholesale replace the LLM's free-text "what's your name and email"
        # with the form trigger — the form already says "we need a few more
        # details" so the LLM's version is redundant.
        final_reply = (
            "Just need a few more details before I send the full quote 👇\n\n"
            "[CUSTOMER_FORM]"
        )
    elif (
        _channel_needs_gate
        and _needs_upload_first
        and (_had_prior_quote or last_quote_id is not None)
        and _looks_affirmative
        and "[ARTWORK_UPLOAD]" not in final_reply
        and not _upload_marker_earlier
    ):
        # The user said "yes" expecting to move forward but their artwork
        # isn't uploaded yet. Replace the LLM's reply with a clean nudge
        # that surfaces the upload button, no [CUSTOMER_FORM].
        print(
            f"[craig] AUTO-EMIT [ARTWORK_UPLOAD] (upload-first override) "
            f"— user said yes but no artwork uploaded yet on conv "
            f"{conversation.id}. channel={channel!r}",
            flush=True,
        )
        final_reply = (
            "Send your print-ready artwork over and I'll wrap up the "
            "full quote 👇\n\n[ARTWORK_UPLOAD]"
        )

    # Always echo back the most recent quote_id so the widget can render
    # the PDF card even if it lost local state (e.g. a reload between
    # turns). When a tool ran THIS turn last_quote_id is already set;
    # otherwise fall back to the most recent existing quote on the
    # conversation.
    if last_quote_id is None and existing_quotes:
        # existing_quotes is ordered desc by created_at, so [0] is newest
        last_quote_id = existing_quotes[0].id

    # Persist the turn. Phase F: synthetic [SYSTEM] messages (sent by the
    # widget after a form submit to nudge Craig into emitting [QUOTE_READY])
    # are stored as `role="system"` so the dashboard transcript doesn't
    # render them as customer dialogue.
    history = list(conversation.messages or [])
    user_role = "system" if user_message.startswith("[SYSTEM]") else "user"
    history.append({"role": user_role, "content": user_message})
    history.append({"role": "assistant", "content": final_reply})
    conversation.messages = history

    # Conversation status transitions.
    # IMPORTANT: we only flip to "escalated" once we actually have the
    # customer's contact info. DeepSeek sometimes calls escalate_to_justin
    # the instant it decides a request is custom, BEFORE it has asked for
    # name / email / phone — which left the dashboard showing "escalated"
    # conversations with no one to contact. Staying "open" until
    # save_customer_info runs means Justin's queue only surfaces rows he
    # can actually act on. The raw `escalated` signal is still returned in
    # the API response so the widget can render its "escalated" toast.
    has_contact = bool(
        (conversation.customer_email or "").strip()
        or (conversation.customer_phone or "").strip()
    )
    if order_confirmed:
        # confirm_order already set the status to "order_placed" inside
        # _exec_tool(). We assert it here in case the LLM called
        # confirm_order more than once or mixed signals in the same turn.
        conversation.status = "order_placed"
    elif escalated and has_contact:
        conversation.status = "escalated"
    elif escalated and not has_contact:
        # Still collecting info — keep the conversation visible as an open thread.
        conversation.status = "awaiting_contact"
    elif quote_generated:
        conversation.status = "quoted"

    db.commit()

    # Phase G — surface the quote total + customer artwork status so
    # the widget can render the dynamic shipping label and show the
    # artwork upload state without an extra round trip.
    quote_total_inc_vat: float | None = None
    quote_product_key: str | None = None
    artwork_files_count = 0
    if last_quote_id is not None:
        latest_q = db.query(Quote).filter_by(id=last_quote_id).first()
        if latest_q is not None:
            try:
                quote_total_inc_vat = float(latest_q.final_price_inc_vat or 0)
            except Exception:
                quote_total_inc_vat = None
            quote_product_key = latest_q.product_key
            artwork_files_count = len(parse_artwork_files(getattr(latest_q, "artwork_files", None)))

    return {
        "reply": final_reply,
        "conversation_id": conversation.id,
        "quote_generated": quote_generated,
        "quote_id": last_quote_id,
        "quote_total_inc_vat": quote_total_inc_vat,
        # v40 — product key for the GTM quote_generated event payload.
        "product_key": quote_product_key,
        "artwork_files_count": artwork_files_count,
        "customer_has_own_artwork": getattr(conversation, "customer_has_own_artwork", None),
        "escalated": escalated,
        "order_confirmed": order_confirmed,
        "tool_calls": tool_calls_audit,
    }
