# Craig — AI Quoting Agent for Just Print

Read this first. This file is the handoff context for any new Claude Code session.

---

## What this project is

**Client:** Justin Byrne — Just-Print.ie (Irish print shop, Dublin)
**Agency:** Strategos AI — Roi (owner) + JS (builder)
**Status:** **Live in production.** Multi-tenant. Cloud Run + Cloud SQL Postgres. Auto-deployed via Cloud Build (`main` push → 189-test gate → image build → Cloud Run revision).

Craig is a custom AI quoting agent. It fronts:
- A floating chat widget embedded on just-print.ie (`static/widget.js`, served from `/widget.js`)
- An email channel via Missive (`info@just-print.ie` inbox)

Future channels (WhatsApp) will connect to the same `/chat` core. **No GHL** — this is a standalone microservice on purpose.

Underlying model: DeepSeek (OpenAI-compatible) with tool-calling. The LLM never computes prices; it calls into the pricing engine.

**Commercial:** €2,500 upfront, €1,400 on delivery (paid). Monthly TBD.

---

## The golden rules — NEVER break these

1. **Craig NEVER invents a price.** Every quote comes from `pricing_engine.py` via a real tool call against the DB.
2. **If product/qty/spec isn't on the sheet → escalate.** Don't guess, don't approximate. Don't fall back to yield-only math when `Product.requires_dimensions=True` (v38).
3. **Every quote is saved with `status="pending_approval"`.** Justin approves from the dashboard before the customer sees any commercial action.
4. **Pricing engine and LLM stay decoupled.** Engine is pure Python + DB session. LLM is one chat loop + tool definitions.
5. **No prod deploy without explicit user authorization.** `git push main` triggers Cloud Build → Cloud Run. `git push` to a dashboard branch + merge triggers Vercel.

---

## Architecture (post-v40)

```
Customer (web widget │ Missive email)
      ↓
FastAPI /chat
      ├── llm/inbound_classifier.py    (Missive only — Tier 1/2/3 triage)
      ├── attribution.py                (merge first/last touch; identity backfill)
      └── llm/craig_agent.chat_with_craig
              ├── DeepSeek tool loop (max 5 iters, temperature=0.3)
              │     tools: quote_small_format, quote_large_format, quote_booklet,
              │            list_products, save_customer_info, find_past_quotes_by_email,
              │            escalate_to_justin, confirm_order
              ├── Server-side gates (Phase F/G + v37/v38):
              │     - hallucinated-quote ([QUOTE_READY] without a Quote row → stripped)
              │     - premature [QUOTE_READY] (no contact, funnel open, artwork unanswered)
              │     - artwork choice/upload auto-emit ([ARTWORK_CHOICE], [ARTWORK_UPLOAD])
              │     - customer-form auto-emit ([CUSTOMER_FORM])
              │     - reply sanitizer (_humanize_reply: strip markdown)
              │     - Quote dedupe (v26 — same product+specs → reuse pending row)
              └── pricing_engine
                    ├── _quote_per_sqm  (v36: vinyl labels, banners — area math)
                    ├── _quote_per_sheet (v36: foamex/dibond/corri — panel packing)
                    ├── _stack_tiers     (v34: 530 cards → 500 + 100 combo)
                    └── apply_shipping_to_quote (€15 inc VAT, free over €100)
      ↓
Cloud SQL Postgres (just-print-craig:europe-west1:craig-db)
   └── 12 tables, scoped by organization_slug

Outbound (after Justin approves a quote in dashboard):
   admin_api PATCH /quotes/:id status=approved
      ├── stripe_client.create_payment_link        (Connect mode: Stripe-Account header)
      ├── printlogic_push.push_quote               (idempotent on real order_id; DRY-* if dry_run)
      └── missive_outbound.send_quote_draft        (PDF + payment link → email)
```

**LLM channel override (load-bearing):** `_CHANNEL_CONTEXT["missive"]` in [llm/craig_agent.py:513](llm/craig_agent.py:513) SUPERSEDES the base personality + business rules. When `channel="missive"` we drop both because phrases like "Nice one!" and the chat-bubble tone bleed verbatim into emails. Email replies fly on: channel override (with 4-step funnel: specs → artwork → funnel → PDF) + FAQs + live catalog.

