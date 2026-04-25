# Manual end-to-end smoke test

A checklist a human follows in ~10 minutes to confirm Craig's full
customer-facing flow still works after a deploy. Designed for the
moment **before** you tell Roi "it's done" — catches the regressions
unit tests can't catch (like CSS breakage, browser-specific quirks,
or "I forgot to redeploy").

There's also a `## TODO: automate this` section at the bottom — when
we're ready to invest in Playwright we use this checklist as the
specification.

> **When to run:** before every promotion to production traffic. After
> the activation runbook (`go-live-checklist.md`) the first time.

---

## Pre-flight (1 min)

- [ ] Cloud Run latest revision is healthy: `curl https://<cloud-run>/health` returns 200 with `service: craig-pricing-service`
- [ ] Dashboard at `https://strategos-ai.com` loads, you can log in
- [ ] You have a non-Just-Print email account ready (Gmail throwaway works)

---

## Test 1 — Widget round-trip on the live URL (3 min)

Tests: pricing engine, LLM tool calling, conversation persistence,
PDF generation, quote confirmation.

- [ ] Open `https://<cloud-run>/?client=just-print` in an incognito window
- [ ] Click the floating chat bubble (bottom-right)
- [ ] Type: `500 business cards soft touch double sided`
- [ ] Craig responds with a price (~€237, or near it). Verify:
  - [ ] Price contains the soft-touch flat fee (€15) added
  - [ ] Price uses 13.5% VAT (printed matter)
  - [ ] No `[QUOTE_READY]` token leaked into the visible reply
- [ ] Type: `yes that's good, john@example.com, 555-1234`
- [ ] Craig confirms and shows a **quote card** with View / Download buttons
- [ ] Click **View** → PDF opens in a new tab
- [ ] PDF has Just Print's logo, payment icons, the customer name,
      the correct line items + grand total, no overlapping text
- [ ] Type: `confirm`
- [ ] Craig responds with `Order confirmed. Justin will be in touch...`
- [ ] If Stripe is enabled: the response includes a payment URL

---

## Test 2 — Widget error banner appears on backend failure (1 min)

Tests: the new error UX from this PR.

- [ ] Open the widget locally pointed at a dead URL: edit
      `static/widget.js` `API_BASE` temporarily to a 404 endpoint
      (or just stop the local server while a chat is open)
- [ ] Send a message → red banner appears at the top of the message
      list with copy "Couldn't reach Just Print's quoting agent..."
- [ ] Banner auto-hides after ~8 seconds
- [ ] Restart the server and send again → banner clears, normal reply works

---

## Test 3 — Dashboard reflects the new conversation (2 min)

Tests: admin API, multi-tenancy scoping, JWT auth.

- [ ] Open Strategos dashboard → `/c/just-print/a/craig`
- [ ] **Overview** tab → IntegrationsHealthCard shows 3 integrations
      with sensible health (likely yellow/unknown if not yet activated)
- [ ] Stat cards have non-zero counts (you just made a quote)
- [ ] **Conversations** tab → see your conversation, click in →
      messages render in order
- [ ] **Quotes** tab → see the quote you just made
- [ ] Status badge says `pending_approval`
- [ ] Click **Approve** → flips to `approved` (toast confirms)
- [ ] **PrintLogic** column shows the **Push** button (no order yet)
- [ ] **Payment** column shows **Create link** (Stripe disabled = won't actually create)
- [ ] Click **PDF** → drawer opens with the PDF preview

---

## Test 4 — Connections tabs render and load (1 min)

Tests: the new PrintLogicTab and StripeTab.

- [ ] Navigate to `Craig → Connections`
- [ ] Tabs visible: Widget | WhatsApp | Missive | **PrintLogic** | **Stripe** | Email
- [ ] Click **PrintLogic** → loads, status pill shows current health,
      api_key field shows masked value, Live mode switch reflects DB state
- [ ] Click **Stripe** → loads, webhook URL is shown with Copy button,
      enable switch is disabled until secret + whsec are populated

---

## Test 5 — Rate limiting works (1 min)

Tests: the new rate limiter on `/chat`.

- [ ] Open the widget. Quickly send 35 messages in a row (just type
      `1`, hit enter, repeat — the input becomes disabled briefly
      between sends, but you can mash through).
- [ ] Around message 30-31, the error banner appears with copy
      "Too many messages too fast. Give it a few seconds and try
      again." — confirming `/chat` is returning 429.
- [ ] Wait 60 seconds → next message works normally.

---

## Test 6 — Demo tenant doesn't pollute just-print (1 min)

Tests: the new `--org-slug` parameterization.

- [ ] Open `https://<cloud-run>/?client=demo` in incognito
- [ ] Quote `500 business cards soft touch` → returns the same
      €237ish price (demo uses just-print's catalog by default)
- [ ] In the dashboard, switch to `/c/demo/a/craig/quotes` → you see
      the demo quote, but the just-print Quotes tab does NOT (data is isolated)

---

## Sign-off

- [ ] All 6 test sections passed without surprises
- [ ] Cloud Logging has no fresh ERROR-level entries from the smoke run
- [ ] Smoke run took less than ~12 min (if it took longer, something
      is dragging — investigate before promoting)

If everything's green, the build is ready to hand to Roi.

---

## TODO: automate this with Playwright

This entire checklist is mechanical and worth automating once the
maintenance burden of running it manually outweighs the cost of
writing the test. Skeleton plan:

```
strategos-dashboard/
└── e2e/
    ├── playwright.config.ts
    ├── fixtures/
    │   └── auth.ts            # login + JWT cookie helper
    └── tests/
        ├── widget-roundtrip.spec.ts    # tests 1+2 from this checklist
        ├── dashboard-quotes.spec.ts    # test 3
        ├── connections-tabs.spec.ts    # test 4
        └── rate-limit.spec.ts          # test 5
```

Decisions:
- Run against staging (a separate Cloud Run revision with `STAGING=true`
  env var that disables actual Missive / Stripe / PrintLogic outbound
  calls regardless of Settings) — never against prod
- Use a dedicated `demo-test` tenant seeded fresh per CI run via
  `python -m scripts.seed_demo_tenant` (rename the slug constant)
- Run on every PR via GitHub Actions, but mark as `continue-on-error`
  for the first month so flaky-test fatigue doesn't gate human PRs
  while the suite is maturing

Effort estimate: 1 dev-day for the suite + 0.5 dev-day for CI plumbing.
Worth doing once we have ≥3 paying tenants — until then the manual
checklist is fine.
