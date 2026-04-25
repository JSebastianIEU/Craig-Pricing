# Go-live activation checklist

This is the runbook for moving Craig from "code deployed, all dormant" to
"all integrations live for Just Print" in production. Anyone (you, a
future engineer, Roi if it comes to it) should be able to follow this top
to bottom and end with a fully operational deployment.

The work is staged: the safest steps run first, the destructive ones
last. Every step has a rollback recipe in case something misbehaves.

> **The golden rule:** every integration starts disabled. The defaults in
> `v9_missive_settings_seed.py`, `v13_printlogic_settings_seed.py`, and
> `v16_stripe_settings_seed.py` are deliberately set so that a fresh
> deploy is dormant. Activating each one is a conscious, supervised act
> in this doc.

---

## Prerequisites — what you need before starting

| Asset | Where it comes from | Status today |
|---|---|---|
| PrintLogic API key | Already in DB (Setting `printlogic_api_key`, tenant `just-print`) | Stored |
| PrintLogic test order number + customer email | Justin / Alexander | `1519487` + `info@just-print.ie` (per Phase A plan) |
| Missive credentials | Justin gave them in `credentials.md` | `info@just-print.ie` / `Hjk379bm!?` — never used |
| Stripe live secret key (`sk_live_...`) | Justin's Stripe dashboard → Developers → API keys | **Pending — ask Roi to ask Justin** |
| Stripe webhook signing secret (`whsec_...`) | Created when registering the webhook endpoint | **Pending — generated in step 4** |
| Cloud Run URL of the Craig backend | Already deployed | `https://<your-cloud-run-service>.run.app` |
| Strategos dashboard admin login | Existing | You have it |

If any "Pending" row above isn't filled in, stop here. Activation can
only proceed when you have the credentials in hand.

---

## Step 1 — Validate the PrintLogic API key (READ-ONLY, zero risk)

Time: ~2 min. Risk: zero. Side effects: zero.

```bash
cd Craig-Pricing
python -m scripts.probe_printlogic just-print 1519487 info@just-print.ie
```

Expected output:

```
✓ Tenant: just-print
✓ api_key loaded (length=...)
  printlogic_dry_run currently: 'true'

[probe 1/2] get_order_detail('1519487') — read-only...
  ✓ OK  (order #1519487, total=...)

[probe 2/2] find_customer(email='info@just-print.ie') — read-only...
  ✓ OK  found customer_id=...

✓ READY FOR STAGE 2 — auth + firm binding validated.
```

If the probe fails:
- `AMBIGUOUS` shape (`{"result":"ok","raw_body_length":53}`) → API key is
  authenticated but **not bound to Justin's firm**. Talk to Alexander
  (Wildcard) before proceeding. Do NOT attempt any push.
- `401` / `auth` error → API key is wrong or revoked. Ask Justin for a
  new one through the PrintLogic admin.
- `Crashed: ...` → likely a network blip or PrintLogic outage. Retry in
  a few minutes; if persistent, escalate.

**Rollback:** none needed — the probe writes nothing.

---

## Step 2 — Activate Missive on Justin's inbox

Time: ~15 min. Risk: low. Side effects: drafts will start landing in
Justin's Missive when customers email — but they're drafts, not sent.

### 2a. Generate a Missive API token from Justin's account

1. Open `https://missiveapp.com` in a private browser window.
2. Log in with `info@just-print.ie` / `Hjk379bm!?` (from `credentials.md`).
3. Go to **Settings → Integrations → API**. Click **New token**.
4. Name it `craig-prod` and copy the value. Stash it in 1Password
   immediately — Missive shows it only once.

### 2b. Configure Missive's webhook rule

Still in Justin's Missive:

