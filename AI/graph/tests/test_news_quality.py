"""Protect timestamp provenance and literal company-event evidence boundaries."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from news_quality import audit_timestamp,parse_explicit_time,financial_evidence,text_digest


class QualityTests(unittest.TestCase):
    def row(self,**kwargs):
        return dict(published_at_raw='2021-05-04 13:00:00',published_at='2021-05-04T13:00:00',published_date='2021-05-04',date_normalization_method='source_parser',**kwargs)
    def test_naive_country_not_used_as_timezone(self):
        self.assertIsNone(audit_timestamp(self.row(region='domestic'))['publication_time_candidate'])
        r=audit_timestamp(self.row(published_timezone='Asia/Seoul'))
        self.assertEqual(r['publication_time_candidate'],'2021-05-04T04:00:00+00:00')
    def test_date_only_is_not_upgraded(self):
        r=self.row(published_timezone='UTC');r['published_at_raw']='2021-05-04';r['published_at']='2021-05-04T00:00:00Z'
        r['collected_at']='2021-05-04T14:00:00Z'
        self.assertEqual(audit_timestamp(r)['timestamp_status'],'date_only')
        self.assertEqual(audit_timestamp(r)['collected_time_candidate'],'2021-05-04T14:00:00+00:00')
    def test_invalid_conflicting_and_future_collection(self):
        r=self.row(published_timezone='UTC');r['published_date']='2021-05-05'
        self.assertEqual(audit_timestamp(r)['timestamp_status'],'date_time_conflict')
        r=self.row(published_timezone='UTC',collected_at='2021-05-04T12:00:00Z')
        self.assertEqual(audit_timestamp(r)['timestamp_status'],'collection_before_publication')
        r['collected_at']='2026-09-08T12:00:00Z'
        self.assertIn('backfill_or_delayed_collection',audit_timestamp(r)['time_flags'])
    def test_ambiguous_dst_not_guessed(self):
        self.assertIsNone(parse_explicit_time('2021-11-07T01:30:00','America/New_York'))
        self.assertIsNone(parse_explicit_time('2021-03-14T02:30:00','America/New_York'))
        self.assertIsNotNone(parse_explicit_time('2021-11-07T01:30:00-04:00'))
    def test_event_is_same_sentence_and_literal(self):
        title='Apple reported quarterly revenue growth.';body='Other firms discussed mergers.'
        e=financial_evidence(title,body,[dict(ticker='AAPL',start=0,end=5)])
        self.assertTrue(e);self.assertEqual({r['event_type'] for r in e},{'earnings'})
        for r in e:self.assertEqual(r['quote'],(title+'\n'+body)[r['start']:r['end']])
        self.assertFalse(financial_evidence('Apple makes phones.','Other firms reported earnings.',[dict(ticker='AAPL',start=0,end=5)]))
    def test_whitespace_duplicates(self):self.assertEqual(text_digest('A  B\nC'),text_digest('A B C'))


if __name__=='__main__':unittest.main()
