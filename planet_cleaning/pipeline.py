#!/usr/bin/env python3
"""Planet Networks form attribution, version 2. See README.md before integration.

Requires Python >=3.10, pandas >=2.0, tzdata (on systems without an IANA database).
Example: python pipeline.py Results_latest.csv payload.json --start 2026-09-01 --end 2026-09-07
The end DATE is inclusive. Default: March 1, 2026 through yesterday, Eastern.
No source-system writes. Input exports are never modified.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pandas as pd

VERSION = "2.1.0"
COLUMNS = ['InsertDate', 'AvailabilityID', 'QuoteID', 'NJPR_municipalityName',
    'Zipcode', 'MarketingToken', 'ReferralData', 'email_id', 'uniqueURL',
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
    'utm_id', 'fbclid', 'msclkid', 'ndclid', 'tw_source', 'tw_adid',
    'tw_campaign', 'tw_kwdid', 'gad_source', 'gad_campaignid', 'gbraid', 'gclid']
TRACKING = COLUMNS[9:] + ['wbraid']
CLICK_IDS = ['gclid', 'fbclid', 'msclkid', 'ndclid', 'gbraid', 'wbraid']
UTMS = [c for c in TRACKING if c.startswith('utm_')]
GROUPS = ['Paid', 'Email', 'Offline-Referral', 'AI-Referral', 'Organic-Direct']
STATUSES = {0: 'Unavailable', 1: 'Available Now', 2: 'Coming soon, no ordering',
    3: 'Coming soon, preorder available', 4: 'Permitting',
    5: 'Strand Construction', 6: 'Fiber Construction'}
PAID_MEDIA = {'cpc', 'ppc', 'paid', 'paid-social', 'paid-search', 'paidsocial',
              'paidsearch', 'cpm', 'cpv', 'display', 'retargeting'}
EMAIL_SOURCES = {'sfmc', 'hs-email', 'hs-automation', 'newsletter', 'community-newsletter'}
AI_SOURCES = {'chatgpt', 'chatgpt.com', 'copilot', 'copilot.com',
              'copilot.microsoft.com', 'perplexity', 'perplexity.ai'}
PLATFORMS = {'google': 'Google', 'adwords': 'Google', 'googleads': 'Google',
    'google-ads': 'Google', 'facebook': 'Meta', 'facebook.com': 'Meta',
    'fb': 'Meta', 'instagram': 'Meta', 'instagram.com': 'Meta', 'ig': 'Meta',
    'meta': 'Meta', 'meta-fb': 'Meta', 'meta-ig': 'Meta', 'meta-an': 'Meta',
    'meta-th': 'Meta', 'bing': 'Bing', 'microsoft': 'Bing', 'microsoft-ads': 'Bing',
    'nextdoor': 'Nextdoor', 'next-door': 'Nextdoor', 'reddit': 'Reddit',
    'reddit.com': 'Reddit', 'mntn': 'MNTN', 'mountain': 'MNTN'}
NULLS = {'', 'null', 'none', 'nan', '(null)', 'undefined'}


def text(v):
    return '' if pd.isna(v) or str(v).strip().lower() in NULLS else str(v).strip()


def norm(v):
    return re.sub(r'[\s_]+', '-', text(v).lower())


def add_flag(row, value):
    row['review_flags'] = sorted(set(row.get('review_flags', []) + [value]))


def load(path):
    """Headered exports can omit optional columns. Headerless exports require 26."""
    with open(path, encoding='utf-8-sig', newline='') as f:
        first = next(csv.reader(f), [])
    if not first:
        raise ValueError('Empty input file.')
    headered = any(text(x).casefold() == 'insertdate' for x in first)
    df = pd.read_csv(path, header=0 if headered else None, dtype=str,
                     keep_default_na=False, encoding='utf-8-sig')
    if headered:
        canonical = {c.casefold(): c for c in COLUMNS + ['wbraid']}
        df.columns = [canonical.get(text(c).casefold(), text(c)) for c in df.columns]
        if df.columns.duplicated().any():
            raise ValueError('Duplicate column names after normalization.')
    else:
        if len(df.columns) != len(COLUMNS):
            raise ValueError(f'Expected 26 headerless columns; received {len(df.columns)}.')
        df.columns = COLUMNS
    required = {'InsertDate', 'AvailabilityID', 'email_id'}
    if required - set(df.columns):
        raise ValueError(f'Missing required columns: {sorted(required - set(df.columns))}')
    missing = sorted(set(COLUMNS) - set(df.columns))
    for col in COLUMNS + ['wbraid']:
        if col not in df:
            df[col] = ''
    df['input_row'] = range(1, len(df) + 1)
    return df, missing


def clean_zip(value):
    """Recover leading zeroes; never extract digits from arbitrary garbage."""
    s = text(value)
    if re.fullmatch(r'\d{1,5}\.0+', s):
        s = s.split('.')[0]
    if re.fullmatch(r'\d{1,5}', s):
        z = s.zfill(5)
    elif re.fullmatch(r'\d{5}-\d{4}', s):
        z = s[:5]
    elif re.fullmatch(r'\d{9}', s):
        z = s[:5]
    else:
        return '', 'invalid_zip' if s else 'missing_zip'
    return (z, '') if z != '00000' else ('', 'invalid_zip')


def load_zip_lookup(path):
    if not path:
        return {}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not {'Zipcode', 'State'}.issubset(df.columns):
        raise ValueError('ZIP reference needs Zipcode and State columns.')
    lookup = {}
    for row in df.to_dict('records'):
        z, error = clean_zip(row['Zipcode'])
        state = text(row['State']).upper()
        if error or not re.fullmatch('[A-Z]{2}', state):
            raise ValueError('Invalid ZIP reference row.')
        if z in lookup and lookup[z] != state:
            raise ValueError(f'Conflicting state reference for ZIP {z}.')
        lookup[z] = state
    return lookup


def state_for_zip(z, lookup):
    if not z:
        return 'Unknown', 'unknown'
    if z in lookup:
        return lookup[z], 'zip_reference'
    # Deliberately limited, approximate service-region fallback. No DC->VA mapping.
    # Exact ZIP reference wins; every inferred result is visibly marked estimated.
    p = int(z[:3])
    for lo, hi, state in [(70, 89, 'NJ'), (100, 149, 'NY'),
                          (150, 196, 'PA'), (220, 246, 'VA')]:
        if lo <= p <= hi:
            return state, 'prefix_estimate'
    return 'Unknown', 'unknown'


def timestamp(value, timezone):
    ts = pd.Timestamp(text(value))
    if pd.isna(ts):
        raise ValueError('Missing timestamp')
    if ts.tzinfo is None:
        # Ambiguous/nonexistent DST wall times are rejected, never guessed.
        ts = ts.tz_localize(timezone, ambiguous='raise', nonexistent='raise')
    return ts.tz_convert('UTC')


def normalize_rows(df, timezone, now, zip_lookup):
    rows, rejected = [], []
    for original in df.to_dict('records'):
        row = {k: text(v) for k, v in original.items()}
        for key in COLUMNS + ['wbraid']:
            row.setdefault(key, '')
        row['input_row'] = int(original['input_row'])
        row['review_flags'] = []
        errors = []
        try:
            row['_utc'] = timestamp(row['InsertDate'], timezone)
            if row['_utc'] > now.tz_convert('UTC'):
                errors.append('future_submission')
        except Exception:
            errors.append('invalid_or_ambiguous_timestamp')
        try:
            val = float(row['AvailabilityID'])
            if not math.isfinite(val) or val not in STATUSES:
                raise ValueError()
            row['AvailabilityID'] = int(val)
        except (ValueError, TypeError):
            errors.append('invalid_availability')
        row['email_id'] = row['email_id'].lower()
        if not re.fullmatch(r'[0-9a-f]{64}', row['email_id']):
            errors.append('invalid_email_hash')
        if errors:
            rejected.append({**original, 'validation_errors': ';'.join(errors)})
            continue
        row['InsertDate_raw'] = row['InsertDate']
        row['InsertDate'] = row['_utc'].tz_convert(timezone).isoformat()
        row['Zipcode_raw'] = row['Zipcode']
        row['Zipcode'], flag = clean_zip(row['Zipcode'])
        if flag:
            add_flag(row, flag)
        row['State'], row['state_basis'] = state_for_zip(row['Zipcode'], zip_lookup)
        if row['state_basis'] != 'zip_reference':
            add_flag(row, 'state_' + row['state_basis'])
        row['Municipality'] = row['NJPR_municipalityName']
        try:
            query = parse_qs(urlsplit(row['uniqueURL']).query, keep_blank_values=True)
            query = {k.lower(): v for k, v in query.items()}
        except ValueError:
            query = {}
            add_flag(row, 'invalid_landing_url')
        for key in TRACKING:
            row[key + '_raw'] = row.get(key, '')
            values = [text(x) for x in query.get(key, []) if text(x)]
            if len(set(values)) > 1:
                add_flag(row, 'duplicate_url_parameter:' + key)
            if values and row.get(key) and values[0] != row[key]:
                add_flag(row, 'field_url_conflict:' + key)
            if not row.get(key) and values:
                row[key] = values[0]
                add_flag(row, 'recovered_from_url:' + key)
        # MarketingToken is ONLY the exported server-side field, never URL token=.
        row['_src'] = norm(row['utm_source'])
        row['_med'] = norm(row['utm_medium'])
        row['_platform'] = PLATFORMS.get(row['_src'])
        row['_paid_utm'] = bool(row['_src'] and row['_med'] in PAID_MEDIA)
        rows.append(row)
    return sorted(rows, key=lambda r: (r['_utc'], r['input_row'])), rejected


def cookie_click_time(url):
    """Only _gcl_aw: accept the supplied linker form or a literal GCL value."""
    url = unquote(url)
    m = re.search(r'_gcl_aw(?:\*|=)([^*&\s]+)', url)
    if not m:
        return None, None
    raw = m.group(1)
    try:
        if raw.startswith('GCL.'):
            decoded = raw
        else:
            raw = raw.rstrip('.').replace('-', '+').replace('_', '/')
            decoded = base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True).decode('utf-8')
        match = re.fullmatch(r'GCL\.(\d+)\.(.+)', decoded)
        if not match:
            raise ValueError()
        return pd.Timestamp(int(match.group(1)), unit='s', tz='UTC'), None
    except Exception:
        return None, 'invalid_google_cookie'


def load_token_map(path):
    mapping = json.loads(Path(path).read_text()) if path else {}
    if not isinstance(mapping, dict):
        raise ValueError('Token map must be a JSON object keyed by EXACT token.')
    result = {}
    for token, entry in mapping.items():
        if not isinstance(entry, dict) or entry.get('channel_group') not in GROUPS or not text(entry.get('channel_detail')):
            raise ValueError(f'Invalid confirmed token mapping: {token}')
        if token.strip().casefold() in result:
            raise ValueError('Duplicate normalized token mapping.')
        result[token.strip().casefold()] = entry
    return result


def classify(rows, token_map=None):
    """Deterministic single-channel assignment with preserved evidence and conflicts.

    Person/ID history uses ALL valid supplied rows, before reporting date filtering.
    History is only as complete as the supplied export; it is not lifetime history.
    """
    token_map = token_map or {}
    first_seen = {}
    for row in rows:
        row['evidence'] = []
        src, med = row['_src'], row['_med']
        token = row['MarketingToken']
        mapped = token_map.get(token.casefold())
        mp_paid = token.upper().startswith('MP-')
        if mp_paid and mapped and mapped['channel_group'] != 'Paid':
            add_flag(row, 'mp_prefix_conflicts_with_exact_mapping')
        signals = []  # tuple: platform, evidence
        if med in PAID_MEDIA:
            signals.append((row['_platform'] or 'Paid (other)', 'paid_medium'))
            if not src:
                add_flag(row, 'paid_medium_missing_source')
            elif not row['_platform']:
                add_flag(row, 'paid_source_unmapped')
        for key, platform in [('gclid', 'Google'), ('msclkid', 'Bing'),
                              ('ndclid', 'Nextdoor'), ('gbraid', 'Google'), ('wbraid', 'Google')]:
            if row[key]:
                signals.append((platform, key))
        if row['gad_source']:
            signals.append(('Google', 'gad_source'))
        if norm(row['tw_source']) == 'google':
            # Custom tracking field; needs independent advertising evidence.
            add_flag(row, 'custom_google_source_unverified')
        if row['fbclid']:
            add_flag(row, 'fbclid_does_not_prove_paid')
        # A repeated ID is not a person. Preserve first person's later submissions;
        # retain others within the original 60-minute policy, visibly flagging overlap.
        for key in ['gclid', 'fbclid', 'msclkid', 'ndclid']:
            click_id = row[key]
            if not click_id:
                continue
            identity = (key, click_id)
            owner, first_time = first_seen.setdefault(identity, (row['email_id'], row['_utc']))
            if owner != row['email_id']:
                gap = (row['_utc'] - first_time).total_seconds()
                add_flag(row, 'shared_click_id:' + key)
                if gap > 3600:
                    signals = [(p, e) for p, e in signals if e != key]
                    # Co-traveling broad Google parameters cannot rescue a copied GCLID.
                    if key == 'gclid':
                        signals = [(p, e) for p, e in signals if e != 'gad_source']
                    add_flag(row, 'late_reused_click_id:' + key)
                else:
                    add_flag(row, 'shared_click_id_within_60m:' + key)
        if mp_paid:
            signals.append(('Paid (other)', 'confirmed_mp_prefix'))
        elif mapped and mapped['channel_group'] == 'Paid':
            signals.append((mapped['channel_detail'], 'confirmed_token'))

        # A server-side marketing token is not a UTM or click ID. A valid recent
        # advertising cookie can therefore support Paid even when a token exists.
        cookie, error = cookie_click_time(row['uniqueURL'])
        if error:
            add_flag(row, error)
        no_tracking = not any(row.get(k) for k in TRACKING)
        age = (row['_utc'] - cookie).total_seconds() if cookie is not None else None
        if age is not None and age < 0:
            add_flag(row, 'future_google_cookie')
        if cookie is not None and no_tracking and 0 <= age < 3600:
            signals.append(('Google', '_gcl_aw'))
            add_flag(row, 'cookie_attribution_inferred')
        elif cookie is not None and age is not None and age >= 3600:
            add_flag(row, 'google_cookie_outside_window')

        def assign(group, detail, tier, evidence):
            row.update(channel_group=group, channel_detail=detail, match_tier=tier,
                       evidence=list(evidence))

        # Latest user rule: every MP- token is Paid. Protect email/AI otherwise.
        email = med == 'email' or src in EMAIL_SOURCES or bool(re.match(r'^EM\d', token, re.I))
        ai = src in AI_SOURCES
        if mp_paid and (email or ai or med in {'social', 'organic', 'organic-social', 'referral'}):
            add_flag(row, 'confirmed_mp_paid_conflicts_with_nonpaid_tracking')
        if not mp_paid and (email or (mapped and mapped['channel_group'] == 'Email')):
            assign('Email', 'Email', 'explicit_email', ['email_source_medium_or_token'])
            if signals:
                add_flag(row, 'email_paid_conflict')
        elif not mp_paid and (ai or (mapped and mapped['channel_group'] == 'AI-Referral')):
            assign('AI-Referral', src or mapped['channel_detail'], 'explicit_ai', ['ai_source_or_token'])
            if signals:
                add_flag(row, 'ai_paid_conflict')
        elif not mp_paid and med in {'social', 'organic', 'organic-social', 'referral'}:
            assign('Organic-Direct', 'Organic social' if 'social' in med else 'Organic / referral',
                   'explicit_nonpaid', ['utm_medium'])
            if signals:
                add_flag(row, 'nonpaid_paid_conflict')
        elif signals:
            platforms = set(p for p, e in signals if p != 'Paid (other)')
            # Paid UTMs establish the platform when available; otherwise no arbitrary winner.
            if row['_paid_utm'] and row['_platform']:
                detail = row['_platform']
            elif len(platforms) == 1:
                detail = next(iter(platforms))
            elif len(platforms) > 1:
                detail = 'Paid (platform conflict)'
            else:
                detail = 'Paid (other)'
            if len(platforms) > 1:
                add_flag(row, 'multiple_paid_platforms')
            tier = 'paid_utm' if med in PAID_MEDIA else ('confirmed_token' if mapped and mapped['channel_group'] == 'Paid' else 'ad_identifier')
            if signals == [('Google', '_gcl_aw')]:
                tier = 'recent_google_cookie'
            if mp_paid:
                tier = 'confirmed_mp_prefix'
            assign('Paid', detail, tier, [e for p, e in signals])
        elif mapped:
            assign(mapped['channel_group'], mapped['channel_detail'], 'confirmed_token', ['MarketingToken'])
        elif token:
            # Do not invent sales-rep, direct-mail, event or agency mappings from shapes.
            assign('Offline-Referral', 'Unconfirmed token', 'unconfirmed_token', ['MarketingToken'])
            add_flag(row, 'token_mapping_unconfirmed')
        else:
            ambiguous = bool(src or med or any(row.get(k) for k in CLICK_IDS) or row['tw_source'])
            assign('Organic-Direct', 'Unattributed / review' if ambiguous else 'Direct / unknown',
                   'unresolved' if ambiguous else 'no_attribution', [])
            if ambiguous:
                add_flag(row, 'insufficient_channel_evidence')
        if row['channel_group'] == 'Paid' and not row['utm_campaign']:
            add_flag(row, 'missing_utm_campaign')
        if row['ReferralData']:
            add_flag(row, 'referral_data_present_not_mapped')
        row['needs_review'] = bool([f for f in row['review_flags'] if not f.startswith(('state_', 'recovered_from_url:'))])
    return rows


def select_period(rows, start, end_exclusive):
    """Date filter FIRST, then dedupe by email. Qualification comes AFTER dedupe."""
    window = [r.copy() for r in rows if start <= r['_utc'] < end_exclusive]
    seen = set()
    selected = []
    for row in window:
        row['selected_for_metric'] = row['email_id'] not in seen
        row['dedupe_reason'] = 'earliest_in_period' if row['selected_for_metric'] else 'later_submission_in_period'
        seen.add(row['email_id'])
        if row['selected_for_metric']:
            selected.append(row)
    return window, selected


def build_payload(selected, window, start, end, timezone, spend_total=None, partial=False):
    """Version 2 is a FIXED reporting window; arrays contain retained people only."""
    pairs = sorted({(r['channel_group'], r['channel_detail']) for r in selected},
                   key=lambda p: (GROUPS.index(p[0]), p[1]))
    zips = sorted({(r['Zipcode'], r['Municipality'], r['State']) for r in selected})
    emails = [r['email_id'] for r in selected]
    toks = sorted({r['MarketingToken'] for r in selected})
    ci, zi, ti = ({v: i for i, v in enumerate(vals)} for vals in (pairs, zips, toks))
    day0 = start.tz_convert(timezone).date()
    days = [(r['_utc'].tz_convert(timezone).date() - day0).days for r in selected]
    paid_available = sum(r['channel_group'] == 'Paid' and r['AvailabilityID'] == 1 for r in selected)
    summary = {'unique_people': len(selected), 'raw_period_rows': len(window),
               'duplicates_removed': len(window) - len(selected),
               'available_now_all_channels': sum(r['AvailabilityID'] == 1 for r in selected),
               'paid_available_now': paid_available,
               'paid_available_needs_review': sum(r['channel_group'] == 'Paid' and r['AvailabilityID'] == 1 and r['needs_review'] for r in selected),
               'channel_counts': {g: sum(r['channel_group'] == g for r in selected) for g in GROUPS},
               'cost_per_paid_available_lead': spend_total / paid_available if spend_total is not None and paid_available else None}
    return {'schema_version': 2, 'pipeline_version': VERSION,
        'reporting_window': {'start': str(day0), 'end_inclusive': str(end.date()),
                             'timezone': timezone, 'includes_partial_day': partial},
        'grain': 'earliest_submission_per_email_within_reporting_window',
        'dashboard_contract': 'Fixed window only. Regenerate for different date windows. Do not reuse old dashboard without adapting schema v2.',
        'day0': str(day0), 'maxday': max(days, default=-1),
        'zips': [{'z': z, 'm': m, 's': s} for z, m, s in zips],
        'chans': [{'g': g, 'd': d} for g, d in pairs], 'emails': emails,
        'toks': [t or None for t in toks], 'd': days,
        'zi': [zi[(r['Zipcode'], r['Municipality'], r['State'])] for r in selected],
        's': [r['AvailabilityID'] for r in selected],
        'a': [int(r['channel_group'] == 'Paid') for r in selected],
        'c': [ci[(r['channel_group'], r['channel_detail'])] for r in selected],
        'e': list(range(len(selected))), 't': [ti[r['MarketingToken']] for r in selected],
        'ts': [int(r['_utc'].timestamp()) for r in selected], 'timestamp_basis': 'UTC Unix seconds',
        'match_tier': [r['match_tier'] for r in selected],
        'review_flags': [r['review_flags'] for r in selected],
        'needs_review': [r['needs_review'] for r in selected],
        'spend_total': spend_total, 'spend_basis': 'User supplied for this exact period and Paid channel scope' if spend_total is not None else 'Not supplied; no cost metric calculated',
        'raw_rows': len(window), 'retained_rows': len(selected), 'summary': summary}


def dump_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def write_rows(path, rows):
    public = [{k: (';'.join(v) if isinstance(v, list) else v)
               for k, v in r.items() if not k.startswith('_')} for r in rows]
    df = pd.DataFrame(public) if public else pd.DataFrame(columns=COLUMNS + ['channel_group', 'channel_detail', 'match_tier'])
    df.to_csv(path, index=False)


def report_bounds(start, end, timezone, now):
    today = now.tz_convert(timezone).normalize()
    start = pd.Timestamp(start).tz_localize(timezone)
    end = pd.Timestamp(end).tz_localize(timezone) if end else today - pd.DateOffset(days=1)
    if start != start.normalize() or end != end.normalize():
        raise ValueError('Use dates only (YYYY-MM-DD) for --start and --end.')
    if end > today or start > end:
        raise ValueError('Invalid reporting range: start must precede end, and end cannot be after today.')
    return start, end, end + pd.DateOffset(days=1), end == today


def run(args, now=None):
    now = now if now is not None else pd.Timestamp.now(tz='UTC')
    start, end, end_exclusive, partial = report_bounds(args.start, args.end, args.timezone, now)
    if args.spend_total is not None and (not math.isfinite(args.spend_total) or args.spend_total < 0):
        raise ValueError('--spend-total must be a finite, nonnegative number.')
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    prefix = out.with_suffix('')
    paths = {k: Path(str(prefix) + '.' + k + ext) for k, ext in
             [('quality', '.json'), ('audit', '.csv'), ('cleaned', '.csv'),
              ('rejected', '.csv'), ('snapshot', '.json')]}
    outputs = [out] + list(paths.values())
    inputs = [Path(p).resolve() for p in [args.input, args.previous_snapshot, args.token_map, args.zip_lookup] if p]
    if any(p.resolve() in inputs for p in outputs):
        raise ValueError('An output path would overwrite an input. Choose another output name.')
    # Prevent a failed rerun leaving a stale payload that looks current.
    if any(p.exists() for p in outputs):
        raise ValueError('Output already exists. Use a new output filename for each run.')
    df, missing = load(args.input)
    rows, rejected = normalize_rows(df, args.timezone, now, load_zip_lookup(args.zip_lookup))
    rows = classify(rows, load_token_map(args.token_map))
    window, selected = select_period(rows, start, end_exclusive)
    daily = {}
    for r in rows:
        day = str(r['_utc'].tz_convert(args.timezone).date())
        daily[day] = daily.get(day, 0) + 1
    extraction_start = pd.Timestamp(args.extraction_start).date()
    today = now.tz_convert(args.timezone).date()
    snapshot = {'schema_version': 1, 'pipeline_version': VERSION,
        'generated_at': now.isoformat(), 'timezone': args.timezone,
        'extraction_start': str(extraction_start), 'complete_before': str(today),
        'source_rows': len(df), 'daily_raw_counts': daily}
    blockers, warnings, completeness_issues = [], [], []
    if rejected:
        blockers.append(f'{len(rejected)} invalid rows quarantined. Correct the export before publishing.')
    if not rows:
        blockers.append('No valid rows in export.')
    if extraction_start > start.date():
        blockers.append('Reporting start precedes declared extraction coverage.')
    if rows and rows[0]['_utc'].tz_convert(args.timezone).date() < extraction_start:
        blockers.append('Rows precede declared extraction start; verify extraction coverage metadata.')
    if len(df) and len(df) % 5000 == 0:
        completeness_issues.append(f'Suspiciously round export count: {len(df)}. Verify export completeness.')
    expected_last = min(end.date(), (now.tz_convert(args.timezone).normalize() - pd.DateOffset(days=1)).date())
    if daily and max(daily) < str(expected_last):
        completeness_issues.append(f'Latest row date {max(daily)} precedes expected complete day {expected_last}. Verify completeness or a genuine zero-entry period.')
    if missing:
        warnings.append('Missing optional fields: ' + ', '.join(missing))
    if partial:
        warnings.append('Report includes the current partial day; do not treat it as a complete day in daily averages.')
    if not args.zip_lookup:
        warnings.append('State is an explicitly flagged service-region prefix estimate or Unknown; supply an authoritative ZIP reference for exact state reporting.')
    if not args.previous_snapshot:
        warnings.append('No previous snapshot supplied: historical row-loss check was not performed.')
    else:
        prev = json.loads(Path(args.previous_snapshot).read_text())
        if prev.get('schema_version') != 1 or prev.get('timezone') != args.timezone:
            raise ValueError('Previous snapshot schema/timezone does not match.')
        if prev.get('publishable') is False:
            raise ValueError('Previous snapshot was blocked; use the last successful, verified baseline.')
        common_start = max(str(extraction_start), prev['extraction_start'])
        common_end = min(str(today), prev['complete_before'])
        for day, previous_count in prev['daily_raw_counts'].items():
            if common_start <= day < common_end and daily.get(day, 0) < previous_count:
                completeness_issues.append(f'Historical row loss on {day}: {previous_count} -> {daily.get(day, 0)}.')
    if text(args.completeness_note):
        warnings.extend(completeness_issues)
    else:
        blockers.extend(completeness_issues)
    if not window:
        warnings.append('No submissions in selected reporting window.')
    warnings.append('Attribution reflects recorded form evidence, not causal lift or complete cross-device/view-through attribution.')
    snapshot['publishable'] = not blockers
    quality = {'pipeline_version': VERSION, 'publishable': not blockers,
        'source_file_sha256': hashlib.sha256(Path(args.input).read_bytes()).hexdigest(),
        'source_rows': len(df), 'valid_rows': len(rows), 'rejected_rows': len(rejected),
        'period_rows': len(window), 'retained_people': len(selected),
        'blockers': blockers, 'warnings': warnings,
        'acknowledged_completeness_issues': completeness_issues if text(args.completeness_note) else [],
        'completeness_verification_note': text(args.completeness_note) or None,
        'review_rows_in_period': sum(r['needs_review'] for r in window),
        'rule_settings': {'timezone': args.timezone, 'cookie_window_seconds': 3600,
                          'confirmed_paid_token_prefixes': ['MP-'],
                          'mp_prefix_overrides_nonpaid_channel_signals': True,
                          'shared_id_window_seconds': 3600, 'unknown_tokens': 'unconfirmed',
                          'paid_mediums': sorted(PAID_MEDIA),
                          'confirmed_token_map': load_token_map(args.token_map)}}
    write_rows(paths['audit'], window)
    write_rows(paths['cleaned'], selected)
    write_rows(paths['rejected'], rejected)
    dump_json(paths['quality'], quality)
    dump_json(paths['snapshot'], snapshot)
    if blockers:
        print('BLOCKED: no dashboard payload written. See ' + str(paths['quality']), file=sys.stderr)
        for issue in blockers:
            print(' - ' + issue, file=sys.stderr)
        return 2
    payload = build_payload(selected, window, start, end, args.timezone, args.spend_total, partial)
    payload['quality_report'] = paths['quality'].name
    payload['generated_at'] = now.isoformat()
    dump_json(out, payload)
    print(json.dumps(payload['summary'], indent=2))
    print(f'Written: {out}\nAudit: {paths["audit"]}\nQuality: {paths["quality"]}')
    return 0


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('input', help='CSV export from the supplied SQL query, with or without headers')
    p.add_argument('output', nargs='?', default='payload.json')
    p.add_argument('--start', default='2026-03-01', help='Reporting start date, inclusive')
    p.add_argument('--end', help='Reporting end date, inclusive; defaults to yesterday')
    p.add_argument('--timezone', default='America/New_York', help='Timezone for naive InsertDate values AND reporting calendar; verify with database owner')
    p.add_argument('--extraction-start', default='2026-03-01', help='Declared SQL extraction start date')
    p.add_argument('--spend-total', type=float, help='Actual spend for the EXACT selected dates and Paid channel scope; otherwise cost metric stays null')
    p.add_argument('--previous-snapshot', help='Previous run snapshot JSON from the same export population')
    p.add_argument('--token-map', default=str(Path(__file__).with_name('confirmed_tokens.json')),
                   help='JSON of confirmed exact MarketingToken mappings; defaults to bundled confirmed_tokens.json')
    p.add_argument('--zip-lookup', help='Authoritative ZIP-to-state CSV with Zipcode,State columns')
    p.add_argument('--completeness-note', help='Only after verifying SQL/export counts: explanation acknowledging round counts, historical losses, or genuine zero-entry days. Cannot override invalid rows.')
    return p


if __name__ == '__main__':
    try:
        sys.exit(run(parser().parse_args()))
    except (ValueError, OSError, KeyError, pd.errors.ParserError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(2)
