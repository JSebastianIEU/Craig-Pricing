# Stripe Connect runbook

**Status:** implemented and deployed.
**What it replaces:** the manual paste-`sk_***` + paste-`whsec_***`
flow that was the original Phase B.

This doc is the operator's runbook for getting Stripe Connect live for
the platform (one-time, Roi's job) and for onboarding any new tenant
(per-tenant, ~30 seconds for the user).

---

## Why Connect

Onboarding a tenant pre-Connect:
1. Generate `sk_live_***` from their Stripe dashboard
2. Register a webhook endpoint on Stripe's side
3. Copy the `whsec_***` signing secret
4. Paste both into our dashboard

~15 minutes per tenant, plus we end up custodian of their secret API keys.

Onboarding a tenant post-Connect:
1. Click **"Connect with Stripe"**
2. Authorize on Stripe's consent screen
3. Done

~30 seconds. We never see, store, or transmit their secret key — Stripe
issues us scoped tokens. Same UX pattern as Shopify, Substack, Lemon
Squeezy.

---

## One-time platform setup (Roi)

These six steps are blocked by Stripe's KYC review for any new platform.
Allow ~1 day for approval.

### 1. Apply for Connect on the Strategos AI Stripe account

[Dashboard → Settings → Connect](https://dashboard.stripe.com/settings/connect/onboarding-options).
Choose **Standard** account type. Standard means:
- Each tenant has their own Stripe account
- Money flows directly to the tenant's bank
- No platform fee unless we explicitly add one (we don't)
- Tenants manage their own payouts, refunds, disputes in their dashboard

### 2. Get the OAuth client id

Once approved, in Connect → Settings → Integration, find
**OAuth settings**. The `client_id` looks like `ca_*****************`.
Copy it.

### 3. Generate platform secret keys

Two of them:
- **Test:** `sk_test_strategos_***` — for staging + local dev
- **Live:** `sk_live_strategos_***` — for prod

These are the platform's "I am Strategos" keys. They authenticate every
API call we make to Stripe. We then add a `Stripe-Account: acct_xxx`
header on each call to act on behalf of a connected tenant.

### 4. Configure OAuth redirect URI

In Connect → Settings → Integration → Redirects, add the exact URL of
our backend callback:

```
https://craig-pricing-277215252762.europe-west1.run.app/admin/api/oauth/stripe/callback
```

If we ever change Cloud Run URLs, this must be updated in lockstep or
Stripe will reject the OAuth flow with `redirect_uri_mismatch`.

### 5. Configure platform-level webhook endpoint

In Webhooks → **Add endpoint**:
- URL: `https://craig-pricing-277215252762.europe-west1.run.app/admin/api/webhooks/stripe-connect`
- **Mode: "Connected accounts"** ← critical, NOT "Your account"
- Events to subscribe to:
  - `checkout.session.completed`
  - `payment_intent.succeeded`
  - `payment_intent.payment_failed`
  - `charge.refunded`

Copy the resulting signing secret (`whsec_***`).

This single endpoint receives events from EVERY connected tenant. Each
event has an `account: 'acct_xxx'` field at the top level — our handler
looks up which tenant owns that account_id and routes the event to
their Quote table.

### 6. Provision the 3 secrets in Google Secret Manager

```bash
gcloud secrets create strategos-stripe-platform-key \
    --replication-policy=automatic
echo -n "sk_live_strategos_***REPLACE***" \
    | gcloud secrets versions add strategos-stripe-platform-key --data-file=-

gcloud secrets create strategos-stripe-connect-client-id \
    --replication-policy=automatic
echo -n "ca_***REPLACE***" \
    | gcloud secrets versions add strategos-stripe-connect-client-id --data-file=-

gcloud secrets create strategos-stripe-connect-webhook-secret \
    --replication-policy=automatic
echo -n "whsec_***REPLACE***" \
    | gcloud secrets versions add strategos-stripe-connect-webhook-secret --data-file=-
```

Grant the Cloud Run service account access:

```bash
SA=277215252762-compute@developer.gserviceaccount.com
for SECRET in strategos-stripe-platform-key \
              strategos-stripe-connect-client-id \
              strategos-stripe-connect-webhook-secret; do
    gcloud secrets add-iam-policy-binding $SECRET \
        --member=serviceAccount:$SA \
        --role=roles/secretmanager.secretAccessor
done
```

Mount them as env vars on the Cloud Run service:

```bash
gcloud run services update craig-pricing --region=europe-west1 \
    --update-secrets=\
STRATEGOS_STRIPE_PLATFORM_KEY=strategos-stripe-platform-key:latest,\
STRATEGOS_STRIPE_CONNECT_CLIENT_ID=strategos-stripe-connect-client-id:latest,\
STRATEGOS_STRIPE_CONNECT_WEBHOOK_SECRET=strategos-stripe-connect-webhook-secret:latest
```

Cloud Run redeploys automatically. Within 30 seconds, the new revision
is live and the dashboard's StripeTab "Connect with Stripe" button
becomes functional.

---

## Per-tenant onboarding (the user)

When a new client (e.g. Just Print) wants to enable Stripe payments:

1. Sign in to https://agents.strategos-ai.com → their workspace
2. Navigate to **Craig → Connections → Stripe**
3. Click **"Connect with Stripe"**
4. Stripe takes over: choose existing account or create new one (~2 min if new)
5. Approve the consent screen
6. Land back on the dashboard with a green "Connected to Stripe" toast

After this, every confirmed quote auto-generates a Payment Link. Money
goes directly to the tenant's bank — Strategos doesn't custody funds.

---

## Disconnect / re-connect

The tenant can click **Disconnect** anytime in the StripeTab. That:
1. Calls Stripe's `/oauth/deauthorize` to revoke our access
2. Clears the local `stripe_account_id`, `stripe_access_token`,
   `stripe_publishable_key`, `stripe_connected_at`, `stripe_user_email`

Existing Payment Links stay valid until the tenant cancels them in
Stripe directly. Already-paid quotes are unaffected.

To reconnect: click **Connect with Stripe** again. The tenant goes
through OAuth a second time. Each fresh connection mints new tokens.

---

## Code map

| File | Role |
|---|---|
| `stripe_connect.py` | OAuth helpers — state signing/verify, code exchange, deauthorize. Reads platform creds from env vars at module load |
| `stripe_client.py` | HTTP client for Stripe REST API. `account_id` param gates the Stripe-Account header injection |
| `stripe_push.py` | Orchestrator — reads `stripe_account_id` (not `stripe_secret_key`), passes it through `create_payment_link` |
| `admin_api.py` | Endpoints: `POST /orgs/:slug/oauth/stripe/authorize-url`, `GET /oauth/stripe/callback`, `POST /orgs/:slug/oauth/stripe/disconnect`, `POST /webhooks/stripe-connect`, `GET /orgs/:slug/integrations/stripe/connect-status` |
| `scripts/v16_stripe_settings_seed.py` | Seeds the 5 new Connect-era setting keys with empty placeholders |
| `scripts/v18_stripe_connect_migration.py` | Deletes legacy `stripe_secret_key` + `stripe_webhook_secret` rows |
| `settings_security.py` | `SECRET_KEYS` includes `stripe_access_token` (the only Stripe secret we still custody, encrypted via Fernet) |

Tests: `test_stripe.py` (auth-path updates) + `test_stripe_connect.py`
(state signing, code exchange, webhook routing). Default suite: 158
tests, all green.

---

## Rollback

If something is misbehaving badly, `stripe_enabled=false` per tenant
disables link creation without disconnecting. Setting takes effect on
the next quote confirmation (no deploy required).

If the platform-level whsec leaks: rotate it in Stripe Connect →
Webhooks → endpoint → Signing secret → "Roll secret". Update Secret
Manager:

```bash
echo -n "whsec_NEW_***" \
    | gcloud secrets versions add strategos-stripe-connect-webhook-secret --data-file=-
gcloud run services update craig-pricing --region=europe-west1
```

Cloud Run picks up the new secret on next deploy. The old whsec keeps
working for ~24h per Stripe's grace period.

If the platform OAuth client_id is compromised: revoke it in Connect
settings, generate a new one. ALL tenants will need to re-connect (the
old `stripe_user_id` values become invalid). This is a nuclear option.

---

## Out of scope

- Stripe Connect Express / Custom (we only support Standard)
- Embedded Stripe.js / Elements (Payment Links work; Elements is future)
- Payouts / disputes UI (handled in tenant's own Stripe dashboard)
- Reacting to `account.application.deauthorized` (when a tenant revokes
  from Stripe's side without telling us — they re-connect to recover)
