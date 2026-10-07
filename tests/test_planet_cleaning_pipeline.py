"""Synthetic regression tests. No Planet customer data is used."""
import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from planet_cleaning import pipeline as p

TZ = 'America/New_York'
NOW = pd.Timestamp('2026-09-11T16:00:00Z')


def record(person='a', date='2026-09-07 12:00:00', **kwargs):
    r = {c: '' for c in p.COLUMNS}
    r.update(InsertDate=date, AvailabilityID='1', email_id=person * 64,
             Zipcode='7860', NJPR_municipalityName='Newton')
    r.update(kwargs)
    return r


def classify(*records, token_map=None):
    df = pd.DataFrame(records)
    df['input_row'] = range(1, len(df) + 1)
    rows, bad = p.normalize_rows(df, TZ, NOW, {})
    if bad:
        raise AssertionError(bad)
    return p.classify(rows, token_map)


def cookie(date, encoded=True):
    raw = 'GCL.' + str(int(pd.Timestamp(date).timestamp())) + '.abc123'
    value = base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=') if encoded else raw
    return 'https://order.planet.net/?_gl=1*example*_gcl_aw*' + value


class AttributionTests(unittest.TestCase):
    def test_google_cpc(self):
        r = classify(record(utm_source=' Google ', utm_medium='CPC', utm_campaign='Summer'))[0]
        self.assertEqual((r['channel_group'], r['channel_detail']), ('Paid', 'Google'))

    def test_social_alias(self):
        r = classify(record(utm_source='FACEBOOK', utm_medium='paid_social'))[0]
        self.assertEqual(r['channel_detail'], 'Meta')

    def test_actual_meta_placement_sources(self):
        for source in ['meta_fb', 'meta_ig', 'meta_an', 'meta_th']:
            r = classify(record(utm_source=source, utm_medium='paid-social'))[0]
            self.assertEqual(r['channel_detail'], 'Meta')

    def test_fbclid_is_not_paid(self):
        r = classify(record(fbclid='organic-click'))[0]
        self.assertEqual(r['channel_group'], 'Organic-Direct')
        self.assertTrue(r['needs_review'])

    def test_source_alone_is_not_paid(self):
        for source in ['bing', 'reddit', 'unbounce', 'leadpages', 'meta', 'adwords']:
            with self.subTest(source=source):
                self.assertNotEqual(classify(record(utm_source=source))[0]['channel_group'], 'Paid')

    def test_email_protected(self):
        r = classify(record(utm_medium='EMAIL', gclid='click'))[0]
        self.assertEqual(r['channel_group'], 'Email')
        self.assertIn('email_paid_conflict', r['review_flags'])

    def test_email_source_alias(self):
        self.assertEqual(classify(record(utm_source='HS_EMAIL', gclid='click'))[0]['channel_group'], 'Email')

    def test_email_token_case(self):
        self.assertEqual(classify(record(MarketingToken='em123', gclid='click'))[0]['channel_group'], 'Email')

    def test_ai_protected_even_with_offline_token(self):
        r = classify(record(utm_source='chatgpt.com', gclid='click', MarketingToken='PC1'))[0]
        self.assertEqual(r['channel_group'], 'AI-Referral')

    def test_organic_social_protected(self):
        r = classify(record(utm_medium='social', fbclid='click', gclid='other'))[0]
        self.assertEqual(r['channel_group'], 'Organic-Direct')
        self.assertIn('nonpaid_paid_conflict', r['review_flags'])

    def test_recent_cookie(self):
        r = classify(record(uniqueURL=cookie('2026-09-07T15:30:00Z')))[0]
        self.assertEqual(r['match_tier'], 'recent_google_cookie')

    def test_recent_cookie_with_server_campaign_token(self):
        r = classify(record(MarketingToken='UnconfirmedCode', uniqueURL=cookie('2026-09-07T15:30:00Z')))[0]
        self.assertEqual(r['channel_group'], 'Paid')
        self.assertEqual(r['match_tier'], 'recent_google_cookie')

    def test_recent_cookie_cannot_override_email_token(self):
        r = classify(record(MarketingToken='EM123', uniqueURL=cookie('2026-09-07T15:30:00Z')))[0]
        self.assertEqual(r['channel_group'], 'Email')

    def test_cookie_age_limits(self):
        for dt in ['2026-09-07T16:01:00Z', '2026-09-07T15:00:00Z', '2026-09-07T14:59:00Z']:
            with self.subTest(dt=dt):
                self.assertNotEqual(classify(record(uniqueURL=cookie(dt)))[0]['channel_group'], 'Paid')

    def test_cookie_cannot_override_any_utm(self):
        for key in p.UTMS:
            with self.subTest(key=key):
                r = classify(record(uniqueURL=cookie('2026-09-07T15:30:00Z'), **{key: 'something'}))[0]
                self.assertNotEqual(r['match_tier'], 'recent_google_cookie')

    def test_cookie_cannot_override_fbclid(self):
        r = classify(record(uniqueURL=cookie('2026-09-07T15:30:00Z'), fbclid='organic'))[0]
        self.assertNotEqual(r['channel_group'], 'Paid')

    def test_gcl_au_ignored(self):
        r = classify(record(uniqueURL=cookie('2026-09-07T15:30:00Z').replace('_gcl_aw', '_gcl_au')))[0]
        self.assertNotEqual(r['channel_group'], 'Paid')

    def test_literal_cookie(self):
        self.assertEqual(classify(record(uniqueURL=cookie('2026-09-07T15:30:00Z', False)))[0]['channel_group'], 'Paid')

    def test_reused_id_same_person_later(self):
        rows = classify(record(gclid='one'), record(date='2026-09-08 12:00:00', gclid='one'))
        self.assertEqual([r['channel_group'] for r in rows], ['Paid', 'Paid'])

    def test_reused_id_different_person_later(self):
        rows = classify(record(gclid='one'), record(person='b', date='2026-09-08 12:00:00', gclid='one', gad_source='1'))
        self.assertNotEqual(rows[1]['channel_group'], 'Paid')

    def test_reused_id_different_person_with_paid_utm(self):
        rows = classify(record(gclid='one'), record(person='b', date='2026-09-08 12:00:00',
                        gclid='one', utm_source='google', utm_medium='cpc'))
        self.assertEqual(rows[1]['channel_group'], 'Paid')

    def test_shared_id_window_boundary(self):
        for time, expected in [('13:00:00', 'Paid'), ('13:00:01', 'Organic-Direct')]:
            rows = classify(record(gclid='one'), record(person='b', date='2026-09-07 ' + time, gclid='one'))
            self.assertEqual(rows[1]['channel_group'], expected)

    def test_shared_braid_not_demoted(self):
        rows = classify(record(gbraid='shared'), record(person='b', date='2026-09-08 12:00:00', gbraid='shared'))
        self.assertEqual(rows[1]['channel_group'], 'Paid')

    def test_platform_conflict_no_arbitrary_winner(self):
        r = classify(record(gclid='google', msclkid='bing'))[0]
        self.assertEqual(r['channel_detail'], 'Paid (platform conflict)')

    def test_paid_utm_platform_beats_other_identifier(self):
        r = classify(record(utm_source='meta', utm_medium='paid-social', gclid='google'))[0]
        self.assertEqual(r['channel_detail'], 'Meta')
        self.assertIn('multiple_paid_platforms', r['review_flags'])

    def test_missing_campaign_is_flag_not_demotion(self):
        r = classify(record(gclid='one'))[0]
        self.assertEqual(r['channel_group'], 'Paid')
        self.assertIn('missing_utm_campaign', r['review_flags'])

    def test_unknown_token_not_guessed(self):
        for token in ['PC1', 'MRitchie', 'NJFAIR2025']:
            self.assertEqual(classify(record(MarketingToken=token))[0]['channel_detail'], 'Unconfirmed token')

    def test_confirmed_token(self):
        mapping = {'pc1': {'channel_group': 'Offline-Referral', 'channel_detail': 'Direct mail'}}
        self.assertEqual(classify(record(MarketingToken='PC1'), token_map=mapping)[0]['channel_detail'], 'Direct mail')

    def test_confirmed_september_paid_token(self):
        mapping = p.load_token_map(Path(p.__file__).with_name('confirmed_tokens.json'))
        r = classify(record(MarketingToken='MP-SEP-26'), token_map=mapping)[0]
        self.assertEqual((r['channel_group'], r['channel_detail']), ('Paid', 'Paid (other)'))
        self.assertEqual(r['match_tier'], 'confirmed_mp_prefix')

    def test_september_token_preserves_known_platform(self):
        mapping = p.load_token_map(Path(p.__file__).with_name('confirmed_tokens.json'))
        r = classify(record(MarketingToken='MP-SEP-26', gclid='google-click'), token_map=mapping)[0]
        self.assertEqual((r['channel_group'], r['channel_detail']), ('Paid', 'Google'))
        self.assertNotIn('multiple_paid_platforms', r['review_flags'])

    def test_all_mp_prefixes_are_paid_without_map(self):
        for token in ['MP-AUG-26', 'MP-SEP-26', 'MP-OCT-26', 'MP-MP-AUG-26', ' mp-future-campaign ']:
            r = classify(record(MarketingToken=token))[0]
            self.assertEqual((r['channel_group'], r['match_tier']), ('Paid', 'confirmed_mp_prefix'))

    def test_mp_prefix_is_not_substring_match(self):
        for token in ['NOT-MP-SEP-26', 'MPSEP26', 'MP']:
            self.assertNotEqual(classify(record(MarketingToken=token))[0]['channel_group'], 'Paid')

    def test_mp_paid_overrides_conflicting_nonpaid_with_flag(self):
        for source, medium in [('newsletter', 'email'), ('chatgpt.com', ''), ('facebook', 'social')]:
            r = classify(record(MarketingToken='MP-FUTURE', utm_source=source, utm_medium=medium))[0]
            self.assertEqual(r['channel_group'], 'Paid')
            self.assertIn('confirmed_mp_paid_conflicts_with_nonpaid_tracking', r['review_flags'])

    def test_server_token_only(self):
        self.assertEqual(classify(record(uniqueURL='https://order.planet.net/?token=EM123'))[0]['channel_group'], 'Organic-Direct')

    def test_url_recovery_and_conflict(self):
        r = classify(record(uniqueURL='https://order.planet.net/?utm_source=google&utm_medium=cpc'))[0]
        self.assertEqual(r['channel_detail'], 'Google')
        r = classify(record(utm_source='meta', utm_medium='paid_social',
                            uniqueURL='https://order.planet.net/?utm_source=google'))[0]
        self.assertEqual(r['channel_detail'], 'Meta')
        self.assertIn('field_url_conflict:utm_source', r['review_flags'])


