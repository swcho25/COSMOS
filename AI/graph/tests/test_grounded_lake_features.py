"""Fail closed on wrong availability, fabricated sentiment and changed score identities."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grounded_lake_features import build_grounded_features, load_verified_price_snapshot, normalize_canonical_mentions


class GroundedLakeFeatureTests(unittest.TestCase):
    def setUp(self):
        self.companies = [{"ticker": "A", "market": "KOSPI", "name_official": "Alpha"},
                          {"ticker": "B", "market": "KOSPI"},
                          {"ticker": "C", "market": "NASDAQ"}]
        self.end = "2026-09-16T00:00:00Z"

    def mention(self, document="n1", **fields):
        return {"document_id": document, "ticker": "A", "available_at": "2026-09-15T12:00:00Z",
                "sentiment_status": "analyzed", "sentiment_label": "POSITIVE", "sentiment_score": .6,
                "sentiment_probabilities": {"positive": .7, "negative": .1, "neutral": .2}, **fields}

    def relation(self, record="r1", **fields):
        return {"record_id": record, "evidence_id": "e:" + record, "document_id": "canonical:" + record,
                "source_kind": "NEWS", "src_ticker": "A", "dst_ticker": "B", "rel_type": "SUPPLY",
                "available_at": "2026-09-15T12:00:00Z", "support_weight": 1.0, **fields}

    def build(self, mentions=(), relations=(), disclosures=(), prices=(), **options):
        return build_grounded_features(self.companies, mentions, relations, disclosures, prices,
                                       self.end, run_id="test-grounded", provenance={"source_sha256": "abc"},
                                       **options)

    def metric(self, result, ticker="A", table="company_metrics", window="7D"):
        return next(row for row in result["datasets"][table]
                    if row["stock_code"] == ticker and row["window_type"] == window)

    def test_unknown_sentiment_and_missing_date_never_become_neutral_or_today(self):
        pending = self.mention("pending", sentiment_status="ambiguous_target_context",
                               sentiment_label=None, sentiment_score=None, sentiment_probabilities=None)
        unknown_date = self.mention("undated", available_at=None)
        result = self.build([pending, unknown_date])
        metric = self.metric(result)
        self.assertEqual(metric["news_mention_count"], 1)
        self.assertEqual(metric["sentiment_analyzed_count"], 0)
        self.assertEqual(metric["sentiment_coverage"], 0)
        self.assertIsNone(metric["neutral_count"])
        self.assertIsNone(metric["sentiment_score"])
        self.assertEqual(result["coverage"]["news_unknown_availability_rows"], 1)

    def test_target_specific_sentiment_is_not_copied_to_other_company(self):
        negative = self.mention(ticker="B", sentiment_label="NEGATIVE", sentiment_score=-.6,
                                sentiment_probabilities={"positive": .1, "negative": .7, "neutral": .2})
        result = self.build([self.mention(), negative])
        self.assertEqual(self.metric(result)["positive_count"], 1)
        self.assertEqual(self.metric(result, ticker="B")["negative_count"], 1)
        self.assertIsNone(self.metric(result, ticker="C")["sentiment_score"])
        self.assertFalse(self.metric(result)["is_stock_price_prediction"])

    def test_duplicate_document_company_conflict_fails_instead_of_arbitrary_choice(self):
        self.assertEqual(self.metric(self.build([self.mention(), self.mention()]))["news_mention_count"], 1)
        other = self.mention(sentiment_score=.5, sentiment_probabilities=None)
        with self.assertRaisesRegex(ValueError, "Conflicting canonical"):
            self.build([self.mention(), other])

    def test_availability_not_raw_publication_controls_exclusive_boundary(self):
        delayed = self.relation(published_at="2026-09-15", available_at="2026-09-16T12:00:00Z")
        current = self.relation("current", available_at="2026-09-09T00:00:00Z")
        result = self.build(relations=[delayed, current])
        scores = result["datasets"]["relationship_scores"]
        self.assertEqual(len(scores), 3)
        self.assertTrue(all(row["evidence_count"] == 1 for row in scores))
        self.assertEqual(json.loads(result["datasets"]["graph_relationships"][0]["evidence_ids_json"]), ["e:current"])
        for bad in ("2026-09-15", "2026-09-15T12:00:00"):
            with self.assertRaises(ValueError):
                self.build(relations=[self.relation(available_at=bad)])

    def test_historical_assertions_are_separate_from_window_edges(self):
        old = self.relation(available_at="2022-01-01T00:00:00Z")
        result = self.build(relations=[old])
        self.assertEqual(result["datasets"]["graph_edges"], [])
        assertions = result["datasets"]["graph_relationships"]
        self.assertEqual(len(assertions), 1)
        self.assertEqual(assertions[0]["current_contract_state"], "not_established")
        self.assertNotIn("score", assertions[0])
        self.assertEqual(len(result["datasets"]["graph_nodes"]), 3)

    def test_null_source_components_direction_and_frozen_score_identity(self):
        rows = [self.relation(), self.relation(evidence_id="second-quote-same-document"),
                self.relation("reverse", src_ticker="B", dst_ticker="A")]
        result = self.build(relations=rows)
        scores = result["datasets"]["relationship_scores"]
        self.assertEqual(len(scores), 6)
        self.assertTrue(all(row["disclosure_score"] is None for row in scores))
        self.assertTrue(all(row["evidence_count"] == 1 for row in scores))
        self.build(relations=rows, expected_scores=scores)
        changed = copy.deepcopy(scores)
        changed[0]["news_score"] = 99
        with self.assertRaisesRegex(ValueError, "Frozen integrated score"):
            self.build(relations=rows, expected_scores=changed)

    def test_bad_sentiment_and_unknown_company_fail(self):
        for row in [self.mention(sentiment_status="ambiguous_target_context"),
                    self.mention(sentiment_label="NEGATIVE"),
                    self.mention(sentiment_score=.2), self.mention(ticker="UNKNOWN")]:
            with self.assertRaises(ValueError):
                self.build([row])

    def test_price_staleness_and_missing_adjusted_basis_are_explicit(self):
        prices = [{"ticker": "A", "date": "2026-09-10", "close": 100, "adjusted_close": None},
                  {"ticker": "A", "date": "2026-09-11", "close": 110, "adjusted_close": 110}]
        result = self.build(prices=prices)
        feature = self.metric(result, table="company_features")
        self.assertIsNone(feature["price_return"])
        self.assertEqual(feature["price_calendar_days_since_last"], 5)
        self.assertEqual(feature["price_status"], "missing_adjusted_close")
        self.assertIsNone(self.metric(result, ticker="B", table="company_features")["last_close"])

    def test_disclosure_counts_deduplicate_report_and_unknown_date_has_coverage(self):
        row = {"document_id": "d1", "ticker": "A", "available_at": "2026-09-15T12:00:00Z"}
        result = self.build(disclosures=[row, row, {**row, "document_id": "d2", "available_at": None}])
        self.assertEqual(self.metric(result)["disclosure_count"], 1)
        self.assertEqual(result["coverage"]["disclosure_unknown_availability_rows"], 1)

    def test_canonical_duplicates_identical_results_keep_single_analysis(self):
        normalized, report = normalize_canonical_mentions([self.mention(), self.mention()])
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["sentiment_label"], "POSITIVE")
        self.assertEqual(normalized[0]["source_row_count"], 2)
        self.assertEqual(report["duplicate_source_rows"], 1)
        self.assertEqual(report["canonical_revision_conflict_groups"], 0)

    def test_canonical_conflicting_prediction_or_probability_becomes_null(self):
        changed = self.mention(sentiment_score=.5,
                                sentiment_probabilities={"positive": .65, "negative": .15, "neutral": .2})
        normalized, report = normalize_canonical_mentions([self.mention(), changed])
        self.assertEqual(normalized[0]["sentiment_status"], "canonical_revision_conflict")
        self.assertIsNone(normalized[0]["sentiment_label"])
        self.assertIsNone(normalized[0]["sentiment_score"])
        self.assertIsNone(normalized[0]["sentiment_probabilities"])
        self.assertEqual(report["sentiment_conflict_groups"], 1)
        self.assertEqual(self.metric(self.build(normalized))["sentiment_analyzed_count"], 0)

    def test_canonical_newer_revision_prevents_backdating_same_sentiment(self):
        normalized, report = normalize_canonical_mentions([
            self.mention(available_at="2026-09-14T00:00:00Z"),
            self.mention(available_at="2026-09-16T00:00:00Z")])
        self.assertEqual(normalized[0]["available_at"], self.end)
        self.assertIsNone(normalized[0]["sentiment_label"])
        self.assertEqual(report["availability_conflict_groups"], 1)
        self.assertEqual(self.metric(self.build(normalized))["news_mention_count"], 0)

    def test_canonical_unknown_revision_time_stays_unknown_and_order_independent(self):
        rows = [self.mention(), self.mention(available_at=None)]
        first = normalize_canonical_mentions(rows)
        self.assertEqual(first, normalize_canonical_mentions(list(reversed(rows))))
        self.assertIsNone(first[0][0]["available_at"])
        self.assertEqual(first[1]["unknown_availability_groups"], 1)


class VerifiedPriceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[1] / "artifacts"
        self.folder = self.base / ("grounded_price_test_" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.companies = [{"ticker": "A", "market": "KOSPI"}]

    def tearDown(self):
        resolved = self.folder.resolve()
        self.assertTrue(resolved.is_relative_to(self.base.resolve()))
        shutil.rmtree(resolved)

    def snapshot(self, rows):
        import pandas as pd
        path = self.folder / "prices.parquet"
        records = [{"ticker": "A", "market": "KOSPI", "interval_type": "1d", "trading_volume": 1,
                    **row} for row in rows]
        pd.DataFrame(records).to_parquet(path)
        manifest = self.folder / "manifest.json"
        manifest.write_text(json.dumps({"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                       "bytes": path.stat().st_size, "counts": {"rows": len(rows)},
                                       "snapshot_id": "test-price"}), encoding="utf8")
        return path, manifest

    def test_one_anchor_future_exclusion_zero_placeholders_and_basis(self):
        path, manifest = self.snapshot([
            {"trading_at": "2026-01-01", "close_price": 50, "adj_close": 50},
            {"trading_at": "2026-06-17", "close_price": 100, "adj_close": 100},
            {"trading_at": "2026-09-10", "close_price": 0, "adj_close": 0},
            {"trading_at": "2026-09-11", "close_price": 110, "adj_close": 110},
            {"trading_at": "2026-09-16", "close_price": 999, "adj_close": 999}])
        rows, provenance = load_verified_price_snapshot(path, manifest, self.companies, "2026-09-16T00:00:00Z")
        self.assertEqual([row["date"] for row in rows], ["2026-06-17", "2026-09-11"])
        self.assertEqual(provenance["nonpositive_placeholder_rows_excluded"], {"A": 1})
        self.assertFalse(provenance["is_point_in_time_backtest_source"])
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_verified_price_snapshot(path, manifest, self.companies, "2026-09-16T00:00:00Z")

    def test_conflicting_daily_duplicates_fail(self):
        path, manifest = self.snapshot([
            {"trading_at": "2026-09-11", "close_price": 110, "adj_close": 110},
            {"trading_at": "2026-09-11", "close_price": 120, "adj_close": 120}])
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
            load_verified_price_snapshot(path, manifest, self.companies, "2026-09-16T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
