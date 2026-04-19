# Craig — AI quoting agent

Craig is a production-grade conversational quoting agent, built for
Just Print (an Irish print shop) as the first tenant on a multi-tenant
platform. Customers talk to Craig via an embeddable web widget or by
emailing Missive, and Craig replies with accurate prices pulled from a
tenant-scoped pricing database — never invented by the LLM.

Every quote is saved as `pending_approval` and surfaces in the Strategos
dashboard for human review before any money moves.

---

## Architecture

```
                       ┌──────── Customers ────────┐
                       │                           │
                       ▼                           ▼
                  Web widget              info@just-print.ie
                (just-print.ie)             (via Missive)
                       │                           │
                       │ POST /chat                │ POST webhook
                       │                           ▼
                       │                    Missive rule (HMAC)
                       │                           │
                       ▼                           ▼
        ┌──────────────────── FastAPI (Cloud Run) ────────────────────┐
        │                                                             │
        │  /chat    /webhook/missive/:org    /quotes/:id/pdf          │
        │     │              │                      │                 │
        │     └──────┬───────┘                      │                 │
        │            ▼                              │                 │
        │    chat_with_craig()                      │                 │
        │      │                                    │                 │
        │      ├─ build system prompt (channel-aware)                 │
        │      │    = OVERRIDE + personality + catalog + rules        │
        │      ├─ DeepSeek tool-calling loop                          │
        │      │    tools: quote_small_format / quote_large_format /  │
        │      │           quote_booklet / list_products /            │
        │      │           save_customer_info / escalate_to_justin /  │
        │      │           confirm_order                              │
        │      ├─ pricing_engine.py — pure DB queries                 │
        │      └─ save Conversation + Quote rows                      │
        │                                                             │
        └─────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
                      Cloud SQL (Postgres) — multi-tenant,
                      every table scoped by organization_slug.
```

**Golden rule:** the LLM never touches a price directly. Pricing tools
read only from the DB; the system prompt ships the live catalog on
every turn so Craig knows what finishes / quantities / bindings are
even valid.

---

## Key features

| Feature | Where it lives |
|---|---|
| Multi-tenant pricing engine (catalog, tiers, surcharges, tax) | `pricing_engine.py`, `db/models.py` |
| DeepSeek tool-calling orchestration | `llm/craig_agent.py::chat_with_craig` |
| Channel-aware system prompt (web chat vs email) | `llm/craig_agent.py::_CHANNEL_CONTEXT` |
| Web chat widget (embeddable, per-tenant branded) | `static/widget.js`, `/widget-config` endpoint |
| Missive email integration (webhook in, draft out with PDF) | `missive.py`, `app.py::missive_webhook` |
| Contact-gate on `[QUOTE_READY]` + `escalate_to_justin` | `llm/craig_agent.py` |
| Order confirmation (customer replies "yes" → flips quote + conv status) | `confirm_order` tool in `llm/craig_agent.py` |
| Admin API (dashboard-facing) for products, quotes, conversations, metrics, settings | `admin_api.py` |
| Branded PDF quote generator | `pdf_generator.py`, `/quotes/:id/pdf` endpoint |

---

## Local development

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Point at a local SQLite file (the default) or a Postgres URL
cp .env.example .env
# Edit .env — paste DEEPSEEK_API_KEY. Leave CRAIG_DATABASE_URL blank for
# local SQLite; set it to a Postgres URL to run against your own DB.

# 3. Bootstrap + apply all migrations (idempotent)
python -m scripts.startup

# 4. Run
uvicorn app:app --reload --port 8000

# 5. Open http://localhost:8000 — preview page with the widget.
```

**Tests:**
```bash
pytest test_missive.py test_escalation.py -v    # no DB seeding required
pytest test_pricing.py                          # requires a seeded craig.db
```

---

## Deploying to Cloud Run (production)

Production uses Cloud Run + Cloud SQL Postgres (project `just-print-craig`,
region `europe-west1`).

```bash
gcloud run deploy craig-pricing \
    --source . \
    --region europe-west1 \
    --project just-print-craig \
    --add-cloudsql-instances just-print-craig:europe-west1:craig-db \
    --set-env-vars CRAIG_DATABASE_URL="postgresql+pg8000://craig:PASSWORD@/craig?unix_sock=/cloudsql/just-print-craig:europe-west1:craig-db/.s.PGSQL.5432" \
    --set-env-vars DEEPSEEK_API_KEY="..." \
    --min-instances=1 \
    --no-cpu-throttling \
    --quiet
