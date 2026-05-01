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
)
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
- ALWAYS confirm the specs back to the customer BEFORE calling the pricing tool, even if they gave everything upfront. Example: "Just to confirm — 500 business cards, single-sided, soft-touch finish?" Wait for them to say yes, THEN call the tool.
- This avoids quoting the wrong thing if you misunderstood their message.

## CRITICAL: How to present the price
- Give ONLY the total price (inc VAT). The customer just wants to know what they'll pay.
- DO NOT mention "ex VAT", "plus VAT", "before VAT", or show any VAT breakdown.
- Just say the total: "That'll be €46.74 for 500 business cards 👍"
- After giving the price, ALWAYS ask if they want the full quote: "Want me to put together the full quote for you? 📋"
- If they say yes, respond with EXACTLY this format (the widget will detect it): "Here's your quote! 📋 [QUOTE_READY]"
- Design service is a **flat one-time fee of €65 ex VAT (€79.95 inc VAT) per order** — it is NOT hourly, NOT per piece, NEVER use phrases like "per hour" or "/hr". Just say "€79.95 inc VAT" or "€65 + VAT" — flat fee for the whole job. When the customer confirms they want it, on the NEXT pricing tool call pass `needs_artwork=true, artwork_hours=1.0` — that's how we bill it through the engine. If they have print-ready artwork, omit both arguments (no design line item).
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
- "Nice one! That comes to €46.74 for 500 business cards 👍"
- "Let me check that for you 🔍"
- "That's one for Justin — I'll get him to come back to you on that 👍"
- "Single-sided or double-sided?"
- "What kind of finish are you after? Gloss, matte, or soft-touch?"
- "Hmm, that doesn't look quite right — could you double-check the email? 🤔"

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
    "i have", "i've got", "ive got", "got it", "got my own",
    "have my own", "my own", "yes i have", "yeah i have",
    "have artwork", "have the artwork", "have a design", "have one",
    "yes have", "got artwork", "ready", "print-ready",
)
_ARTWORK_NEED_DESIGN = (
    "need design", "need help", "design service", "design please",
    "no i don", "no, design", "design it", "make one", "create one",
    "i need", "no artwork", "don't have", "dont have",
    "no, i need", "can you design", "can you make",
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
    """
    from db.models import Product, PriceTier

    products = (
        db.query(Product)
        .filter_by(organization_slug=organization_slug)
        .order_by(Product.category, Product.key)
        .all()
    )
    if not products:
        return ""

    # Group by category
    by_cat: dict[str, list[Product]] = {}
    for p in products:
        by_cat.setdefault(p.category or "other", []).append(p)

    lines: list[str] = [
        "## Product catalog (live from database — the ONLY products/specs/quantities that exist)",
        "Do NOT ask about options not listed here. If the customer asks for something off-list, escalate.",
        "",
    ]

    for cat, items in by_cat.items():
        lines.append(f"### {cat.replace('_', ' ').title()}")
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
            if spec_keys:
                parts.append(f"  options: {', '.join(spec_keys)}")
            if qtys:
                parts.append(f"  quantities: {', '.join(str(q) for q in qtys)}")
            if p.notes:
                parts.append(f"  note: {p.notes}")
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
        "You are drafting a business email on behalf of Justin. You ARE Justin\n"
        "for this message. Do not mention Craig or AI. Do not use a chat voice.\n"
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
        "\n"
        "## Required structure of the reply\n"
        "1. One-line greeting: \"Hi <FirstName>,\" (from the sender envelope)\n"
        "2. One short paragraph stating the quoted total inc VAT and what it covers.\n"
        "3. One short paragraph saying the full branded quote is attached as a PDF\n"
        "   and inviting the customer to reply to confirm the order (or ask for\n"
        "   adjustments). Mention turnaround briefly (3-5 working days).\n"
        "4. Sign-off on its own lines exactly:\n"
        "       Best,\n"
        "       Justin\n"
        "       Just Print\n"
        "5. Then a final line on its own with just [QUOTE_READY] (server strips this).\n"
        "\n"
        "## Flow\n"
        "- If the email has product + quantity + required specs (finish, sides,\n"
        "  pages, cover, whatever the product needs), CALL THE PRICING TOOL\n"
        "  IMMEDIATELY. Do not ask whether they want a quote \u2014 they already\n"
        "  asked by writing to you. Produce the full reply as specified above,\n"
        "  ending with [QUOTE_READY] so the PDF gets attached.\n"
        "- If a spec is genuinely missing (e.g. customer said \"flyers\" with no\n"
        "  size or quantity), write a short email asking ONLY the missing field,\n"
        "  in one sentence, signed off as above. No [QUOTE_READY].\n"
        "- NEVER ask for name, email, or phone \u2014 we already have them from\n"
        "  the envelope.\n"
        "\n"
        "## Example \u2014 first contact, full specs (this is the exact voice to use)\n"
        "Input: \"I need 500 business cards, soft-touch, double-sided\"\n"
        "\n"
        "Before composing ANY reply you MUST call:\n"
        "    quote_small_format(product_key=\"business_cards\", quantity=500,\n"
        "                       double_sided=true, finish=\"soft-touch\")\n"
        "Take the `final_price_inc_vat` the tool returns. THEN write:\n"
        "\n"
        "Hi Juan,\n"
        "\n"
        "Thanks for reaching out. For 500 business cards with a soft-touch finish,\n"
        "double-sided, the total comes to \u20ac<final_price_inc_vat from tool> including VAT.\n"
        "\n"
        "I've attached the full branded quote as a PDF for your records. Turnaround\n"
        "is 3-5 working days from when we have print-ready artwork. Reply to this\n"
        "email to confirm the order or if you'd like any adjustments.\n"
        "\n"
        "Best,\n"
        "Justin\n"
        "Just Print\n"
        "\n"
        "[QUOTE_READY]\n"
        "\n"
        "## Order confirmation mode (when PRIOR QUOTES exist on this thread)\n"
        "If the system injected a [PRIOR QUOTES ALREADY SENT ON THIS THREAD]\n"
        "section listing JP-xxxx references, and the customer's latest message\n"
        "is a confirmation \u2014 \"yes\", \"go ahead\", \"confirmed\", \"proceed\",\n"
        "\"please print\", \"perfect, do it\", etc. \u2014 you MUST:\n"
        "  1. Call confirm_order(quote_id=<the integer from the JP-xxxx ref>).\n"
        "  2. Reply with a short confirmation. Do NOT re-quote. Do NOT attach\n"
        "     another PDF (no [QUOTE_READY]).\n"
        "  3. Invite them to reply with delivery details or artwork.\n"
        "\n"
        "## Example \u2014 order confirmation\n"
        "Prior quote in thread: JP-0018, 500 business_cards, \u20ac269.56, status=pending_approval\n"
        "Input: \"Yes, please go ahead\"\n"
        "Good reply:\n"
        "Hi Juan,\n"
        "\n"
        "Perfect, your order for JP-0018 (500 business cards, soft-touch,\n"
        "double-sided, \u20ac269.56 including VAT) is confirmed.\n"
        "\n"
        "Please send through your print-ready artwork when it's ready, or\n"
        "reply with the delivery address if you haven't shared one yet.\n"
        "We'll get everything moving on our side and be in touch with a\n"
        "production timeline.\n"
        "\n"
        "Best,\n"
        "Justin\n"
        "Just Print\n"
        "############################################################\n"
    ),
    "web": (
        "# CURRENT CHANNEL: WEB CHAT\n"
        "Replies render in a floating chat widget. Keep them short (2-3\n"
        "sentences). Emojis are fine and expected. Follow the personality\n"
        "tone above.\n"
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
                "compliment slips, letterheads, NCR pads). Returns the exact price from Justin's sheet "
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
                            "ncr_pads_a5", "ncr_pads_a4",
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
                "vinyl labels). Applies unit or bulk pricing based on quantity."
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
                        "description": "Number of units or square metres (for per-sq/m products).",
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


# =============================================================================
# TOOL EXECUTION
# =============================================================================


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
        # Phase F refined — pricing tool guard. Refuse to price until the
        # artwork question has been answered. Forces the LLM to ask the
        # customer "do you have artwork or need design service?" first,
        # since DeepSeek tends to skip that step otherwise. The widget
        # then sniffs the answer and stamps the conversation flag, which
        # unlocks pricing.
        if name in ("quote_small_format", "quote_large_format", "quote_booklet"):
            if conversation_id is not None:
                _conv = db.query(Conversation).filter_by(id=conversation_id).first()
                if (
                    _conv is not None
                    and _conv.customer_has_own_artwork is None
                    and not bool(args.get("needs_artwork"))
                ):
                    return {
                        "success": False,
                        "escalate": False,
                        "error": (
                            "ARTWORK_QUESTION_REQUIRED: Before quoting, ask "
                            "the customer this exact question: 'Do you have "
                            "print-ready artwork, or would you like our "
                            "design service? It's a flat €65 ex VAT (€79.95 "
                            "inc VAT) one-time fee per order — NOT per "
                            "hour.'. Wait for their answer, then call the "
                            "pricing tool with the appropriate needs_artwork "
                            "value. Do NOT proceed to quoting until they've "
                            "answered. NEVER tell the customer the design "
                            "service is hourly — it's a flat per-order fee."
                        ),
                        "needs_artwork_question": True,
                    }

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
            return result.to_dict()

        if name == "quote_large_format":
            result = quote_large_format(
                db,
                product_key=args["product_key"],
                quantity=int(args["quantity"]),
                needs_artwork=bool(args.get("needs_artwork", False)),
                artwork_hours=float(args.get("artwork_hours", 0.0)),
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
            if conversation_id:
                conv = db.query(Conversation).filter_by(id=conversation_id).first()
                if conv:
                    if (args.get("name") or "").strip():
                        conv.customer_name = args["name"].strip()
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
                    if (args.get("past_customer_email") or "").strip():
                        conv.past_customer_email = args["past_customer_email"].strip()
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
) -> dict:
    """
    Main entry point. Handles one turn of conversation.

    `organization_slug` scopes everything:
      - the system prompt is loaded from the Setting table for that tenant
        (falls back to the hardcoded CRAIG_SYSTEM_PROMPT if not found)
      - every tool call hits the pricing engine with that tenant's data
      - the Conversation + Quote records are tagged with that tenant

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
            )
            db.add(conversation)
            db.flush()
    else:
        conversation = Conversation(
            organization_slug=organization_slug,
            external_id=external_id, channel=channel, messages=[],
        )
        db.add(conversation)
        db.flush()

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
            price = q.final_price_inc_vat or 0.0
            summary_lines.append(
                f"- JP-{q.id:04d}: {q.product_key or 'custom'}, "
                f"\u20ac{price:.2f} inc VAT, status={q.status}"
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

    # Phase F refined — sniff the customer's answer to the artwork
    # question if they're answering it. Only sets the field if the
    # signal is unambiguous. The result is also surfaced as a soft
    # system hint to the LLM below so it picks the right needs_artwork
    # value when it next calls a pricing tool.
    if conversation.customer_has_own_artwork is None:
        last_asst = next(
            (m.get("content") for m in reversed(prior_messages)
             if m.get("role") == "assistant"),
            None,
        )
        sniffed_art = _sniff_artwork_answer(last_asst, user_message)
        if sniffed_art is not None:
            conversation.customer_has_own_artwork = sniffed_art
            db.flush()
            print(
                f"[craig] artwork sniff: conv={conversation.id} "
                f"customer_has_own_artwork={sniffed_art}",
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
                "artwork_hours=1.0 — that's the standard flat €65 ex VAT "
                "design service. Do NOT emit [ARTWORK_UPLOAD] (no upload "
                "needed; we're designing for them)."
            ),
        })

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    tool_calls_audit: list[dict] = []
    quote_generated = False
    escalated = False
    order_confirmed = False
    last_quote_id: int | None = None

    # Tool-calling loop — LLM may call tools 0+ times before giving final answer
    for _ in range(5):  # safety cap
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            tools=TOOLS,
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
                # Save the quote to DB
                q = Quote(
                    organization_slug=organization_slug,
                    conversation_id=conversation.id,
                    product_key=args.get("product_key") or (
                        f"booklet_{args.get('format')}_{args.get('binding')}"
                        if tc.function.name == "quote_booklet" else None
                    ),
                    specs=args,
                    base_price=result["base_price"],
                    surcharges=result["surcharges_applied"],
                    final_price_ex_vat=result["final_price_ex_vat"],
                    vat_amount=result["vat_amount"],
                    final_price_inc_vat=result["final_price_inc_vat"],
                    artwork_cost=result.get("artwork_cost_ex_vat") or 0.0,
                    total=result["total_inc_everything"],
                    status="pending_approval",
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
    # When the artwork question hasn't been answered yet, Craig's reply
    # MUST be ONLY the artwork question — no spec recap, no "want full
    # quote?", no price. DeepSeek frequently bundles them despite the
    # business_rules. We detect the fusion and surgically strip the
    # extra sentences so the customer sees ONLY the artwork question
    # this turn.
    if (
        conversation.customer_has_own_artwork is None
        and "?" in final_reply
        and any(p in final_reply.lower() for p in ("artwork", "design service"))
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
        if conversation.customer_has_own_artwork is None:
            tail = (
                "Quick question before I put the full quote together: do "
                "you have print-ready artwork, or would you like our "
                "design service? It's a flat €65 ex VAT (€79.95 inc VAT) "
                "one-time fee per order."
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
    # Tightened in v24 — we ONLY fire this when the customer EXPLICITLY
    # said they have own artwork (server-side sniff stamped
    # customer_has_own_artwork=True). The previous gate fired on the
    # default needs_artwork=False, which made the upload button appear
    # on every quote even when the customer hadn't been asked.
    _pricing_called_this_turn = any(
        (tc.get("tool") or "").lower() in (
            "quote_small_format", "quote_large_format", "quote_booklet",
        )
        and (tc.get("result") or {}).get("success") is True
        for tc in tool_calls_audit
    )
    _quote_has_artwork = bool(
        last_quote_id is not None
        and (
            db.query(Quote)
            .filter_by(id=last_quote_id)
            .first()
            .artwork_file_url
            if last_quote_id is not None else False
        )
    ) if last_quote_id is not None else False
    _upload_marker_already = "[ARTWORK_UPLOAD]" in final_reply
    _upload_marker_earlier = any(
        "[ARTWORK_UPLOAD]" in (m.get("content") or "")
        for m in (conversation.messages or [])
        if m.get("role") == "assistant"
    )
    if (
        _channel_needs_gate
        and conversation.customer_has_own_artwork is True   # explicit signal
        and _pricing_called_this_turn                       # quote just generated
        and not _quote_has_artwork                          # not uploaded yet
        and not _upload_marker_already
        and not _upload_marker_earlier
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
    if (
        _channel_needs_gate
        and (_had_prior_quote or last_quote_id is not None)
        and not _funnel_complete
        and _artwork_answered                  # NEW: gate requires explicit artwork answer
        and _looks_affirmative
        and not _form_marker_in_reply
        and not _form_already_shown_earlier
        and not order_confirmed
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
    artwork_files_count = 0
    if last_quote_id is not None:
        latest_q = db.query(Quote).filter_by(id=last_quote_id).first()
        if latest_q is not None:
            try:
                quote_total_inc_vat = float(latest_q.final_price_inc_vat or 0)
            except Exception:
                quote_total_inc_vat = None
            artwork_files_count = len(getattr(latest_q, "artwork_files", None) or [])

    return {
        "reply": final_reply,
        "conversation_id": conversation.id,
        "quote_generated": quote_generated,
        "quote_id": last_quote_id,
        "quote_total_inc_vat": quote_total_inc_vat,
        "artwork_files_count": artwork_files_count,
        "customer_has_own_artwork": getattr(conversation, "customer_has_own_artwork", None),
        "escalated": escalated,
        "order_confirmed": order_confirmed,
        "tool_calls": tool_calls_audit,
    }
