# Craig — AI Quoting Agent for Just Print

Read this first. This file is the handoff context for any new Claude Code session.

---

## What this project is

**Client:** Justin Byrne — Just-Print.ie (Irish print shop, Dublin)
**Agency:** Strategos AI — Roi (owner) + JS (builder)
**Status:** MVP. Not production. Custom-built microservice — NOT a GHL build.

Craig is a custom AI quoting agent for Just Print's customers. It lives as a standalone microservice (this repo) and fronts a website chat widget + quick-quote form. Future channels (WhatsApp, email) will connect directly to this same backend. **We are deliberately not using GHL here — this custom build is the reason the project exists.**

It uses a structured pricing database plus DeepSeek with tool-calling for natural conversation.

**Commercial:** €2,500 paid upfront, €1,400 on delivery. Monthly plan TBD.

---

## The golden rules — NEVER break these

1. **Craig NEVER invents a price.** Every quote comes from the DB via the pricing engine.
2. **If the product/quantity/spec isn't on the sheet → escalate.** Don't guess, don't approximate.
3. **Every quote is saved with `status="pending_approval"`.** Justin reviews before the customer ever sees it.
4. **The DB is temporary.** Real prices will eventually come from PrintLogic API — we're waiting on Alexander (Wildcard) to build the pricing endpoints. Until then, the JSON/DB is our source of truth.
5. **Keep the pricing engine and the LLM decoupled.** The LLM handles conversation; the engine owns prices. Never let the LLM compute a price directly.

---

## Architecture

```
Customer (widget / email / WhatsApp)
        ↓
   FastAPI /chat endpoint
        ↓
   DeepSeek LLM (tool-calling)
        ├── Handles natural conversation
        ├── Calls pricing tools when it has all specs
        └── Never touches prices directly
        ↓
   Pricing Engine (pure Python)
        ├── Reads from SQLite
        ├── Applies surcharge rules
        └── Returns exact price OR escalation
        ↓
   SQLite DB (craig.db)
        ├── products, price_tiers, aliases
        ├── surcharge_rules, settings
        └── conversations, quotes
```

**LLM flow:** Every customer message goes to DeepSeek with Craig's system prompt + tool definitions. DeepSeek decides when it has enough info, calls `quote_small_format`, `quote_large_format`, `quote_booklet`, `list_products`, or `escalate_to_justin`. We execute the tool against the DB and feed the result back. DeepSeek then generates the natural-language reply.

---

## File layout

```
craig-pricing-service/
├── app.py                        # FastAPI app (entry point)
├── pricing_engine.py             # Pure pricing logic, reads from SQLite
├── db/
│   ├── __init__.py               # SQLAlchemy engine + session helpers
│   └── models.py                 # 7 tables (see schema below)
├── llm/
│   └── craig_agent.py            # DeepSeek tool-calling + Craig system prompt
├── static/
│   ├── index.html                # Preview page mocking just-print.ie + widget
│   └── widget.js                 # Embeddable floating widget (chat + form tabs)
├── data/                         # Justin's pricing (human-editable JSON)
│   ├── small_format.json         # 10 products × 5 qty tiers
│   ├── large_format.json         # 12 products, unit + bulk pricing
│   ├── booklets.json             # A5/A4 × saddle/perfect × pages × covers
│   └── rules.json                # Surcharges, VAT rate, turnaround, POA items
├── scripts/
│   └── migrate_json_to_db.py     # JSON → SQLite bootstrap (run once)
├── test_pricing.py               # 31 tests verifying prices match spreadsheets
├── demo.ipynb                    # Direct API endpoint demo
├── demo_extractor.ipynb          # Legacy fuzzy-matching extractor demo
├── extractor.py                  # Legacy fuzzy matcher (kept for alias data)
├── main.py                       # Legacy v1 API (superseded by app.py, kept passing tests)
├── .env.example                  # Env template (DEEPSEEK_API_KEY)
├── requirements.txt
├── README.md                     # Human-facing setup docs
└── CLAUDE.md                     # THIS FILE
```

---

## Database schema (SQLite, migratable to Postgres)

- **products** — 26 rows. Small format (10), large format (12), booklets (4 variants — a5_saddle_stitch, a5_perfect_bound, a4_saddle_stitch, a4_perfect_bound).
- **price_tiers** — 660 rows. For small format: `{product_id, quantity, price}`. For booklets: spec_key encodes `"{pages}pp|{cover_type}"`.
- **product_aliases** — 180 rows. Free-text synonyms → product_key (e.g. "biz cards" → `business_cards`).
- **surcharge_rules** — 3 rows: `double_sided` (+20%), `soft_touch` (+25%), `triplicate` (+10%).
- **settings** — 4 rows: `vat_rate` (0.23), `artwork_rate_eur` (65.0), `standard_turnaround`, `poa_items`.
- **conversations** — persisted chat history, one row per customer session.
- **quotes** — every quote Craig produces, with `status="pending_approval"` until Justin approves.

---

## Pricing rules (Justin confirmed, April 10 2026)