**Inbound triage (v37, Missive only):** [app.py:921](app.py:921) `classify_inbound_email` + confidence threshold (default 0.85, per-tenant). Tier 1 (`confidence < LOW_CONFIDENCE_FLOOR`) silent drop. Tier 2 (`< threshold` OR `engaged-thread + verdict=False`) — runs Craig for preview, parks proposed reply in `Conversation.engagement_classification`, emails Justin to Approve/Reject. Tier 3 (`>= threshold`, `verdict=True`) — auto-responds.

---

## File layout (current)

```
Craig-Pricing/
├── app.py                          # FastAPI; /chat, /quote/*, /widget-config, /widget.js,
│                                   #   /webhook/missive/{org_slug}, Missive handler
├── pricing_engine.py               # Pure pricing — small/large/booklet + per-sqm + per-sheet
│                                   #   + apply_shipping_to_quote + client multiplier
├── attribution.py                  # v40 — merge_attribution + backfill_attribution_by_identity
├── widget_api.py                   # POST /widget/conversations/{cid}/customer-info,
│                                   #   /upload-artwork, /report-issue
├── admin_api.py                    # ~60 endpoints under /admin/api/* — JWT-protected per route
├── auth/jwt_auth.py                # HS256 verify; StrategosClaims; access_guard, require_role
├── db/
│   ├── __init__.py                 # engine, SessionLocal, get_db, parse_artwork_files
│   └── models.py                   # 12 tables (see schema below)
├── llm/
│   ├── craig_agent.py              # chat_with_craig orchestration (~2.9k LOC, all the gates)
│   └── inbound_classifier.py       # v37 — classify_inbound_email + obvious_junk
├── missive.py                      # Async client: verify_webhook, get_message, create_draft,
│                                   #   add_shared_labels (v37.7), download_attachment_bytes
├── missive_outbound.py             # Bridge: PDF + payment link → email thread
├── printlogic.py                   # Async client: find_customer, create_order (dry-run),
│                                   #   update_order_status
├── printlogic_payload.py           # build_payload_from_quote — multi-line jobsheets, due_date
├── printlogic_push.py              # Idempotent push orchestrator (DRY-* vs real order_id)
├── stripe_client.py                # Payment Links (inline price) + webhook signature verify
├── stripe_connect.py               # OAuth state signing (HMAC, 5-min TTL); code exchange
├── notifications.py                # Resend — approval, manual-review, admin alerts,
│                                   #   engagement-approval emails
├── secrets_crypto.py               # Fernet (AES-128) at-rest; MultiFernet for rotation;
│                                   #   prefix `enc::v1::`
├── settings_security.py            # SECRET_KEYS allowlist + SECRET_MASK policy
├── rate_limiter.py                 # In-memory sliding window, 60/min default
├── pdf_generator.py                # ReportLab — Just Print branded quotation (v36 layout)
├── integrations_status.py          # Per-tenant green/yellow/red for Missive/PrintLogic/Stripe
├── pricing_data.py                 # Legacy JSON loader (kept for tests)
├── extractor.py                    # Legacy fuzzy matcher (kept for alias data)
├── main.py                         # Legacy v1 API (kept for old tests)
├── static/
│   ├── widget.js                   # Vanilla JS embeddable widget — captures UTMs (v40),
│   │                               #   pushes dataLayer events, [ARTWORK_CHOICE] buttons
│   └── index.html                  # /just-print.ie preview mock with widget mounted
├── scripts/
│   ├── startup.py                  # Migration orchestrator — runs on every container boot
│   ├── v2..v40 *.py                # Idempotent stacked migrations (see History below)
│   ├── seed_demo_tenant.py         # Provision a `demo` tenant for new client setup
│   ├── probe_printlogic.py         # Step 1 of go-live: READ-ONLY API key validation
│   └── analyze_*.py, export_*.py   # Ops helpers (audit Missive, export conversations)
├── docs/
│   ├── go-live-checklist.md        # 5-step staged production activation runbook
│   ├── missive-integration.md      # Webhook ↔ draft flow; HMAC; payload shapes
│   ├── smoke-test-checklist.md     # Manual 10-min E2E (widget + dashboard + integrations)
│   └── stripe-connect-migration.md # OAuth setup (platform + per-tenant connect)
├── tests/                          # ~30 test files; CI gate runs 8 (see cloudbuild.yaml)
├── cloudbuild.yaml                 # Tests → build → push → deploy (25m timeout)
├── Dockerfile                      # python:3.11-slim; CMD runs startup.py then uvicorn
├── requirements.txt
└── CLAUDE.md                       # THIS FILE
```

---

