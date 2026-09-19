from pathlib import Path
import sys
import io
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_grounded_service_payload import (filing_identity, existing_filing_identity,
                                           relation_identity, stable, validate_typed_row, Writer)


class ServicePayloadTests(unittest.TestCase):
    def test_undirected_relation_normalizes_uuid_order_but_directed_does_not(self):
        a, b = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))
        self.assertEqual(relation_identity(b, a, 'PARTNER'), (a, b, 'PARTNER'))
        self.assertEqual(relation_identity(b, a, 'COMPETE'), (a, b, 'COMPETE'))
        self.assertEqual(relation_identity(b, a, 'SUPPLY'), (b, a, 'SUPPLY'))

    def test_same_sec_filing_shared_across_share_classes(self):
        row = {'source': 'SEC', 'source_url': 'https://www.sec.gov/Archives/edgar/data/1652044/abc/a.txt',
               'report_id': '0001652044-26-000066', 'ticker': 'GOOG'}
        expected = ('SEC', '0001652044', '0001652044-26-000066')
        self.assertEqual(filing_identity(row), expected)
        self.assertEqual(filing_identity({**row, 'ticker': 'GOOGL'}), expected)
        self.assertEqual(existing_filing_identity({'filing_system': 'SEC', 'sec_cik': '1652044',
                                                 'sec_accession_no': row['report_id']}), expected)

    def test_missing_sec_cik_is_rejected_instead_of_guessed(self):
        with self.assertRaisesRegex(ValueError, 'CIK'):
            filing_identity({'source': 'SEC', 'source_url': 'https://example.com/filing',
                             'report_id': '0001652044-26-000066'})

    def test_dart_identity_validates_receipt(self):
        self.assertEqual(filing_identity({'source': 'DART', 'report_id': '20260919001234'}),
                         ('DART', '20260919001234'))
        with self.assertRaisesRegex(ValueError, 'receipt'):
            filing_identity({'source': 'DART', 'report_id': 'bad'})

    def test_sql_text_nul_and_overflow_rejected(self):
        columns = [{'column_name': 'title', 'data_type': 'character varying', 'character_maximum_length': 4}]
        for value in ('abcde', 'a\x00b'):
            with self.assertRaises(ValueError):
                validate_typed_row('sample', {'title': value}, columns)
        validate_typed_row('sample', {'title': None}, columns)

    def test_timestamp_requires_offset_even_if_sql_column_nullable(self):
        columns = [{'column_name': 'published_at', 'data_type': 'timestamp with time zone'}]
        validate_typed_row('sample', {'published_at': None}, columns)
        with self.assertRaisesRegex(ValueError, 'timezone'):
            validate_typed_row('sample', {'published_at': '2026-09-19T12:00:00'}, columns)

    def test_deterministic_ids_different_citation_revisions_stay_distinct(self):
        self.assertEqual(stable('evidence', 'e', 'r1'), stable('evidence', 'e', 'r1'))
        self.assertNotEqual(stable('evidence', 'e', 'r1'), stable('evidence', 'e', 'r2'))

    def test_context_duplicate_key_rejected(self):
        writer = Writer(Path('.'), {'columns': []})
        writer.streams['document_context'] = io.StringIO()
        row = {'document_id': str(uuid.UUID(int=1)), 'source_revision_id': 'r',
               'sort_at': '2026-09-19T00:00:00Z', 'published_at_raw': None,
               'publication_precision': 'date', 'payload': {}}
        writer.write('document_context', row)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            writer.write('document_context', row)


if __name__ == '__main__':
    unittest.main()