| Rule | Detail |
|------|--------|
| Prices | Retail, all quoted directly to customer, **ex VAT** |
| Double-sided | +20% on base price (except business cards — no extra charge) |
| Soft-touch finish | +25% on any product |
| Both stacked | base × 1.20 × 1.25 (surcharges multiply, don't add) |
| NCR triplicate | +10% on base (NCR only) |
| Artwork | €65 + VAT per hour, quoted separately, never bundled |
| VAT | Irish 23% |
| Turnaround | 3-5 working days standard |
| POA items | Z-fold, die-cut labels, installation, rush jobs, custom sizes → escalate |

---

## Quick start (for Claude Code sessions)

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Env
cp .env.example .env
# Paste DEEPSEEK_API_KEY into .env

# 3. Build the DB from JSON (run this anytime JSON changes)
python scripts/migrate_json_to_db.py

# 4. Run
uvicorn app:app --reload
# Open http://localhost:8000 — preview page with widget loads bottom-right
```

**Known SQLite gotcha:** If the project lives on a cloud-synced folder (iCloud, Dropbox, OneDrive), SQLite throws "disk I/O error". Fix: `export CRAIG_DB_PATH=/tmp/craig.db` or `~/craig.db`.

**Tests:**
```bash
pytest test_pricing.py -v
# 31 passing — verifies every surcharge combo against Justin's sheet
```

---

## What's been built ✅

### Pricing + conversation core
- [x] FastAPI microservice with `/chat`, `/quote/*`, `/products`, `/conversations`, `/quotes/:id/pdf` endpoints
- [x] Pricing engine with surcharge logic, tenant-scoped (31/31 tests passing, prices cross-verified)
- [x] DeepSeek tool-calling integration — tools: `quote_small_format`, `quote_large_format`, `quote_booklet`, `list_products`, `save_customer_info`, `escalate_to_justin`, `confirm_order`
- [x] Channel-aware system prompt (web chat vs. email override with few-shot example)
- [x] Server-side gates: `[QUOTE_READY]` for the PDF flow (web), `escalate_to_justin` refuses without contact info, `confirm_order` rejects cross-conversation quote IDs
- [x] Prior-quote injection — LLM sees a `[PRIOR QUOTES ALREADY SENT ON THIS THREAD]` system message so it can route customer confirmations to `confirm_order` instead of re-quoting
- [x] Branded PDF quote generator (`pdf_generator.py`)

### Multi-tenancy + deployment
- [x] V2–V9 idempotent migrations (see README) — every table scoped by `organization_slug`
- [x] Cloud SQL Postgres backend (migrated from SQLite; same ORM, different connection string)
- [x] Cloud Run production deploy with `--min-instances=1 --no-cpu-throttling` so Missive background tasks complete
- [x] Tenant settings all live in `Setting` table — dashboard edits take effect next turn

### Channels
- [x] Web chat widget — branded per tenant via `/widget-config?client=<slug>`, embed-ready at `/widget.js`
- [x] Missive email integration — webhook in, HMAC-verified, draft out with PDF attached. Full docs: [`docs/missive-integration.md`](./docs/missive-integration.md).

### Admin API
- [x] `admin_api.py` — JWT-authenticated CRUD for products, tiers, surcharges, tax rates, categories, quotes, conversations, settings, metrics
- [x] Upsert semantics on settings PATCH so dashboard can create new keys without a schema migration

## What's NOT done ❌

### Blockers (need Roi / Justin)
- PrintLogic pricing API (waiting on Alexander from Wildcard) — still not blocking MVP
- 5 small open questions about the pricing sheet — still not blocking MVP

### Phase order (explicitly confirmed by the user)

**Step 1 — Prove it works locally (widget + form)**
Both the chat widget and the Quick Quote form must return real prices from the DB and route to DeepSeek cleanly. This is nearly done — just needs a real DeepSeek API key plugged in and a live test.

**Step 2 — Deploy to Google Cloud**
Target: Google Cloud Run (containerized FastAPI, pay-per-request, free tier). Alternative: GCE or App Engine. Do NOT use Railway or Render.

Must include:
- Dockerfile for the FastAPI app
- Cloud Build or gcloud run deploy command
- Managed SSL (Cloud Run gives this by default)
- Env vars set as Cloud Run secrets (DEEPSEEK_API_KEY in particular)
- DB strategy: SQLite works for single-instance; if scaling, migrate to Cloud SQL (Postgres). Schema is already Postgres-compatible.

**Step 3 — Install widget on just-print.ie**
WordPress backoffice at `/justprint_20200712_backoffice`. Inject `<script src="https://<cloud-run-url>/widget.js" defer></script>` on relevant pages.

**Step 4 — WhatsApp integration (direct, no GHL)**
Connect WhatsApp Business API directly to this microservice. Options:
- WhatsApp Cloud API (Meta's direct offering, free tier for testing)
- Twilio WhatsApp (easier setup, paid per message)
New endpoint needed: `/webhook/whatsapp` that receives incoming messages, passes them through the existing `/chat` logic, and replies via the WhatsApp API.

**Step 5 — Email integration (Missive)**
Connect Missive API to receive inbound quote emails, run them through `/chat` logic, and post drafted replies back to Missive for Justin's approval.

**Step 6 — PrintLogic order creation**
When a customer accepts a quote, `POST` the order to PrintLogic's `create_order` endpoint. API docs in `Printlogic API-2.pdf`. Auth via `api_key=GA5PQHGaxDl3IJJVuIEZpard9OgCyPOFmegd4W4K` query param.

**Step 7 — Supervised launch + polish**
Craig runs in approval mode (Justin sees every quote before customer does). Tune prompts and escalation rules based on real customer interactions.

---

## Open questions (not blocking MVP, collect later)

1. Business cards: does "double-sided no extra charge" exception still stand, or move to +20% like everything else?
2. Brochures A4 qty 1,000 shows €26 (lower than 2,500's €48). Likely sheet error. Justin to verify.
3. Large format per-sq/m products: should Craig calculate area from customer dimensions, or collect and escalate?
4. OnPrintShop (separate admin at just-print.onprintshop.com) vs PrintLogic — which is the primary order destination?
5. Missive password confirmation.

None of these block development. Build the MVP assuming current behavior; Craig escalates when unsure.

---

## Craig's personality (don't drift from this)

Casual, helpful, specifically Irish-market professional. Transparent about being AI on the first message, doesn't keep repeating it. Replies are short and clear — no corporate fluff. Always mentions "Justin will double-check before we run anything" after giving a price.

**Opening line style — DON'T drift into generic:**
- ❌ "Hey! I'm Craig, your AI assistant. How can I help you today?"
- ✅ "Hey — Craig here, I handle pricing for Just Print. What are you looking to print?"
- ✅ "Hi, Craig here. I can get you a quick price — what do you need?"

Full system prompt lives in `llm/craig_agent.py` — edit there, not elsewhere.

---

## Credentials (sensitive — never commit to git)

Stored in the original `credentials.md` that Justin sent. Summary:
- **PrintLogic API key:** `GA5PQHGaxDl3IJJVuIEZpard9OgCyPOFmegd4W4K`
- **OnPrintShop admin:** `just-print.onprintshop.com/admin` — `Just Print Admin` / `N1mda123`
- **Missive:** `info@just-print.ie` / `Hjk379bm!?`
- **WordPress:** `just-print.ie/justprint_20200712_backoffice` — `ninja_admin` / `dHTPPU%rJCSQX&V2KBrzyR0O`

Add DeepSeek key to `.env` locally. Never commit `.env`.

---

## Coding conventions

- Python 3.10+, type hints where useful, not religious about them
- FastAPI for HTTP, SQLAlchemy 2.0 for DB, Pydantic v2 for validation
- No ORM migrations tooling yet — `init_db()` creates tables on startup, run `migrate_json_to_db.py` to populate
- Keep the pricing engine **pure** — no HTTP, no LLM concerns, just functions + DB session
- Keep the LLM layer **thin** — system prompt + tool definitions + one chat loop, that's it
- Widget is vanilla JS, no build step, no bundler. Keep it that way.

---

## When in doubt

- Pricing logic changes → edit JSON in `data/`, re-run `migrate_json_to_db.py`, run `pytest`
- Craig behavior changes → edit system prompt in `llm/craig_agent.py`
- Widget look changes → `static/widget.js` (all styles + markup inside)
- New endpoints → `app.py`
- New DB tables or columns → `db/models.py` + drop and rebuild the DB (it's fine, prices are in JSON)

**Escalate to the human (JS / Roi) when:**
- Pricing behavior changes that aren't on the sheet
- Anything client-facing (copy, tone, escalation wording)
- Payment / commercial questions
- Cloud infrastructure choices (project ID, billing, region)

---

## Deployment target: Google Cloud Run

We are using **Google Cloud** for deployment — not Railway, not Render.

**Preferred service:** Cloud Run (container-based, fully managed, scales to zero, free tier covers MVP traffic).

**What's needed (doesn't exist yet — build it when we get to Step 2):**
- `Dockerfile` at project root — FastAPI + uvicorn + all deps
- `.dockerignore` — exclude `craig.db`, `.env`, `preview_*.png`, `__pycache__`, `.cache`
- Cloud Run deployment script / gcloud commands documented
- Secret Manager entry for `DEEPSEEK_API_KEY`
- Initial deploy runs `scripts/migrate_json_to_db.py` at container startup (or bake the DB into the image for MVP)
- Cloud Run service URL used in the widget embed script for just-print.ie

**DB scaling path:** SQLite is fine on a single Cloud Run instance for MVP. If we hit concurrency issues or need multi-instance, migrate to Cloud SQL (Postgres) — the schema is already Postgres-compatible, just swap `CRAIG_DATABASE_URL`.

---

*Last updated: April 15, 2026 — MVP checkpoint (scope updated: no GHL, deploy to Google Cloud)*