## Database schema (12 tables, Postgres prod / SQLite local)

All tables carry `organization_slug` for tenant scoping. Default: `just-print`.

| Table | Purpose | Key fields |
|-------|---------|-----------|
| `products` | Catalog | `pricing_strategy` (tiered / per_unit / per_sqm / per_sheet / bulk_break / per_job), `manual_review_required` (v34), `requires_dimensions` (v38), `sanity_max_unit_price` (v38), `min_billable_sqm` (v39), `yield_per_sqm` / `default_unit_size_mm` / `sheet_size_mm` / `sheet_price` (v36) |
| `price_tiers` | `{product_id, spec_key, quantity, price}` — booklets encode spec as `"32pp\|self_cover"` |
| `product_aliases` | Free-text synonyms → product_key |
| `surcharge_rules` | `kind` (multiplier / additive), `applies_to_category` (v32), `applies_to_product_keys` (v34 — JSON list, wins over category) |
| `settings` | Per-tenant K/V. Encrypted at rest for keys in `settings_security.SECRET_KEYS` |
| `tax_rates` + `category_tax_map` | Per-category VAT (Irish standard 23%, reduced 13.5%) |
| `categories` | First-class category with name/description/icon (v3) |
| `conversations` | Per-customer session. Includes Phase E funnel (`is_company`, `delivery_method`, etc.), Phase G `customer_has_own_artwork` + `artwork_will_send_later`, `engagement_classification` (v37 JSONB), `attribution` (v40 JSONB), `is_test` (v35) |
| `quotes` | Every quote Craig produces. PrintLogic / Stripe / Missive outbound state lives here, plus v33 approval (`approved_at`, `notification_sent_at`), v34 manual pricing (`manual_quote_price_inc_vat`, `manual_review_reason`), Phase G `artwork_files` JSON array, Phase F shipping cols |
| `issue_reports` | v35 customer-side problem reports from widget footer |
| `pricing_verification_flags` | v34 per-(product, qty, spec) operator flag + comment |

Schema in [db/models.py](db/models.py). Quote lifecycle: `pending_approval` → `approved` (Justin clicks Approve) → payment link sent → Stripe webhook `paid` → PrintLogic push → `in_production`. Off-path: `needs_revision` (manual_review flow), `rejected`.

---

## Pricing rules (Justin confirmed)

| Rule | Detail |
|------|--------|
| Prices | Retail, quoted to customer **inc VAT** in conversation; PDF shows breakdown |
| Double-sided | +20% on base (except business cards — no extra) |
| Soft-touch finish | +25% multiplier OR €15 additive (per-product config since v34) |
| NCR triplicate | +10% on base (NCR only) |
| Artwork | €65 + VAT per hour (one-hour standard). Standard 23% VAT rate (service, not goods) |
| Shipping | €15 inc VAT delivery, free over €100 inc-VAT goods; €0 for collect |
| Per-sqm products | Vinyl labels, banners, graphics. v38 `requires_dimensions=True` blocks yield-fallback runaway |
| Per-sheet products | Foamex/dibond/corri panels. Greedy axis-aligned packing with rotation |
| Off-tier qty | v34 stack-tier: 530 cards → 500 + 100 = 600 billed (cheapest combination, max 5× largest tier) |
| Min billable area | v39 `min_billable_sqm` — vinyl labels under 1 m² billed as 1 m² (per-product config) |
| Sanity ceiling | v38 `sanity_max_unit_price` — engine refuses to quote above per-unit cap, escalates |
| VAT | Irish 23% standard, 13.5% reduced (per-category via tax_rates + category_tax_map) |
| Turnaround | "3-5 working days" setting |
| Client multiplier | Per-tenant scalar applied AFTER surcharges, BEFORE VAT. Clamp: 0 < x ≤ 10 |
| POA items | Z-fold, die-cut, installation, rush, custom sizes → escalate via `escalate_to_justin` |

---

## Channels

### Web widget

`static/widget.js` is embedded on just-print.ie. Loads config from `GET /widget-config?client={slug}`. v37.8 kill switch: `widget_enabled=false` setting prevents mount. v40 captures `utm_*`, `gclid`/`gbraid`/`wbraid`, `fbclid`/`fbc`/`fbp`, `ttclid`, `msclkid`, `li_fat_id` from URL + first-touch write-once in localStorage; sends on every `/chat` + `/widget/conversations/{cid}/customer-info`. Pushes `lead_created` + `quote_generated` events to `window.dataLayer` with dedup `event_id`. **Decision LOCKED:** we feed dataLayer only; Google Ads/GTM team handles CAPI server-side.

