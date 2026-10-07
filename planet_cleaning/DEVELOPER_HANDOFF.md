# Planet Networks cleaning scripts: developer handoff

Version 2.1.0. Start here. Jake owns the confirmed channel business rules.

## Request

Integrate the revised cleaning/classification logic into the job that powers the
existing paid-media dashboard. Jake does not operate that dashboard directly.
This package is code and documentation; it contains no customer-level export.
Use Jake's original `Results 9-11-26.csv` to reproduce the supplied verification counts.

## Confirmed rule, including future campaigns

Read the exported/server-side **MarketingToken** field. Trim whitespace and compare
case-insensitively. If it starts with the literal prefix `MP-`, classify the entry as
**Paid**. This includes MP-AUG-26, MP-SEP-26, MP-OCT-26, and future MP- codes. Do not
require monthly edits or a populated UTM campaign. Do not use URL `token=` as a
replacement for the exported field. A token merely containing MP- elsewhere does
not match.

This version applies the rule to all processed rows, including historical MP- codes.
The MP- business rule has priority over contradictory nonpaid tracking; record the
conflict in review_flags. Other entries retain the email/AI protections in the code.

An MP- token confirms paid media, not the platform. Preserve a supported Google,
Meta, Bing, etc. attribution when independent evidence identifies it. Otherwise
use Paid (other). Keep one channel per row, with match_tier=confirmed_mp_prefix and
the evidence retained. Never add the token count on top of already-counted paid leads.

## What to use

| File | Purpose |
| --- | --- |
| pipeline.py | Full loader, normalizer, classifier, date filtering/deduplication, quality checks and fixed-window payload builder. |
| confirmed_tokens.json | Empty mapping for additional confirmed exact codes. MP- is built into the code. Keep this file next to pipeline.py. |
| test_pipeline.py | 51 synthetic regression tests. |
| requirements.txt | Python dependencies. |
| README.md | Detailed rules, output contract and run options. |
| expected_counts.json | Reconciliation results from the September 11 export under version 2.1.0. No customer-level rows. |

Python 3.10+:

```bash
python -m pip install -r requirements.txt
python -m unittest -q test_pipeline.py
python pipeline.py "Results 9-11-26.csv" september_01_10_v210.json --start 2026-09-01 --end 2026-09-10
```

The supplied SQL extraction query can remain unchanged. Input is a CSV with headers
or the exact supplied 26-column order without headers. Use a new output name for each
run. The original export is never modified.

## Required dashboard integration

The existing dashboard JavaScript was not supplied or inspected. The standalone
JSON is schema v2 and is NOT a drop-in replacement for the old all-history payload.
Integrate the functions or update the dashboard's data contract before deployment.

Two supported integration approaches:

1. **Keep the current dashboard's all-row dataset.** Port/reuse load,
   normalize_rows, classify, and the quality checks, while preserving all valid
   classified submissions for the dashboard. Reused-ID checks must see the full
   supplied history. For every requested reporting window, filter dates first,
   sort chronologically, keep the earliest row per email hash in that window, THEN
   count channel and availability. Do not globally dedupe before the date filter.
   Validate equivalent results against select_period and expected_counts.json.
2. **Use the standalone fixed-window pipeline.** Run it for each requested date
   range and display that range's already-deduplicated payload. Regenerate from the
   original export when dates change. Filtering an old deduplicated payload cannot
   reproduce earliest-in-new-period counts.

For either approach, preserve these semantics:

- Paid Available Now = one retained email hash with channel_group=Paid AND
  AvailabilityID=1. This is not the number of all paid inquiries or completed orders.
- Keep the earliest submission's channel and availability. Do not select only
  Available Now rows before dedupe, or silently promote a later status/channel.
- Source data dates are assumed America/New_York; confirm the actual database
  timezone. The new payload's ts is real UTC Unix seconds. Do not apply an extra
  Eastern offset to it. The old script encoded naive local time differently.
- Exclude today's incomplete day by default; label it explicitly if included.
- Do not replace municipality with a ZIP-wide mode. New ZIP lookup entries are
  keyed by ZIP/municipality/state and can repeat the same ZIP.
- Keep match_tier and review_flags available. A missing UTM campaign on a Google
  auto-tagged visit is not automatic proof of invalid paid attribution.
- Do not use the former hard-coded 195675 spend. Supply actual spend for the exact
  dates and channel scope. Missing spend/cost must display unavailable, not zero.
  Platform CPA requires matching platform spend; a period-wide scalar is insufficient.
- Preserve validation failures and completeness checks. Use an earlier successful
  snapshot for daily-count comparisons. The original SQL view and export mechanics
  still need independent completeness checks.

## Verification against the September 11 export

Use the original 18,163-row export, with no input rows removed. The 26 partial-day
September 11 rows are outside the reporting periods below. Filter before dedupe.

| Dates, inclusive | Paid Available Now |
| --- | ---: |
| August 1–31 | 201 |
| September 1–10 | 65 |
| September 4–10 | 46 |

These now include all confirmed MP- tokens, including MP-AUG-26. Earlier reports
with 64/45 used only the exact MP-SEP-26 confirmation and are superseded by this
broader business rule. Counts apply to this export snapshot and metric definition.

Please confirm the dashboard matches these counts and shows the correct period,
qualification definition, unknown platforms, and spend scope before using it for
the next paid-media readout. No dashboard was changed or deployed in this handoff.
