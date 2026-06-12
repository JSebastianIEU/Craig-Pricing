# Product Test Report — runbook

Generates a client-ready PDF report of **complete test conversations for every
product** against the live Craig agent, plus every quote PDF those
conversations produced — then deletes all test data from prod. Built to hand
to Justin so he can *read* the product coverage instead of testing
product-by-product (pairs with the "How Craig Prices Your Products" guide).

## What a run produces

```
~/JustPrint/craig-test-reports/<YYYY-MM-DD>/
├── Craig-Product-Test-Report.pdf    # cover · clickable TOC · one section per
│                                    # product group · full chat transcripts as
│                                    # bubbles · PASS/CHECK per scenario ·
│                                    # appendix with every quote
├── quotes/
│   ├── JP-0265.pdf                  # the real branded quote PDF, one per
│   └── ...                          # quote, named by quote number
└── run-manifest.json                # machine-readable run record (ids,
                                     # results, recovery data)
```

## How to run

```bash
cd Craig-Pricing
source .venv/bin/activate
set -a && source .env && set +a        # needs STRATEGOS_JWT_SECRET

# See the scenario plan without touching the network:
python -m scripts.generate_product_test_report --dry-run

# Small pilot (any subset of group keys):
python -m scripts.generate_product_test_report --groups posters,ncr

# Full suite (~42 scenarios, ~30-40 min):
python -m scripts.generate_product_test_report
```

Flags:

| Flag | Meaning |
|---|---|
| `--groups a,b,c` | run only those group keys (see `--dry-run` for the list) |
| `--out-dir PATH` | output folder (default `~/JustPrint/craig-test-reports/<date>`) |
| `--keep-data` | skip cleanup — conversations stay visible in the dashboard |
| `--cleanup-manifest PATH` | ONLY delete the conversations recorded in a manifest (recovery after an interrupted run) |
| `--base-url URL` | target another deployment (default: prod Cloud Run) |
| `--concurrency N` | parallel scenarios (default 3 — keeps under the 30/min chat rate limit) |

## How it works (4 phases)

1. **Run** — drives each scenario's turns through the real public `/chat`
   endpoint, exactly like the website widget does. (NOT the v35 test-chat
   sandbox: that injects a TEST MODE prompt that skips the artwork/contact
   funnel, so the report would show behaviour no customer ever sees.)
2. **Harvest** — fetches the authoritative transcript per conversation
   (admin API, JWT) and downloads each quote's PDF from `GET /quotes/{id}/pdf`.
3. **Report** — builds the master PDF (ReportLab, `multiBuild` for the TOC
   page numbers + sidebar bookmarks).
4. **Cleanup** — deletes **exactly** the conversation ids this run created
   (DELETE cascades quotes), then verifies they are gone. Runs in a `finally`
   block, so even a report-build crash can't strand test data. If the process
   is killed hard, recover with `--cleanup-manifest <out>/run-manifest.json`.

## Safety properties

- **Zero notification emails.** The v33 approval email fires only on the
  widget FORM submit or a Missive confirm_order. The suite gives contact
  details **as chat text only**, which never triggers it.
- **Deletes are id-scoped.** Only the conversation ids recorded in this run's
  manifest are ever deleted — never a range or a filter.
- **Dashboard visibility window.** Test conversations exist in the dashboard
  only between phases 1 and 4 (~the run duration). Run at a sensible time if
  the client shouldn't see them transiently.
- The quote-id counter (JP-xxxx) advances permanently — harmless.

## Editing / adding scenarios

Scenarios live in **`scripts/test_report_scenarios.py`** (data only — the
runner never needs touching). Each group has a `key`, `title`, `products`,
and a list of scenarios:

```python
{
    "name": "A5 double-sided — direct",
    "style": "direct",          # direct | vague | messy | e2e | edge | escalation
    "turns": ["Price for 500 A5 flyers, printed both sides please"],
    "expect": {                  # optional — drives ✓ PASS / (!) CHECK
        "price_contains": "132",        # € substring in any Craig reply
        "reply_contains": "...",        # substring must appear
        "reply_not_contains": "ply",    # word-boundary match must NOT appear
        "marker": "[QUOTE_READY]",      # marker must be emitted
        "quote_created": True,          # a PRICED quote row must exist
        "escalates": True,              # NO priced quote may exist
    },
}
```

Conventions:
- e2e scenarios answer the artwork question and give contact **as chat text**
  (`E2E_CONTACT` constant) and close the funnel ("Collection please…").
- Prices in `expect.price_contains` come from Justin's price lists — update
  them if the catalog prices change, or the report will flag false CHECKs.

## When to re-run

- Before a release Justin should sign off on.
- After loading a new product or changing prices/prompt rules.
- After any change to the funnel gates (artwork / contact / [QUOTE_READY]).