Funnel form: `POST /widget/conversations/{cid}/customer-info` ([widget_api.py:174](widget_api.py:174)) — collects name/email/phone/company/returning/delivery+address. Auto-fills delivery_address from `shop_address` setting on collect. Validates Irish eircode + rejects disposable email domains. Triggers v33 approval notification.

Artwork upload: multi-file (max 10 per quote, 100MB each), allowed exts `.pdf/.jpg/.png/.ai/.indd/.eps/.tiff/.psd/.svg`. GCS bucket `CRAIG_ARTWORK_BUCKET` in prod; `/tmp/craig-artwork` local. Files served via authenticated proxy `/admin/api/orgs/{slug}/quotes/{id}/artwork/{idx}/file`.

### Missive (email)

Webhook at `POST /webhook/missive/{org_slug}`. HMAC-SHA256 verified against `missive_webhook_secret` setting. Returns 200 within 15s, processes in BackgroundTask. Full flow in [app.py:692 `_handle_missive_event`](app.py:692). Key behaviors:
- Idempotency cache (`_DRAFTED_FOR_MESSAGES`) — Missive retries up to 5× over 8 min
- Self-sent / internal-team allowlist (`internal_team_domains` + `internal_team_addresses` v37.7) — silent drop
- HTML strip + quoted-thread strip (`_strip_quoted_thread`)
- Three-tier engagement triage (v37) before any LLM call
- Inbound attachment ingestion → GCS, stamped onto pending Quote, flips `customer_has_own_artwork=True`
- Returning-customer detection injected as `[CUSTOMER STATUS]` system message (v32.2)
- v33 auto-send default ON; only escalations draft. Label tagging via `missive_label_auto_replied` setting (v37.7)

Outbound (after Justin approves a quote on web-channel conv): `missive_outbound.send_quote_draft` creates a brand-new thread with PDF + payment link.

---

## What's been built ✅

- **Pricing core**: 6 strategies (tiered, per_unit, per_unit_metric, bulk_break, per_job, per_sqm, per_sheet), tier stacking, surcharge scoping (product → category → global precedence), shipping, client multiplier, manual-review gate, sanity ceiling, min billable area.
- **LLM**: 8 tools, channel-aware prompts, 4-step email funnel (specs/artwork/funnel/PDF), Phase G upload-first flow, quote dedupe, ~15 server-side gates / sniffers (hallucinated-quote, premature [QUOTE_READY], artwork choice/upload, customer-form, contact-info sniff, artwork-answer sniff, pending-later sniff).
- **Multi-tenancy**: every table scoped by `organization_slug`; 4-role hierarchy (`client_viewer < client_member < client_owner < strategos_admin`); JWT (HS256, 5-min) signed by dashboard, verified by [auth/jwt_auth.py](auth/jwt_auth.py).
- **Dashboard** (separate repo, `strategos-dashboard`): 9 modules (Overview, Quotes, Conversations, Attribution v40, Connections, Catalog, Settings, Test Chat v35, Issues v35). Next.js 16 App Router + Supabase + Tailwind/Radix. Auto-deploys to Vercel.
- **Integrations live**:
  - Missive — webhook in, HMAC-verified; auto-send drafts; label tagging
  - PrintLogic — order push (dry-run default; live mode flippable per-tenant); customer dedup via past_customer_email
  - Stripe Connect — OAuth onboarding (state-signed, 5-min TTL); inline-price Payment Links; webhook with constant-time verify
  - Resend — approval + manual-review + admin-alert + engagement-approval emails (all idempotent on `notification_sent_at`)
- **CI**: Cloud Build runs 189 tests (8 files) before image build. Idempotent migrations apply at every container start via `python -m scripts.startup`.
- **Marketing attribution (v40)**: first-touch write-once + last-touch always; identity backfill stitches email/WhatsApp leads to prior web sessions; `/admin/api/orgs/:slug/attribution-report` (owner-only) groups by utm_source/medium/campaign etc.

## What's NOT done ❌

- **WhatsApp** — endpoint stub planned; not wired.
- **Catalog CSV/XLSX import** — Justin requested; postponed.
- **PrintLogic live mode for Just Print** — currently dry_run=true; Stage 3 of go-live checklist is the supervised flip.
- **Playwright suite for smoke-test-checklist.md** — runbook is manual today.

