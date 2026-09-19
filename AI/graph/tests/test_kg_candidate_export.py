"""CPU coverage for all-candidate export semantics; not a Spark integration test."""
import copy
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from kg_candidate_export import (normalize_source, candidate, candidate_map, source_map, digest,
                                 canonical, assert_counts, inventory_fingerprint, validate_output_path)
from knowledge_graph_news_preview import canonical_input_sha256


class CandidateExportTests(unittest.TestCase):
    def row(self, **values):
        return dict(record_id="raw:01", title="회사 실적 발표", body="회사는 영업이익 100억원을 발표했다.",
                    published_at_raw=" 2001-03-01 12:00 ", published_at="2001-03-01T03:00:00Z",
                    published_date="2001-03-01", url="https://example.test/?b=2&a=1#x", year="2001",
                    region="domestic", source_path="/f/part.parquet", **values)

    def mention(self, **values):
        row = dict(record_id="raw:01", ticker="DYNAMIC-NEW", name_official="회사", confidence=.5, is_sports=True)
        row.update(values)
        return row

    def test_raw_publication_and_url_are_not_normalized(self):
        article, provenance = normalize_source(self.row())
        self.assertEqual(article["published_at"], " 2001-03-01 12:00 ")
        self.assertEqual(article["source_url"], "https://example.test/?b=2&a=1#x")
        self.assertEqual(provenance["publication_field"], "published_at_raw")
        self.assertEqual(provenance["published_at"], "2001-03-01T03:00:00Z")

    def test_date_only_or_missing_date_is_not_dropped(self):
        row = self.row()
        row.update(published_at_raw="2026-09-17", published_at=None)
        self.assertEqual(normalize_source(row)[0]["published_at"], "2026-09-17")
        row.update(published_at_raw=None, published_at=None, published_date=None)
        article, _ = normalize_source(row)
        item, _ = candidate(article, self.mention())
        self.assertIsNone(item["published_at"])

    def test_all_years_all_candidate_companies_confidence_and_sport_are_preserved(self):
        for year in ("1995", "2020", "2026", "2030"):
            row = self.row()
            row["year"] = year
            article, _ = normalize_source(row)
            item, _ = candidate(article, self.mention())
            self.assertEqual(item["ticker"], "DYNAMIC-NEW")
        self.assertEqual(set(item), {"article_id", "ticker", "company_name", "published_at", "title", "body", "source_url"})

    def test_sanitizer_removes_widget_but_retains_economic_numbers(self):
        row = self.row()
        row["body"] = "회사(251,250원 ▼9,750 -3.74%)는 매출 100억원을 발표했다."
        article, provenance = normalize_source(row)
        self.assertNotIn("251,250", article["body"])
        self.assertIn("100억원", article["body"])
        self.assertTrue(provenance["text_changed"])
        self.assertEqual(provenance["raw_body_sha256"], digest(row["body"]))

    def test_full_long_body_is_not_arbitrarily_truncated(self):
        row = self.row()
        row["body"] = "회사의 사업에 관한 문장. " * 10000 + "본문 마지막 원문"
        article, _ = normalize_source(row)
        self.assertEqual(article["body"], row["body"])

    def test_canonical_hash_matches_classifier_contract_and_each_company(self):
        article, _ = normalize_source(self.row())
        a, pa = candidate(article, self.mention())
        b, pb = candidate(article, self.mention(ticker="OTHER", name_official="다른 기업"))
        self.assertEqual(pa["input_sha256"], canonical_input_sha256(a))
        self.assertNotEqual(pa["input_sha256"], pb["input_sha256"])
        self.assertEqual(pa["canonical_article_sha256"], pb["canonical_article_sha256"])

    def test_pair_mismatch_or_unresolved_name_fails(self):
        article, _ = normalize_source(self.row())
        for mention in (self.mention(record_id="other"), self.mention(name_official=""), self.mention(ticker="")):
            with self.assertRaises(ValueError):
                candidate(article, mention)

    def test_null_body_is_explicit_and_empty_article_cannot_be_candidate(self):
        row = self.row()
        row["body"] = None
        article, provenance = normalize_source(row)
        self.assertEqual(article["body"], "")
        self.assertTrue(provenance["source_body_was_null"])
        candidate(article, self.mention())
        row["title"] = None
        article, _ = normalize_source(row)
        with self.assertRaises(ValueError):
            candidate(article, self.mention())

    def test_stream_maps_preserve_row_sha_and_provenance_separation(self):
        values = list(source_map(iter([self.row()])))
        identity, article_sha, article_json, provenance_json, empty = values[0]
        self.assertFalse(empty)
        mapped = list(candidate_map(iter([dict(article_json=article_json, **self.mention())])))[0]
        self.assertEqual(json.loads(mapped[4])["canonical_article_sha256"], article_sha)
        self.assertEqual(mapped[2], canonical_input_sha256(json.loads(mapped[3])))
        self.assertNotIn("body", json.loads(provenance_json))

    def test_inventory_detects_changes_and_is_order_independent(self):
        inventory = [dict(path="/a", bytes=10, modified_ms=2, checksum="crc:1"),
                     dict(path="/b", bytes=20, modified_ms=4, checksum="crc:2")]
        self.assertEqual(inventory_fingerprint(inventory), inventory_fingerprint(inventory[::-1]))
        other = copy.deepcopy(inventory)
        other[0]["checksum"] = "changed"
        self.assertNotEqual(inventory_fingerprint(inventory), inventory_fingerprint(other))
        with self.assertRaises(ValueError):
            inventory_fingerprint(inventory + inventory[:1])

    def test_output_cannot_escape_or_target_production(self):
        expected = "/data-lake/sandbox/news/junwoo/kg-reselection/trial1"
        self.assertEqual(validate_output_path("hdfs://localhost:9000" + expected), expected)
        for value in ("/data-lake/analyzed/news", expected + "/..", expected + "//x", "hdfs://elsewhere:9000" + expected):
            with self.assertRaises(ValueError):
                validate_output_path(value)

    def test_full_export_denominator_never_hides_bad_join(self):
        counts = dict(mention_unique_pairs=2, exported_unique_pairs=2)
        assert_counts(counts)
        for key in ("missing_source_article_ids", "ambiguous_source_article_ids", "ambiguous_pair_names",
                    "unresolved_company_name_rows", "invalid_mention_rows", "empty_cleaned_articles"):
            with self.assertRaises(ValueError):
                assert_counts(dict(counts, **{key: 1}))
        with self.assertRaises(ValueError):
            assert_counts(dict(counts, exported_unique_pairs=1))


if __name__ == "__main__":
    unittest.main()