```

Key deploy flags and why they're non-negotiable:

- `--min-instances=1` — keeps a warm container so Missive background
  tasks can finish (they run after the 200 ack has been sent to Missive).
- `--no-cpu-throttling` — Cloud Run throttles CPU outside request
  handling by default. Without this, any work after the response is
  paused; our 15-second Missive webhook ACK is fine but the
  `chat_with_craig()` call that follows would freeze.
- `--add-cloudsql-instances ...` — attaches a Unix socket at
  `/cloudsql/PROJECT:REGION:INSTANCE` so `pg8000` can connect without a
  VPC connector.

---

## Migration history (idempotent; all applied on every container boot)

Every startup runs `scripts/startup.py` which chains V2 → V9:

| # | What it does |
|---|---|
| V2 | Add `organization_slug` to every table; seed tax rates + category map |
| V3 | Add `category` table, product images, reshape categories |
| V4 | Seed `system_prompt` + widget branding settings |
| V5 | Strip the legacy hardcoded catalog block from stored `system_prompt`s |
| V6 | Seed `business_rules` with defaults (contact-first, no duplicate greeting, etc.) |
| V7 | Remove the "do NOT ask for contact info on standard quotes" paragraph that contradicted V6 |
| V8 | Refresh unedited V6 defaults to latest wording |
| V9 | Seed Missive integration settings (enabled, api_token, webhook_secret, from_address, from_name) |

Re-running is safe. Each migration checks for the specific state it's
applying and skips if already present.

---

## Documentation

- [`CLAUDE.md`](./CLAUDE.md) — project handoff context for Claude Code sessions. High-level intent, architecture, golden rules.
- [`docs/missive-integration.md`](./docs/missive-integration.md) — step-by-step Missive setup, webhook payload shape, attachment format, troubleshooting.

---

## Directory map

```
Craig-Pricing/
├── app.py                        # FastAPI app: /chat, /widget-config, /webhook/missive, /quotes/:id/pdf
├── pricing_engine.py             # Pure pricing logic, tenant-scoped
├── admin_api.py                  # Dashboard-facing CRUD — JWT-protected
├── missive.py                    # Thin Missive REST client (verify_webhook, get_message, create_draft)
├── pdf_generator.py              # reportlab-based branded quote PDF
├── llm/
│   └── craig_agent.py            # DeepSeek orchestration + system prompt composition
├── db/
│   ├── __init__.py               # SQLAlchemy engine (SQLite local, Postgres prod)
│   └── models.py                 # Products, PriceTiers, Surcharges, Settings, Conversations, Quotes, TaxRates
├── data/                         # Justin's hand-edited pricing JSON (source of truth)
├── scripts/
│   ├── startup.py                # Boot-time orchestrator: init_db + V2..V9 migrations
│   ├── migrate_json_to_db.py     # First-time seed: JSON → DB
│   ├── v2_multitenancy_pricing.py
│   ├── v3_categories_images.py
│   ├── v4_system_prompt_seed.py
│   ├── v5_strip_legacy_catalog.py
│   ├── v6_default_business_rules.py
│   ├── v7_patch_contact_contradiction.py
│   ├── v8_refresh_default_rules.py
│   └── v9_missive_settings_seed.py
├── static/
│   ├── index.html                # Preview page with the widget embedded
│   └── widget.js                 # Embeddable customer-facing chat widget
├── test_pricing.py               # Pricing correctness (requires seeded DB)
├── test_missive.py               # Missive HMAC + payload extraction (unit)
└── test_escalation.py            # Escalation gate + confirm_order (unit, in-memory)
```
