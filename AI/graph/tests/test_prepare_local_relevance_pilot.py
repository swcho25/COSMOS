"""The pilot must isolate references/outcomes and preserve train-only identity."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_local_relevance_pilot as pilot


class LocalRelevancePilotTests(unittest.TestCase):
    def case(self):
        raw = {
            "record_id": "real-source-id", "title": "삼성전자 실적 발표", "body": "삼성전자가 오늘 실적을 발표했다.",
            "published_date": "2021-01-28", "published_at_raw": "2021-01-28T09:00:00+09:00",
            "published_at": "2021-01-28T09:00:00+09:00", "published_timezone": "KST",
            "url": "https://example.org/news/1", "source_dataset": "fixture",
            # A source may contain unrelated metadata; none can enter prompts.
            "metadata_json": {"correct_label": "up", "future_return": 12.0},
            "y_1d": -0.123, "split": "train", "expected_label": "do-not-copy",
        }
        packet = {"record_id": raw["record_id"], "review_id": 48, "published_date": raw["published_date"],
                  "title": raw["title"], "original_body": raw["body"], "clean_title": raw["title"],
                  "clean_body": raw["body"], "region": "domestic",
                  "source_companies": [{"ticker": "005930", "name_official": "삼성전자"}]}
        review = {"verdict": "supported", "supported_tickers": ["005930"], "historical_background": "none",
                  "reason": "Prior AI reason must not enter the prompt.", "quote": raw["body"]}
        return raw, packet, review

    def test_prompt_whitelist_does_not_leak_outcomes_or_prior_review(self):
        raw, packet, review = self.case()
        case = pilot.make_case(raw, packet, review, "005930")
        self.assertEqual(set(case["input"]), set(pilot.INPUT_FIELDS))
        self.assertIsNone(case["input"]["company_context"])
        self.assertIsNone(case["evaluation"]["expected_label"])
        self.assertEqual(case["evaluation"]["expected_selection"], "채택")
        self.assertNotIn(review["reason"], str(case["input"]))
        self.assertNotIn("future_return", str(case["input"]))
        self.assertNotIn("y_1d", case["input"])

    def test_rejects_future_period_and_changed_source_text(self):
        raw, packet, review = self.case()
        with self.assertRaisesRegex(ValueError, "training period"):
            pilot.make_case({**raw, "published_date": "2023-01-01"}, packet, review, "005930")
        with self.assertRaisesRegex(ValueError, "text differs"):
            pilot.make_case({**raw, "body": raw["body"] + " modified"}, packet, review, "005930")

    def test_unknown_zone_stays_date_without_invented_clock(self):
        raw, packet, review = self.case()
        raw.update(published_at_raw="2021-01-28 09:00:00", published_at="2021-01-28 09:00:00", published_timezone=None)
        case = pilot.make_case(raw, packet, review, "005930")
        self.assertEqual(case["input"]["published_at"], "2021-01-28")
        self.assertEqual(case["provenance"]["publication_precision"], "date")

    def test_heldout_id_url_and_whitespace_normalized_body_are_excluded(self):
        held = {"ids": {"test-id"}, "urls": {"https://example.org/news/1"}, "bodies": {pilot.text_hash("held out body")}}
        base = {"record_id": "train-id", "title": "Title", "body": "new article", "url": "https://example.org/new"}
        for change in ({"record_id": "test-id"}, {"url": "https://example.org/news/1?utm_source=feed#top"},
                       {"body": " held\n out   body "}):
            with self.subTest(change=change):
                self.assertTrue(pilot.overlaps_holdout({**base, **change}, held))
        self.assertFalse(pilot.overlaps_holdout(base, held))

    def test_holdout_index_respects_actual_split_and_requires_metadata(self):
        documents = [{"record_id": "a", "split": "train", "title": "A", "body": "training"},
                     {"record_id": "b", "split": "test", "title": "B", "body": "holdout"}]
        metadata = [{"record_id": "b", "canonical_url": "https://example.org/b"}]
        with patch.object(pilot, "rows", side_effect=lambda path: iter(documents if path == "docs" else metadata)):
            held = pilot.holdout_index("docs", "metadata")
        self.assertEqual(held["ids"], {"b"})
        self.assertEqual(held["counts"], {"test": 1})
        with patch.object(pilot, "rows", side_effect=lambda path: iter(documents if path == "docs" else [])):
            with self.assertRaisesRegex(ValueError, "every legacy"):
                pilot.holdout_index("docs", "metadata")

    def test_multicompany_uncertainty_is_not_blindly_transferred(self):
        packet = {"review_id": 9, "source_companies": [{"ticker": "005930"}, {"ticker": "006400"}]}
        review = {"verdict": "uncertain"}
        self.assertEqual(pilot.eligible_tickers(packet, review), ["005930"])
        self.assertEqual(pilot.eligible_tickers({**packet, "review_id": 999}, review), [])
        self.assertEqual(pilot.eligible_tickers(packet, {"verdict": "supported", "supported_tickers": ["006400"]}), ["006400"])

    def test_balanced_selection_is_independent_of_input_order(self):
        candidates = [{"stratum": name, "input": {"article_id": name + ":" + str(i)}}
                      for name in pilot.STRATA for i in range(12)]
        first, _ = pilot.choose(candidates, 10)
        second, _ = pilot.choose(list(reversed(copy.deepcopy(candidates))), 10)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 40)
        self.assertEqual(dict(pilot.Counter(row["stratum"] for row in first)), dict.fromkeys(pilot.STRATA, 10))


if __name__ == "__main__":
    unittest.main()
