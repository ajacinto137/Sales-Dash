# Planet Networks: corrected form-data cleaning and attribution

Version 2.1.0. Complete replacement Python pipeline, with synthetic regression tests.
Jake confirmed ALL MarketingToken values beginning with MP- mean Paid. This built-in
case-insensitive prefix rule handles MP-AUG-26, MP-SEP-26, and future campaign codes
without monthly edits. It applies to every row processed by this version, including
historical rows; no effective-date cutoff was supplied. The token does not establish
Meta or any other platform; independent platform evidence is preserved when present.
The optional exact-token mapping file remains available for other confirmed codes.
The September 11 export was subsequently supplied and used to add explicit aliases
for meta_fb, meta_ig, meta_an and meta_th and correct cookie/token coexistence.
An unconfirmed server-side token no longer blocks otherwise eligible recent Google
cookie evidence. Actual export results are delivered separately.
The existing dashboard JavaScript has not been supplied or verified.

## Run it

Keep your SQL extraction query as provided. Save the results as CSV with or without
headers. A headerless file must retain the exact 26-column order from that query.

Install the dependencies once, using Python 3.10 or newer:

```bash
python -m pip install -r requirements.txt
```

Run a specific reporting period. Both dates are inclusive:

```bash
python pipeline.py Results_latest.csv report_2026-09-01_to_07.json --start 2026-09-01 --end 2026-09-07
```

With no dates supplied, the reporting window is March 1, 2026 through yesterday:

```bash
python pipeline.py Results_latest.csv report_latest.json
```

Use a new output filename for each run. This prevents a failed run from leaving an
older payload that appears current. Input exports and previous outputs are not overwritten.

Supply actual spend only when it covers the exact selected dates and the same Paid
channel scope. The following dollar amount is a syntax example, NOT Planet spend:

```bash
python pipeline.py Results_latest.csv report_week_with_spend.json --start 2026-09-01 --end 2026-09-07 --spend-total 1000
```

No spend is assumed by default. Cost per paid Available Now lead stays null if spend
is absent or the denominator is zero. There is no platform or daily spend allocation.
Do not combine all-media spend, including unattributed TV/radio influence, with a
click-attributed denominator and label it channel CPA. The script cannot verify a
manually entered spend figure or its scope.

## What comes out

For `report_week.json`, the script produces:

| File | Contents |
| --- | --- |
| `report_week.json` | Schema v2 fixed-period payload and summary; only written when blocking checks pass or completeness concerns are explicitly acknowledged. |
| `report_week.cleaned.csv` | One retained person per email hash in the selected period, including channel, original data, evidence, and flags. |
| `report_week.audit.csv` | Every valid submission in the selected period, including duplicates and whether it was retained. |
| `report_week.rejected.csv` | Invalid rows from the entire export, with rejection reasons. |
| `report_week.quality.json` | Validation status, completeness checks, warnings, input checksum, and rule settings. |
| `report_week.snapshot.json` | Raw daily counts for comparison with the next export. |

Exit code 0 means a payload was produced. Exit code 2 means it was not.
Review flags are informational/conflict indicators; they are not all automatic
exclusions. `paid_available_needs_review` shows how many paid Available Now leads
have such flags. Missing manual campaign UTMs, for example, do not invalidate a GCLID.

## Exact counting definition

1. Validate and normalize the full SQL export.
2. Classify submissions using their recorded evidence. Check reused IDs against all
   valid history in the export, including submissions before the reporting period.
3. Filter to the selected local reporting dates.
4. Keep the earliest submission per normalized email hash within those dates. Ties
   use input-row order. For reproducibility across exports, add a real unique row
   identifier and use it as a tie-breaker if one exists; QuoteID is not assumed unique.
5. Count channel and AvailabilityID from that retained row.

This preserves your requested earliest-in-period policy. An April submission does
not remove the same person's September submission from a September report.

If someone's first submission within the period is Unavailable and their next one is
Available Now, they remain Unavailable in this metric. If their first submission is
organic and their second is paid, they remain organic. Both entries are visible in
the audit. Changing these behaviors would change the business metric, so this
replacement does not silently do so.

Counts represent unique email hashes, not verified people, households, sales,
installations, or new customers. Available Now indicates serviceability. Call it an
SQL only if that is Planet's agreed SQL definition. Weekly unique counts are not
necessarily additive to monthly unique counts.

## Corrected attribution rules

Strings are trimmed, null placeholders removed, and source/medium comparisons
normalized for case, spaces, and underscores. Click-ID values are not lowercased.
Missing optional tracking fields can be recovered from the landing URL's query
parameters. Exported field values win on conflict, and conflicts are flagged.
MarketingToken is never recovered from the URL's `token=` parameter.