---

## Migration history (high-level)

40 migrations stacked idempotently. `scripts/startup.py` orchestrates: pre-DDL passes (v34/v35/v36/v37/v38/v39/v40) → ORM init → seed/migrate scripts in version order.

Highlights:
- **v2** multi-tenancy (organization_slug everywhere)
- **v9** Missive settings seed
- **v12-v18** PrintLogic + Stripe (paste-flow → Connect OAuth)
- **v17** at-rest encryption for secret settings (Fernet, key from `CRAIG_SECRETS_KEY`)
- **v22-v25** Phase E + F + G (funnel form, artwork upload, multi-file)
- **v33** dashboard approval pipeline + Resend notifications
- **v34** manual-review escalation + stack-tier + per-product surcharge scoping
- **v35** test-chat sandbox + issue reports
- **v36** per-sqm + per-sheet pricing
- **v37 / v37.7** engagement triage + cutover safety (internal-team allowlist, sentinel `missive_from_address_last_known`)
- **v38** `requires_dimensions` + `sanity_max_unit_price` + price-first artwork flow (Bug 3 fix)
- **v39** `min_billable_sqm`
- **v40** marketing attribution

For diff-level detail, read `scripts/vNN_*.py`.

---

## Local dev quick start

The user's bootstrap-from-zero flow (see project memory `craig-pricing-local-bootstrap.md` for the gotcha):

```bash
# 1. Clone + venv
git clone https://github.com/JSebastianIEU/Craig-Pricing.git
cd Craig-Pricing
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-cov httpx respx PyJWT

# 2. Pull secrets from Secret Manager and write .env
#    (gcloud auth assumed, project=just-print-craig)
for s in DEEPSEEK_API_KEY STRATEGOS_JWT_SECRET; do
  echo "$s=$(gcloud secrets versions access latest --secret=$s --project=just-print-craig)" >> .env
done
echo "CRAIG_SECRETS_KEY=$(gcloud secrets versions access latest --secret=craig-secrets-key --project=just-print-craig)" >> .env
echo "CRAIG_DB_PATH=/tmp/craig.db" >> .env

# 3. Seed local DB — MUST export env first; scripts.startup does NOT load .env
set -a && source .env && set +a
python -m scripts.startup

# 4. CI gate
pytest test_chat_smoke.py test_craig_flow.py test_pricing_edge_cases.py \
       test_pricing.py test_cutover_safety.py test_inbound_classifier.py \
       test_missive.py test_attribution.py -q
# → 189 passed

# 5. Run
uvicorn app:app --reload
# /             — preview page with widget
# /docs         — OpenAPI explorer
# /admin/api/me — needs Bearer JWT signed by STRATEGOS_JWT_SECRET
```

**SQLite I/O error on iCloud-synced folders:** `CRAIG_DB_PATH=/tmp/craig.db` in `.env` (already in the snippet above).

---

## Production

| | |
|--|--|
| GCP project | `just-print-craig` |
| Region | `europe-west1` |
| Cloud Run service | `craig-pricing` |
| Live URL | `https://craig-pricing-277215252762.europe-west1.run.app` |
| Cloud SQL instance | `just-print-craig:europe-west1:craig-db` (Postgres, app user `craig`) |
| Artifact Registry | `europe-west1-docker.pkg.dev/just-print-craig/craig/craig-pricing` |
| Dashboard | Vercel — auto-deploy from `strategos-dashboard` `main` |
| CI trigger | Push to `main` of Craig-Pricing → Cloud Build → 8-file pytest gate → build → push → `gcloud run services update` |

Cloud Run flags worth knowing: `--min-instances=1 --no-cpu-throttling` so Missive background tasks (>15s) complete. Env vars resolved from Secret Manager via `secretKeyRef`:

| Setting | Secret name |
|--|--|
| `DEEPSEEK_API_KEY` | `DEEPSEEK_API_KEY` |
| `STRATEGOS_JWT_SECRET` | `STRATEGOS_JWT_SECRET` |
| `CRAIG_SECRETS_KEY` | `craig-secrets-key` (note lowercase-dashed) |
| `STRATEGOS_STRIPE_PLATFORM_KEY` | (Connect platform) |
| `STRATEGOS_STRIPE_CONNECT_WEBHOOK_SECRET` | (Connect webhook signing) |
| `RESEND_API_KEY` | (notification email) |

Per-tenant secrets (PrintLogic API key, Stripe access token, Missive webhook secret, etc.) live in the encrypted `settings` table, NOT in env vars.