class DataTests(unittest.TestCase):
    def test_dedupe_after_date_filter_and_before_availability(self):
        rows = classify(record(date='2026-04-01'), record(date='2026-09-07 10:00:00', AvailabilityID='0'),
                        record(date='2026-09-07 11:00:00', gclid='ad'), record(person='b'))
        start, end, exclusive, _ = p.report_bounds('2026-09-01', '2026-09-07', TZ, NOW)
        window, selected = p.select_period(rows, start, exclusive)
        self.assertEqual(len(window), 3)
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]['AvailabilityID'], 0)
        self.assertEqual(selected[0]['InsertDate'][:10], '2026-09-07')

    def test_zip_strict(self):
        for raw in ['7860', '07860', '7860.0', '07860-1234', '078601234']:
            self.assertEqual(p.clean_zip(raw), ('07860', ''))
        for raw in ['abc07860', '07860xyz', '123456', '-7860', '00000']:
            self.assertEqual(p.clean_zip(raw)[1], 'invalid_zip')

    def test_state_safe_fallback(self):
        self.assertEqual(p.state_for_zip('20001', {})[0], 'Unknown')
        self.assertEqual(p.state_for_zip('07860', {})[0], 'NJ')
        self.assertEqual(p.state_for_zip('20001', {'20001': 'DC'}), ('DC', 'zip_reference'))

    def test_dst_offsets_and_ambiguity(self):
        self.assertEqual(p.timestamp('2026-01-01 12:00:00', TZ).hour, 17)
        self.assertEqual(p.timestamp('2026-07-01 12:00:00', TZ).hour, 16)
        self.assertEqual(p.timestamp('2027-07-01 12:00:00', TZ).hour, 16)
        for date in ['2026-11-01 01:30:00', '2026-03-08 02:30:00']:
            with self.assertRaises(Exception):
                p.timestamp(date, TZ)

    def test_invalid_rows_quarantined(self):
        df = pd.DataFrame([record(email_id='g'*64), record(AvailabilityID='7'),
                           record(AvailabilityID='1.5'), record(date='bad')])
        df['input_row'] = range(1, 5)
        rows, bad = p.normalize_rows(df, TZ, NOW, {})
        self.assertEqual(len(rows), 0)
        self.assertEqual(len(bad), 4)

    def test_default_excludes_today(self):
        _, end, exclusive, partial = p.report_bounds('2026-09-01', None, TZ, NOW)
        self.assertEqual(str(end.date()), '2026-09-10')
        self.assertFalse(partial)
        self.assertEqual(str(exclusive.date()), '2026-09-11')

    def test_payload_grain_spend_and_municipality(self):
        rows = classify(record(gclid='a'), record(person='b', NJPR_municipalityName='Other town'))
        start, end, exclusive, partial = p.report_bounds('2026-09-01', '2026-09-07', TZ, NOW)
        window, selected = p.select_period(rows, start, exclusive)
        payload = p.build_payload(selected, window, start, end, TZ)
        self.assertIsNone(payload['spend_total'])
        self.assertIsNone(payload['summary']['cost_per_paid_available_lead'])
        self.assertEqual(len(payload['zips']), 2)
        self.assertEqual(payload['ts'][0], int(pd.Timestamp('2026-09-07T16:00:00Z').timestamp()))
        for field in ['d', 'zi', 's', 'a', 'c', 'e', 't', 'ts', 'match_tier', 'review_flags']:
            self.assertEqual(len(payload[field]), 2)
        payload = p.build_payload(selected, window, start, end, TZ, 100)
        self.assertEqual(payload['summary']['cost_per_paid_available_lead'], 100)