### Precedence

| Priority | Evidence | Assignment |
| --- | --- | --- |
| 0 | MarketingToken starts with MP-, after trimming and case normalization | Paid. Latest user-confirmed business rule. Contradictory email/AI/nonpaid tracking is flagged but does not change the Paid group. |
| 1 | Email medium/source, `EM` followed by a digit, or a confirmed email token | Email. Conflicting paid evidence is flagged, never allowed to overwrite it. |
| 2 | Recognized ChatGPT, Copilot, or Perplexity source, or confirmed AI token | AI-Referral. Paid and other token evidence do not overwrite it. |
| 3 | Explicit `social`, `organic`, `organic-social`, or `referral` medium | Organic-Direct with a descriptive detail. Contradictory paid evidence is flagged. |
| 4 | Recognized paid medium, trusted advertising identifier, confirmed paid token, or eligible recent Google advertising cookie | Paid, with a platform when supported. Cookie evidence is explicitly marked inferred. |
| 5 | Confirmed exact MarketingToken mapping | Its verified group/detail. |
| 6 | Any other MarketingToken | Offline-Referral / Unconfirmed token, requiring mapping review. This is a provisional compatibility bucket, not proof the lead was offline. |
| 7 | Insufficient or absent attribution | Organic-Direct / Unattributed-review or Direct-unknown. These are not claims of organic acquisition. |

Outside the confirmed MP- rule, explicit email, AI, and nonpaid tagging remain
protected. A conflicting GCLID is a tagging problem to investigate. The MP- rule is
an intentional exception based on Jake's latest instruction, not a generic paid
override. It records conflicts for review.

### Paid evidence

Recognized paid mediums: `cpc`, `ppc`, `paid`, `paid-social`, `paid-search`,
`paidsocial`, `paidsearch`, `cpm`, `cpv`, `display`, `retargeting`. Normalization also
handles forms such as `paid_social` and `paid social`.

Advertising identifiers: `gclid`, `msclkid`, `ndclid`, `gbraid`, `wbraid` and
`gad_source`, subject to precedence and reuse rules. These are attribution signals,
not externally authenticated proof. Capture provenance and copied links still matter.

`fbclid` alone never proves paid. `meta`, `adwords`, `bing`, `reddit`, `unbounce`,
`leadpages`, or a custom `tw_source=google` alone do not prove paid either. They need
independent advertising evidence. Source-only legacy tags can consequently lose paid
classification; review those rows against your campaign setup before replacing old
published totals. A populated campaign name alone is not proof of paid acquisition.

Explicit paid source/medium identifies the platform ahead of conflicting IDs, with
a flag. If several advertising platforms match without a resolving paid UTM pair,
the detail is `Paid (platform conflict)`, rather than arbitrarily choosing the last
one. A paid medium with an unknown source is `Paid (other)` and flagged.

