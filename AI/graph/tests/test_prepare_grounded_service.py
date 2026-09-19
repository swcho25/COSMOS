import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare_grounded_service import (canonicalize_mentions, mention_observation,
                                     news_audit, validate_quote, validate_receipts)


class ServiceProjectionTest(unittest.TestCase):
    def observation(self):
        return {'document_id': 'd', 'ticker': 'AAA', 'available_at': '2026-09-01T00:00:00Z',
                'sentiment_status': 'analyzed', 'sentiment_label': 'POSITIVE',
                'sentiment_score': .6,
                'sentiment_probabilities': {'positive': .7, 'negative': .1, 'neutral': .2}}

    def identities(self):
        return {('d', 'AAA'): [{'market': 'NASDAQ', 'edge_id': 'e1', 'source_revision_id': 'r1'},
                              {'market': 'NASDAQ', 'edge_id': 'e2', 'source_revision_id': 'r2'}]}

    def test_equal_scores_different_probabilities_remain_pending(self):
        first = self.observation()
        second = copy.deepcopy(first)
        second['sentiment_probabilities'] = {'positive': .75, 'negative': .15, 'neutral': .1}
        result, audit = canonicalize_mentions([first, second], self.identities())
        self.assertIsNone(result[0]['sentiment_label'])
        self.assertIsNone(result[0]['sentiment_probabilities'])
        self.assertEqual(audit['canonical_revision_conflict_groups'], 1)
        self.assertIsNone(result[0]['impact_score'])

    def test_different_availability_cannot_be_confident_label(self):
        a, b = self.observation(), self.observation()
        b['available_at'] = '2026-09-02T00:00:00Z'
        result, _ = canonicalize_mentions([a, b], self.identities())
        self.assertIsNone(result[0]['sentiment_label'])
        self.assertEqual(result[0]['available_at'], b['available_at'])

    def test_equal_revisions_preserve_label_and_all_references(self):
        result, _ = canonicalize_mentions([self.observation(), self.observation()], self.identities())
        self.assertEqual(result[0]['sentiment_label'], 'POSITIVE')
        self.assertEqual(result[0]['source_revision_ids'], ['r1', 'r2'])
        self.assertFalse(result[0]['is_stock_price_prediction'])

    def test_source_lake_sentiment_disagreement_rejected(self):
        row = {'source_edge_json': json.dumps({'edge_id': 'e', 'ticker': 'AAA',
                                             'sentiment': {'status': 'analyzed', 'label': 'NEGATIVE', 'score': -.5}}),
               'sentiment_status': 'analyzed', 'sentiment_label': 'POSITIVE', 'sentiment_score': .5}
        with self.assertRaisesRegex(ValueError, 'Source sentiment'):
            mention_observation(row)

    def test_bad_url_and_unknown_times_reported_without_modification(self):
        row = {'document_id': 'd', 'source_url': 'file:///private/news', 'published_at': None,
               'published_date': '2026-09-01'}
        before = copy.deepcopy(row)
        issues = news_audit(row)
        self.assertIn('invalid_original_url', issues)
        self.assertIn('publication_utc_unknown', issues)
        self.assertIn('collection_time_unknown', issues)
        self.assertEqual(row, before)

    def test_long_url_preserved_and_reported(self):
        row = {'source_url': 'https://example.com/' + 'a' * 2100}
        self.assertIn('original_url_over_2048_chars', news_audit(row))
        self.assertEqual(len(row['source_url']), 2120)

    def test_citation_uses_original_revision_and_exact_offsets(self):
        text = '삼성전자\n공급 계약을 체결했다.'
        doc = {'source_revision_id': 'r', 'text': text,
               'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        ev = {'source_revision_id': 'r', 'quote_start': 5, 'quote_end': len(text), 'quote': text[5:]}
        validate_quote(ev, doc)
        with self.assertRaisesRegex(ValueError, 'revision'):
            validate_quote({**ev, 'source_revision_id': 'canonical'}, doc)
        with self.assertRaisesRegex(ValueError, 'quote'):
            validate_quote({**ev, 'quote': '다른 기업'}, doc)

    def test_receipt_digest_must_match(self):
        p = {'run_id': 'r', 'datasets': [{'name': 'd', 'row_count': 1}]}
        audit = {'status': 'PASS', 'run_id': 'r', 'publication_sha256': 'different', 'dataset_count': 1}
        receipt = {'status': 'HDFS_VERIFIED_ONLY'}
        with self.assertRaisesRegex(ValueError, 'SHA'):
            validate_receipts(p, 'expected', audit, receipt)


if __name__ == '__main__':
    unittest.main()