class IntegrationTests(unittest.TestCase):
    def run_case(self, records, extra=None, header=False):
        folder = Path(self.tmp.name)
        src = folder / 'input.csv'
        pd.DataFrame(records, columns=p.COLUMNS).to_csv(src, index=False, header=header)
        out = folder / 'payload.json'
        args = p.parser().parse_args([str(src), str(out), '--start', '2026-09-01', '--end', '2026-09-07'] + (extra or []))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = p.run(args, NOW)
        return code, out

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_headerless_cli_run(self):
        code, out = self.run_case([record(), record(date='2026-04-01')])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text())['retained_rows'], 1)
        self.assertTrue(out.with_suffix('.audit.csv').exists())

    def test_headered_cli_run(self):
        self.assertEqual(self.run_case([record()], header=True)[0], 0)

    def test_invalid_status_blocks_payload(self):
        code, out = self.run_case([record(AvailabilityID='9')])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())
        self.assertTrue(out.with_suffix('.rejected.csv').exists())

    def test_max_date_blocks(self):
        code, out = self.run_case([record(date='2026-09-05')])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_prior_snapshot_detects_loss(self):
        previous = Path(self.tmp.name) / 'prior.json'
        previous.write_text(json.dumps({'schema_version': 1, 'timezone': TZ,
            'extraction_start': '2026-03-01', 'complete_before': '2026-09-10',
            'daily_raw_counts': {'2026-09-07': 2}}))
        code, out = self.run_case([record()], ['--previous-snapshot', str(previous)])
        self.assertEqual(code, 2)
        self.assertIn('Historical row loss', out.with_suffix('.quality.json').read_text())

    def test_existing_output_is_not_overwritten(self):
        self.run_case([record()])
        with self.assertRaisesRegex(ValueError, 'Output already exists'):
            self.run_case([record()])

    def test_verified_zero_entry_days_can_be_acknowledged(self):
        code, out = self.run_case([record(date='2026-09-05')],
            ['--completeness-note', 'Synthetic test: last two days verified zero.'])
        self.assertEqual(code, 0)
        quality = json.loads(out.with_suffix('.quality.json').read_text())
        self.assertTrue(quality['acknowledged_completeness_issues'])

    def test_completeness_note_cannot_bypass_invalid_data(self):
        code, out = self.run_case([record(email_id='invalid')],
            ['--completeness-note', 'Synthetic test acknowledgment.'])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_round_count_blocks(self):
        code, out = self.run_case([record()] * 5000)
        self.assertEqual(code, 2)
        self.assertIn('Suspiciously round', out.with_suffix('.quality.json').read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