---

## Craig's personality (don't drift)

Casual, Irish-market professional. Transparent about AI on first message; doesn't repeat. Short replies — 2-3 sentences, WhatsApp-style. **No markdown ever** (widget renders literal asterisks). Emojis fine on web channel, **zero emojis on email**.

Email opener: "Hi {FirstName}," — body in 1-3 short paragraphs — sign-off `Cheers, / Craig / Just Print`. Web opener: "Hey — Craig here, I handle pricing for Just Print 🖨️ What are you looking to print?"

Forbidden phrases on email: "Nice one!", "That comes to", "Want me to put together the full quote?", "Hey!"/"Hi there!", any emoji.

**Language mirroring (v38):** detect customer's language from turn 1, lock it in. All other rules apply identically in that language.

Full system prompts live in [llm/craig_agent.py:80 `CRAIG_SYSTEM_PROMPT`](llm/craig_agent.py:80) and [llm/craig_agent.py:513 `_CHANNEL_CONTEXT`](llm/craig_agent.py:513). Don't edit elsewhere — per-tenant overrides go in the `system_prompt` setting via the dashboard Settings tab.

---

## Credentials & secrets

Sensitive values **never live in this repo**. Sources:

- **Cloud Run env (Secret Manager)** — fetch with `gcloud secrets versions access latest --secret={name} --project=just-print-craig`.
- **Per-tenant integration secrets** — in the encrypted `settings` table (`missive_api_token`, `missive_webhook_secret`, `printlogic_api_key`, `stripe_access_token`). Decrypted on read via `secrets_crypto.decrypt`; masked as `********` in `GET /admin/api/settings` responses ([settings_security.py](settings_security.py)).
- **`.env` for local dev** — listed above; in `.gitignore`.

**Client-account credentials (Justin's PrintLogic API key, OnPrintShop admin, Missive login, WordPress admin)** — those are Justin's accounts. Roi has them out-of-band. Don't paste into chat or commit.

---

## Coding conventions

- Python 3.11 (CI image). Type hints encouraged, not religious.
- FastAPI for HTTP, SQLAlchemy 2.0 for DB, Pydantic v2 (`ConfigDict(extra="forbid")` on every write body).
- Migrations: write a new `scripts/vNN_description.py`, wire into `scripts/startup.py` in the right order (pre-DDL pass if it ADDs columns; regular pass otherwise). Make it idempotent — startup runs on every container boot.
- Pricing engine stays pure (no HTTP, no LLM). LLM layer stays thin (one chat loop + tool defs + gates).
- Widget stays vanilla JS — no build step, no bundler.
- Tests in CI gate are FAST (no real network — DeepSeek key faked, integrations stubbed). The `slow` marker is for tests hitting real services.

---

## Operational runbooks

- [`docs/go-live-checklist.md`](docs/go-live-checklist.md) — 5-step staged production activation with rollbacks
- [`docs/missive-integration.md`](docs/missive-integration.md) — webhook/draft details, troubleshooting
- [`docs/smoke-test-checklist.md`](docs/smoke-test-checklist.md) — manual 10-min E2E
- [`docs/stripe-connect-migration.md`](docs/stripe-connect-migration.md) — platform + per-tenant OAuth

---

## When in doubt

- Pricing logic → edit `pricing_engine.py`, add a test in `test_pricing.py` or `test_pricing_edge_cases.py`, run `pytest test_pricing*.py -q`
- Craig behavior → edit prompts in `llm/craig_agent.py` (base or channel override); add a test in `test_chat_smoke.py` or `test_craig_flow.py`
- Widget look → `static/widget.js` (all styles + markup inline)
- New endpoints → `app.py` (public) or `admin_api.py` (JWT-protected)
- New DB columns → new `scripts/vNN_*.py` + update `db/models.py`. Test by re-running `python -m scripts.startup` on a wiped DB.

**Escalate to the human (JS / Roi) when:**
- Pricing behavior changes that aren't on Justin's sheet
- Anything client-facing (copy, tone, escalation wording)
- Cloud infrastructure changes (region, project, scaling)
- ANY production deploy (no `gcloud run deploy` / `git push main` without explicit ask)
- Writes against prod admin API (mint JWT + POST) — Justin prefers to make catalog/settings tweaks himself in the dashboard

---

*Last updated: 2026-05-28 — post-v40 ship (per-product min billable area + marketing attribution).*
