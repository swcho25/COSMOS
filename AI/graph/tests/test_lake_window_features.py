"""Observed windows preserve boundaries, document identity, NULLs and price timing."""
import datetime as dt
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lake_window_features import build_features


class LakeWindowFeatureTests(unittest.TestCase):
    def setUp(self):
        self.companies = [{"ticker": "A", "market": "KOSPI"},
                          {"ticker": "B", "market": "KOSPI"},
                          {"ticker": "C", "market": "NASDAQ"}]
        self.end = "2026-09-15T00:00:00Z"

    def build(self, mentions=(), relations=(), disclosures=(), prices=()):
        return build_features(self.companies, list(mentions), list(relations),
                              list(disclosures), list(prices), self.end)

    def mention(self, document="n1", at="2026-09-14", **fields):
        return {"document_id": document, "ticker": "A", "published_at": at,
                "confidence": 1.0, **fields}

    def relation(self, document="r1", at="2026-09-14", **fields):
        return {"record_id": document, "src_ticker": "A", "dst_ticker": "B", "rel_type": "PARTNER",
                "published_at": at, "confidence": 1.0, "source": "NEWS", **fields}

    def row(self, result, window="7D", table="company_metrics", ticker="A"):
        return next(r for r in result[table] if r["window_type"] == window and r["stock_code"] == ticker)

    def prices(self, ticker="A", values=(100, 110, 99, 118.8, 95.04, 123.552), first="2026-09-07"):
        date = dt.date.fromisoformat(first)
        return [{"ticker": ticker, "date": (date + dt.timedelta(days=i)).isoformat(),
                 "close": value, "adjusted_close": value, "volume": 0} for i, value in enumerate(values)]

    def test_exact_7_30_90_day_boundaries_and_exclusive_end(self):
        end = dt.datetime.fromisoformat(self.end.replace("Z", "+00:00"))
        for days in (7, 30, 90):
            start = end - dt.timedelta(days=days)
            before = (start - dt.timedelta(microseconds=1)).isoformat()
            data = self.build(mentions=[self.mention("at", start.isoformat()), self.mention("before", before),
                                        self.mention("end", self.end), self.mention("future", "2026-09-16")],
                              relations=[self.relation("at", start.isoformat()), self.relation("end", self.end)])
            self.assertEqual(self.row(data, f"{days}D")["news_mention_count"], 1)
            score = next(r for r in data["relationship_scores"] if r["window_type"] == f"{days}D")
            self.assertEqual(score["evidence_count"], 1)

    def test_documents_deduplicate_and_unknown_tickers_are_excluded(self):
        mentions = [self.mention(confidence=.3), self.mention(confidence=.8), self.mention(ticker="UNKNOWN")]
        filings = [{"document_id": "d1", "ticker": "A", "published_at": "2026-09-14"}] * 2
        data = self.build(mentions=mentions, disclosures=filings)
        self.assertEqual(data, self.build(mentions=list(reversed(mentions)), disclosures=filings))
        self.assertEqual(len(data["company_metrics"]), 9)
        self.assertEqual(self.row(data)["news_mention_count"], 1)
        self.assertEqual(self.row(data)["disclosure_count"], 1)

    def test_unanalyzed_sentiment_null_and_analyzed_negative_zero_preserved(self):
        missing = self.row(self.build(mentions=[self.mention()]))
        for name in ("positive_count", "negative_count", "sentiment_score"):
            self.assertIsNone(missing[name])
        self.assertEqual(missing["sentiment_analyzed_count"], 0)
        self.assertEqual(missing["sentiment_coverage"], 0)
        data = self.build(mentions=[self.mention("neg", sentiment_label="NEGATIVE", sentiment_score=-1),
                                    self.mention("zero", sentiment_label="NEUTRAL", sentiment_score=0),
                                    self.mention("unknown")])
        row = self.row(data)
        self.assertEqual((row["positive_count"], row["negative_count"], row["neutral_count"]), (0, 1, 1))
        self.assertEqual(row["sentiment_score"], -.5)
        self.assertEqual(row["sentiment_analyzed_count"], 2)
        self.assertAlmostEqual(row["sentiment_coverage"], 2 / 3)
        self.assertIsNone(self.row(data, ticker="B")["sentiment_coverage"])

    def test_score_only_analysis_does_not_invent_sentiment_labels(self):
        row = self.row(self.build(mentions=[self.mention(sentiment_score=0)]))
        self.assertEqual(row["sentiment_score"], 0)
        self.assertEqual(row["sentiment_analyzed_count"], 1)
        self.assertIsNone(row["positive_count"])

    def test_source_dedup_zero_confidence_and_direction(self):
        relations = [self.relation(confidence=0), self.relation(src_ticker="B", dst_ticker="A", confidence=0),
                     self.relation(source="DISCLOSURE", confidence=1),
                     self.relation("supply1", rel_type="SUPPLY"),
                     self.relation("supply2", rel_type="SUPPLY", src_ticker="B", dst_ticker="A")]
        data = self.build(relations=relations)
        score = next(r for r in data["relationship_scores"] if r["window_type"] == "7D"
                     and r["relationship_type_code"] == "PARTNER")
        self.assertEqual(score["news_score"], 0)
        self.assertEqual(score["evidence_count"], 2)
        self.assertEqual(score["score"], round(score["disclosure_score"] / 2, 6))
        self.assertEqual(self.row(data)["relationship_count"], 3)
        self.assertEqual(data, self.build(relations=list(reversed(relations))))
        news_only = self.build(relations=[self.relation(confidence=0)])["relationship_scores"][0]
        self.assertIsNone(news_only["disclosure_score"])
        self.assertEqual(news_only["score"], 0)

    def test_price_anchor_adjustments_and_future_exclusion(self):
        prices = self.prices(values=(100, 110, 99))
        prices[0]["close"] = 200  # raw split-unadjusted price must not affect returns
        prices += self.prices(values=(999,), first="2026-09-15")
        row = self.row(self.build(prices=prices), table="company_features")
        self.assertEqual(row["price_observation_count"], 2)
        self.assertEqual(row["price_return_observation_count"], 2)
        self.assertTrue(row["price_anchor_available"])
        self.assertAlmostEqual(row["price_return"], -.01)
        self.assertAlmostEqual(row["price_volatility"], math.sqrt(.02))
        self.assertEqual(row["last_price_date"], "2026-09-09")
        no_anchor = self.row(self.build(prices=prices[1:]), table="company_features")
        self.assertFalse(no_anchor["price_anchor_available"])
        self.assertAlmostEqual(no_anchor["price_return"], -.1)

    def test_missing_adjusted_price_has_no_returns_and_alias_works(self):
        prices = self.prices(values=(100, 110))
        prices[0].pop("adjusted_close")
        row = self.row(self.build(prices=prices), table="company_features")
        self.assertIsNone(row["price_return"])
        self.assertIsNone(row["price_volatility"])
        self.assertEqual(row["price_status"], "missing_adjusted_close")
        prices[0]["adj_close"] = 100
        self.assertAlmostEqual(self.row(self.build(prices=prices), table="company_features")["price_return"], .1)

    def test_five_same_market_return_dates_for_correlation_and_no_score_mix(self):
        prices = self.prices() + self.prices("B", values=(200, 220, 198, 237.6, 190.08, 247.104))
        plain = self.build(relations=[self.relation()])
        data = self.build(relations=[self.relation()], prices=prices)
        feature = next(r for r in data["relationship_features"] if r["window_type"] == "7D")
        self.assertEqual(feature["price_aligned_return_count"], 5)
        self.assertAlmostEqual(feature["price_correlation"], 1)
        self.assertEqual(data["relationship_scores"], plain["relationship_scores"])
        short = self.build(relations=[self.relation()], prices=prices[:-1])["relationship_features"][0]
        self.assertEqual(short["price_aligned_return_count"], 4)
        self.assertIsNone(short["price_correlation"])

    def test_cross_market_correlation_is_explicitly_unavailable(self):
        data = self.build(relations=[self.relation(dst_ticker="C")], prices=self.prices() + self.prices("C"))
        for row in data["relationship_features"]:
            self.assertIsNone(row["price_correlation"])
            self.assertEqual(row["price_correlation_status"], "cross_market_not_aligned")

    def test_constant_prices_have_observed_zero_return_volatility_but_no_correlation(self):
        prices = self.prices(values=(100,) * 6) + self.prices("B", values=(200,) * 6)
        data = self.build(relations=[self.relation()], prices=prices)
        row = self.row(data, table="company_features")
        self.assertEqual(row["price_return"], 0)
        self.assertEqual(row["price_volatility"], 0)
        self.assertEqual(data["relationship_features"][0]["price_correlation_status"], "constant_returns")

    def test_conflicting_duplicates_and_invalid_values_fail(self):
        with self.assertRaises(ValueError):
            self.build(mentions=[self.mention(), self.mention(at="2026-09-13")])
        with self.assertRaises(ValueError):
            self.build(mentions=[self.mention(sentiment_score=0), self.mention(sentiment_score=-1)])
        with self.assertRaises(ValueError):
            self.build(prices=self.prices(values=(100,)) + self.prices(values=(200,)))
        for confidence in (-.1, 1.1, float("nan")):
            with self.assertRaises(ValueError):
                self.build(mentions=[self.mention(confidence=confidence)])
        for value in (0, -1, float("inf")):
            with self.assertRaises(ValueError):
                self.build(prices=self.prices(values=(value,)))


if __name__ == "__main__":
    unittest.main()
