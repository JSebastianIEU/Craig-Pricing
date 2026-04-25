# TODO: Migrate to Stripe Connect for one-click onboarding

**Status:** documented for future work, NOT scheduled.
**Priority:** medium — pays off from the second client onwards.
**Effort:** 1-2 dev-days.

---

## Why this matters

Today every new tenant has to:

1. Generate `sk_live_...` from their Stripe dashboard
2. Register a webhook endpoint and copy `whsec_...`
3. Paste both into our dashboard
4. Toggle `stripe_enabled=true`

That's ~15 minutes of guided setup per tenant, and **we end up storing
their secret API key** in our DB (encrypted, but still — defense in
depth says don't store what you don't need to).

**Stripe Connect** flips this:

1. Tenant clicks **"Connect with Stripe"** in our dashboard
2. Redirected to Stripe's OAuth flow → authenticates as their account
3. Returned to us with a `stripe_user_id` and OAuth-scoped tokens
4. We charge / refund / create payment links **on their behalf** using
   our platform key + their `stripe_user_id`

Benefits:
- **One click** for the tenant — no copy-paste, no key generation
- **We never see their secret key** — Stripe issues us a scoped token
- **Webhook secret can be auto-provisioned** via Stripe Connect API
- **Standard pattern** used by Shopify, Substack, Lemon Squeezy, every
  modern SaaS that processes payments for clients

Drawbacks:
- Requires creating a **Strategos AI platform account** on Stripe (KYC
  business verification, ~30 min for Roi)
- We become subject to Stripe's platform-level rules (compliance reviews
  if Strategos's volume grows past their thresholds — irrelevant
  immediately)
- Existing tenant (Just Print, configured manually) needs a one-time
  migration to Connect

---

## Implementation outline

### Backend changes

1. **New columns on the per-tenant Settings (or a new `StripeAccount` table):**
   - `stripe_account_id` — Connect account id (`acct_...`)
   - `stripe_access_token` — OAuth refresh token (encrypted via existing crypto layer)
   - `stripe_connected_at` — when the OAuth flow completed
   - `stripe_user_email` — the email Stripe associates with the account

2. **OAuth callback endpoint:**
   ```python
   @router.get("/oauth/stripe/callback")
   def stripe_oauth_callback(code: str, state: str): ...
   ```
   `state` carries the `org_slug` (HMAC-signed to prevent tampering).
   Exchange `code` → `access_token` → store on tenant.

3. **Modify `stripe_client.create_payment_link`:**
   - Today: uses tenant's `sk_live` directly via Basic Auth
   - Connect: uses Strategos platform key + `Stripe-Account: acct_...`
     header (the on-behalf-of pattern)

4. **Webhook auto-registration:**
   - When tenant connects, immediately call `POST /v1/webhook_endpoints`
     scoped to their account, set our URL, store the returned `whsec_`.
     Auto-subscribe to `checkout.session.completed`,
     `payment_intent.succeeded`, `payment_intent.payment_failed`,
     `charge.refunded`.

### Dashboard changes

1. **StripeTab.tsx:**
   - Replace the "paste secret key + paste whsec" flow with a single
     `<Button>Connect with Stripe</Button>` that opens the OAuth URL
     in a popup
   - On success: refresh the tab, show "Connected to acct_xxx (email)"
     with a "Disconnect" button
   - The `Test connection` button stays — same as today but uses the
     Connect tokens

2. **Pricing for the platform fee:**
   - Stripe Connect "Standard" type → no platform fee (money flows
     directly to tenant). Use this. We don't take a cut.
   - "Express" / "Custom" charge platform fees — out of scope.

### Migration for Just Print (one-off)

1. Create the platform account on Stripe (Roi)
2. Deploy the Connect endpoints to staging
3. Justin clicks "Connect with Stripe" once → his existing account links
4. We delete his stored `sk_live` and `whsec` from the Settings table
5. Verify a test payment round-trips with the new flow

---

## When to do this

**Trigger conditions** (any one):
- We're onboarding our 2nd client
- A security audit specifically flags "we store tenant secret keys"
- Stripe sends us a deprecation notice on the API key in URL header
  pattern (unlikely, but)

**Until then**: manual paste-and-save flow is fine. The encryption-at-rest
layer added in `secrets_crypto.py` mitigates the immediate risk.

---

## Reference

- Stripe Connect docs: https://stripe.com/docs/connect
- OAuth flow: https://stripe.com/docs/connect/oauth-reference
- Direct charges with Connect:
  https://stripe.com/docs/connect/direct-charges#using-the-stripe-account-header
- Migration guide for moving existing accounts:
  https://stripe.com/docs/connect/standard-accounts#migrating