1. Open **Settings → Rules → New rule**.
2. Conditions: `When a message is received` (no other filters — Craig
   filters channel-side based on the org slug it's invoked with).
3. Action: `Send webhook to URL`. Paste:
   ```
   https://<cloud-run-url>/webhook/missive/just-print
   ```
4. Sign with shared secret. Generate a fresh secret (32 random bytes,
   base64-url) — or just paste the value already stored in our DB
   under `missive_webhook_secret`. Either works as long as both sides
   match. Read the dashboard's **Connections → Missive** tab to see the
   current secret; copy from there into Missive's rule.
5. Save the rule. Missive will now POST every incoming message to our
   webhook.

### 2c. Update Craig's tenant settings

In the Strategos dashboard:

1. Navigate to `Craig → Connections → Missive`.
2. Paste the API token from 2a into **API token**, save.
3. Change **From address** from `sebastian@strategos-ai.com` (test
   inbox) to `info@just-print.ie`. Save.
4. Verify the **From name** is `Craig @ Just Print`.
5. Toggle **Missive enabled** ON.

### 2d. Smoke test

Send an email to `info@just-print.ie` from a different account
(`gmail.com` works). Within ~5s you should see, in Justin's Missive:

- A draft reply already authored by Craig
- Tagged with the conversation context
- Optionally with a PDF attached (if Craig got far enough to quote)

**Rollback:** flip `missive_enabled=false` in the dashboard. Drafts stop
generating immediately. No emails sent.

---

## Step 3 — Connect Stripe via OAuth ("Connect with Stripe" button)

Time: ~5 min for Justin once the platform setup is done. Risk: zero
(money goes directly to his account; Strategos never custodies it).

> **Prerequisite:** the platform-side setup in
> [`docs/stripe-connect-migration.md`](./stripe-connect-migration.md)
> must be complete first. That's a one-time Roi task. Once done, every
> future client onboarding is just step 3a below.

### 3a. Justin clicks Connect

1. Justin signs in to `https://agents.strategos-ai.com` → his workspace
2. Navigates to **Craig → Connections → Stripe**
3. Clicks **"Connect with Stripe"**
4. Stripe takes over: he picks an existing Stripe account or creates a
   new one (~2 min if new — Stripe walks him through bank details, ID
   verification, etc.)
5. Approves the OAuth consent
6. Lands back on the dashboard with a green "Connected to Stripe" toast

The tab now shows "Connected to {his_email} · acct_xxx" with Test +
Disconnect buttons.

### 3b. Smoke test (test card)

While Justin is still on the call:

1. In the dashboard **Quotes**, find a small recent quote
2. Click **Create link** in the Payment column
3. Copy the URL → open in incognito
4. Pay with `4242 4242 4242 4242`, any future date, any CVC
5. Within 30s the Payment column flips to **Paid €X**

If nothing happens after 60s:
- Check Stripe → Developers → Webhooks → our platform endpoint → see the
  **Event log**. Each delivery shows the response code. 400 = HMAC
  failed (platform whsec env var drift). 503 = platform whsec env var
  missing entirely.
- 200 + no Quote update → confirm `metadata.craig_quote_id` is in the
  event payload (it should be — Craig stamps it when creating the link).
  If missing, the link was created via a different path (manual in
  Stripe's UI) and won't correlate.

**Rollback:** flip `stripe_enabled=false` in the dashboard. No new
links can be created. Existing links remain valid until cancelled via
the **Cancel** button in Quotes. To fully cut the cord: click
**Disconnect** in the Stripe tab (revokes Stripe-side too).

---

## Step 4 — (no-op with Connect)

The legacy "promote to live mode" step doesn't exist with Connect. The
tenant's account IS their live account from step 3a — no test/live
swap. The platform's secret key (set in env vars by Roi) determines
whether the connection is a test-mode or live-mode link, and that
matches what Roi configured in step 6 of the platform setup.

If you need to validate live payments end-to-end before Justin tells
his customers about it:
1. Make sure platform creds are `sk_live_***` (not `sk_test_***`)
2. Connect Justin via OAuth (he'll get prompted for live-mode account)
3. €1 sentinel charge with a real card → confirm webhook lands → refund
4. From this point on, every confirmed quote can charge real money

---

## Step 5 — PrintLogic Stage 3 ceremony with Justin

Time: ~10 min. Risk: high — every push from `printlogic_dry_run=false`
creates a real order in Justin's PrintLogic. Must be done with Justin
on a call watching his UI.

This is the existing Stage 3 plan from `CLAUDE.md` / Phase A. Recap:

### Pre-call setup

1. Create a sentinel quote in the dashboard:
   - Product: `business_cards`
   - Quantity: 1
   - Customer: yourself (so it's obviously a test)
   - In the `notes` field, write: `[CRAIG-TEST-DELETE-ME]`
2. Verify it shows up in **Quotes** with PrintLogic column showing
   the **Push** button (no order_id yet).

### On the call with Justin

1. Justin opens his PrintLogic UI to the orders list. He's watching.
2. You: open `Craig → Connections → PrintLogic`. Confirm
   `printlogic_dry_run` switch is currently OFF (= dry-run on, safe).
3. Flip the **Live mode** switch ON. Confirm the warning about real
   pushes.
4. In **Quotes**, click **Push** on the sentinel.
5. Within 60s Justin sees a new order appear in PrintLogic with
   `[CRAIG-TEST-DELETE-ME]` in the description. Confirm the
   `[CRAIG-PUSH qid=<id>]` marker is also present.
6. In the dashboard, click **Cancel** on the order. Within 60s
   Justin sees the order status flip to "Cancelled" in PrintLogic.
   If PrintLogic refuses the cancel (some statuses can't be cancelled
   via API), Justin deletes manually from his UI — that's why the
   marker matters.
7. Flip **Live mode** OFF (back to dry-run) — this stays off until
   Stage 4 (separate ticket, post-launch).

**Rollback:** PrintLogic doesn't have an "undo" beyond
`update_order_status("Cancelled")`. If we push the wrong batch by
accident, flip `printlogic_dry_run=true` immediately via the dashboard
or via Supabase SQL editor on the Settings table — every subsequent push
will be a no-op DRY-xxx synthetic id, and Justin can clean up manually.

---

## Post-launch monitoring (first 7 days)

What to check daily:

1. **Dashboard → Overview → Integration health card.** All three
   integrations should be **green**. Yellow is acceptable for
   PrintLogic (dry-run is the default safe state). Any **red** is
   urgent.
2. **Cloud Logging** filter: `resource.type="cloud_run_revision"
   AND jsonPayload.component=("stripe" OR "printlogic")`. Any
   `ok=false` line warrants investigation.
3. **Stripe dashboard → Webhooks → Event log.** All deliveries should
   be 200. Recurring 400s = signing secret drift. 5xx = our service
   crashed.
4. **Missive → Justin's inbox.** Spot-check that drafts are being
   created on inbound mail and that the From address shows
   `info@just-print.ie` (not `sebastian@strategos-ai.com` — that's the
   test config that we replaced in step 2c).

What to check weekly:

- **Quote → PrintLogic correlation rate.** Pick 5 random confirmed
  quotes, verify each has a non-DRY `printlogic_order_id` and that the
  order exists in Justin's PrintLogic. Drift here means the auto-push
  on `confirm_order` is failing silently.
- **Stripe payment success rate.** `paid / (paid + failed + unpaid > 7d)`
  via the Quotes table. <80% means the customer-facing checkout flow
  has a regression.

---

## Rollback recipes summary

| Failure mode | Action | Time to safe |
|---|---|---|
| Stripe creating wrong charges | Dashboard → Connections → Stripe → toggle OFF | <30s |
| PrintLogic pushing garbage orders | Dashboard → Connections → PrintLogic → toggle OFF (= dry-run) | <30s |
| Missive spamming Justin's drafts | Dashboard → Connections → Missive → toggle OFF | <30s |
| Cloud Run crashing | `gcloud run services update craig-pricing --traffic 0` | ~2 min |
| Database corruption | Restore Cloud SQL from automated backup (last 7 days, 24h granularity) | ~10 min |

---

## Acceptance bar

This checklist is "done" when, having followed it top-to-bottom:

1. The smoke email in step 2d landed a draft in Justin's Missive ✓
2. The test card in step 3d marked a quote as paid ✓
3. The sentinel quote in step 4 produced a real payment AND a real
   refund, both reflected in the dashboard ✓
4. The sentinel order in step 5 appeared in Justin's PrintLogic AND
   was successfully cancelled ✓
5. The Overview health card shows all three integrations **green** or
   intentional yellow (PrintLogic dry-run) ✓

If any step fails, **stop**, document what happened, fix it, re-run the
failing step. Do not skip and hope.