Blank `utm_campaign` generates a review flag, not automatic demotion: Google
auto-tagging can work without manual campaign UTMs. Reference:
[Google tagging documentation](https://support.google.com/analytics/answer/11242870?hl=en).

### Click-ID reuse

- Retain the first email hash seen for an ID, including its later submissions.
- Different hashes within 60 minutes retain the original policy's eligible ad-ID
  evidence but get shared-ID flags. This window is a chosen rule, not proof that the
  additional users clicked an ad.
- Later use by another hash loses that particular ID as evidence. A co-traveling
  `gad_source` cannot rescue a reused GCLID. Independent paid UTMs, confirmed paid
  tokens, or other eligible identifiers can still support Paid.
- Apply reuse checks to GCLID, FBCLID, MSCLKID, and NDCLID. FBCLID is never sufficient
  for Paid even when trusted. Do not demote GBRAID/WBRAID because they repeat.
- Earliest means earliest within the supplied export, not lifetime first use.

### Google cookie fallback

Only `_gcl_aw`, never `_gcl_au`. Decode the supplied URL/linker format, require a
complete `GCL.timestamp.clickid` structure, and compare timezone-aware UTC times.
Require `0 <= submission time - cookie time < 3600 seconds`. Future, malformed, and
older cookies cannot qualify. All UTM, click-ID, and custom tracking fields must be
absent. Outside the MP- exception, explicit email, AI and nonpaid medium decisions take precedence. A server-side
marketing token does not disqualify a recent cookie. Email tokens remain protected.
This is a chosen attribution window, not proof of a single browser session.

## Tokens: confirm instead of guessing

The MP- prefix is built into pipeline.py and does not need an exact mapping entry.
`confirmed_tokens.json` starts empty for additional confirmed codes. Other
name-shaped strings, PC1, agency codes that do not start MP-, short codes, and dated
codes are not assigned invented meanings. Keep the file alongside pipeline.py;
it loads automatically. An exact mapping cannot override the confirmed MP- Paid rule.

After your team confirms a code, add its exact value. For example, ONLY if PC1 is
confirmed as direct mail:

```json
{
  "PC1": {
    "channel_group": "Offline-Referral",
    "channel_detail": "Direct mail"
  }
}
```

Use `--token-map another_file.json` to select a different map. Matching is case-insensitive and
exact; the separately built-in MP- rule is the only confirmed paid prefix. Source/medium and click-ID precedence
still apply. ReferralData is preserved and flagged for review but not interpreted
without an agreed mapping. Never infer a token represents paid because an agency made it.

## Time, ZIPs and validation

Naive InsertDate values are interpreted as `America/New_York`, matching your stated
assumption. Confirm this with the database owner. `GETDATE()` alone does not prove
the stored timestamp's timezone. `--timezone` changes both the interpretation of
naive dates and reporting calendar. Explicit-offset timestamps are converted correctly.
Ambiguous/nonexistent DST wall times are quarantined rather than guessed. No year or
summer/winter offsets are hard-coded. The current day is excluded by default; if
explicitly requested, the payload and quality report label it partial.

AvailabilityID must be an integer value 0–6, non-null. Email hashes must match
64 hexadecimal characters. Invalid dates, future submissions, statuses, or hashes
block payload creation and appear in the rejected file. Optional columns may be
absent from headered input; missing columns are listed in the quality report.

ZIP cleanup accepts 1–5 digits, integer-style `.0` suffixes, ZIP+4, and 9-digit ZIPs.
It restores leading zeros but does not extract arbitrary digits from malformed text.
Missing/invalid ZIPs remain blank and flagged. Raw ZIPs are preserved.

Without a reference, state is only an explicitly flagged NJ/NY/PA/VA prefix estimate,
or Unknown. Prefixes are not full ZIP validation. For exact state reporting, supply
an authoritative CSV with `Zipcode,State` via `--zip-lookup`. Municipality is retained
per submission, never replaced by a ZIP-wide majority label. No unverified nationwide
ZIP database is bundled.

## Completeness checks

Use the previous successful run's snapshot on subsequent runs:

```bash
python pipeline.py Results_latest.csv report_next_run.json --start 2026-09-01 --end 2026-09-07 --previous-snapshot report_2026-09-01_to_07.snapshot.json
```

The script checks for historical daily decreases over overlapping complete dates,
counts divisible by 5,000, and a last row before the expected complete reporting day.
Heuristic findings block the dashboard payload by default. A missing prior snapshot
is disclosed, not treated as a fabricated successful comparison. Prior blocked
snapshots cannot be used as baselines. Keep the same source population across runs.

A genuinely zero-entry ending day or an exact 5,000-row export can be legitimate.
After verifying against SQL/export counts, rerun with a NEW output filename and
`--completeness-note "Explanation of the verified counts or genuine zero-entry days"`.
The explanation and acknowledged findings are saved. This cannot bypass invalid rows
or structural range errors. Do not use this option to dismiss unexplained losses.

These checks cannot prove that the SQL view itself is complete or free of join
duplication, or detect every possible truncation. SQL row counts and view definitions
remain the upstream evidence for those questions.

## Existing dashboard integration

**Schema v2 is NOT a drop-in replacement for the uninspected dashboard JavaScript.**
Use the cleaned CSV and summary immediately for the requested fixed reporting period.
Adapt the dashboard before connecting the new JSON.

- Arrays contain one retained email per selected reporting period, not all history.
- Changing the reporting dates requires rerunning the pipeline from the full export.
  Filtering already-deduplicated arrays cannot reproduce earliest-in-new-period counts.
- Read `reporting_window`, `grain`, `schema_version`, and `summary`.
- `ts` is now real UTC Unix seconds. Display dates in the declared reporting timezone.
- The ZIP lookup is keyed by ZIP/municipality/state together, preserving municipality.
  The same ZIP can therefore occur more than once in `zips`.
- Null spend/cost must display as unavailable, not zero. There is no supported way to
  recalculate platform/date-filtered CPA from one period-wide spend scalar.
- Do not hide review flags or describe all Organic-Direct rows as organic.
- View-through CTV/radio impact, causal incrementality, customer qualification, and
  agency platform attribution are not inferred from these form fields.

## Verify the implementation

```bash
python -m unittest -q test_pipeline.py
```

The suite uses synthetic data and covers attribution conflicts, organic FBCLID,
paid-medium aliases, protected email/AI, cookies and age boundaries, repeated IDs,
deduplication order, DST, ZIP handling, invalid data, export health, and payload grain.
