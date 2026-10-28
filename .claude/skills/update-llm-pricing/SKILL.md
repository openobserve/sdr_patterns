---
name: update-llm-pricing
description: Update llm_pricing.json with new LLM models and changed prices, verified against each provider's official pricing page. Use when asked to update model pricing, add new models, or run the pricing update locally.
---

Run the same update the weekly `Update LLM pricing` workflow runs, from this checkout.

1. `python3 scripts/check_llm_pricing_drift.py llm_pricing.json > drift-report.md` and read it.
2. Follow `scripts/update_llm_pricing.md` exactly. Use WebFetch on the official pages it lists;
   never write a price you did not read on the provider's own page in this run.
3. `python3 scripts/validate_llm_pricing.py llm_pricing.json` until it passes.
4. Show the user the changes as the table described in the instructions, with a source URL per
   row, and ask before committing or opening a PR. Delete `drift-report.md` and
   `pricing-update-pr.md` afterwards; they are not committed.
