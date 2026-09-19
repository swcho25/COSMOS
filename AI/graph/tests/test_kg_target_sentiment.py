"""CPU-only source attribution, null handling, inference audit and cache tests."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_target_sentiment as sentiment


def row(body="Apple reported that its profit has increased.", **kwargs):
    result = {"article_id": "article-1", "ticker": "AAPL", "company_name": "Apple Inc.",
              "title": "Company results", "body": body, "language": "en",
              "cleaned_title": "Company results", "cleaned_body": body,
              "target_name_aliases": ["Apple"], "other_company_names": ["Samsung"],
              "evidence": [{"source": "body", "start": 0, "end": len(body),
                            "quote": body, "cleaned_quote": body}],
              "input_sha256": "b" * 64}
    result.update(kwargs)
    return result


class FakeRuntime:
    max_tokens = 512
    def __init__(self, spec, device):
        self.repo, self.calls = spec["repo"], []
    def token_lengths(self, texts):
        return [len(text.split()) + 2 for text in texts]
    def predict(self, texts):
        self.calls.append(list(texts))
        return [[0.8, 0.1, 0.1] if "fell" in text else [0.1, 0.8, 0.1] if "meeting" in text
                else [0.4, 0.4, 0.2] if "tie" in text else [0.1, 0.1, 0.8] for text in texts]


class TargetSentimentTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "artifacts" / ("test_target_sentiment_" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        pretrained = {}
        for language in ("ko", "en"):
            role = "sentiment_" + language
            model = self.folder / role
            model.mkdir()
            labels = ["negative", "neutral", "positive"] if language == "ko" else ["positive", "negative", "neutral"]
            (model / "config.json").write_text(json.dumps({"id2label": dict(enumerate(labels))}), encoding="utf-8")
            (model / "weights.bin").write_bytes(b"fixture-not-a-model")
            pretrained[role] = {"path": role, "repo": "fixture/" + language, "revision": "a" * 40}
        (self.folder / "pretrained.json").write_text(json.dumps(pretrained), encoding="utf-8")
        self.predictor = sentiment.TargetSentimentPredictor(self.folder, device="cpu", batch_size=2,
                                                           runtime_factory=FakeRuntime)

    def tearDown(self):
        self.predictor.close()
        path = self.folder.resolve()
        assert path.parent == (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        shutil.rmtree(path)

    def test_source_exact_context_only_and_input_unchanged(self):
        article = row(title="Samsung faces terrible losses")
        before = copy.deepcopy(article)
        result = self.predictor.predict([article])[0]
        self.assertEqual(article, before)
        self.assertEqual(result["label"], "POSITIVE")
        self.assertEqual(result["contexts"][0]["actual_input"], article["body"])
        self.assertNotIn(article["title"], result["contexts"][0]["actual_input"])
        self.assertEqual(result["probabilities"], {"negative": 0.1, "neutral": 0.1, "positive": 0.8})
        self.assertAlmostEqual(result["score"], 0.7)
        self.assertFalse(result["is_stock_price_prediction"])
        self.assertEqual(result["input_sha256"], "b" * 64)
        self.assertEqual(result["contexts"][0]["input_sha256"], sentiment.hashlib.sha256(article["body"].encode()).hexdigest())

    def test_invalid_original_span_or_altered_quote_is_fatal(self):
        for change in ({"start": 1}, {"quote": "Apple said something else"}, {"end": True}, {"source": "missing"}):
            with self.subTest(change=change):
                article = row()
                article["evidence"][0].update(change)
                with self.assertRaises(ValueError):
                    self.predictor.predict([article])
        self.assertEqual(self.predictor._runtimes, {})

    def test_cleaned_input_must_occur_in_cleaned_field_and_widgets_are_removed(self):
        original = "Apple(AAPL +3.5%) reported that its profit has increased."
        clean = "Apple reported that its profit has increased."
        article = row(original, cleaned_body=clean)
        article["evidence"][0]["cleaned_quote"] = clean
        result = self.predictor.predict([article])[0]
        self.assertEqual(result["contexts"][0]["quote"], original)
        self.assertEqual(result["contexts"][0]["actual_input"], clean)
        article["evidence"][0]["cleaned_quote"] = original
        with self.assertRaisesRegex(ValueError, "cleaned source"):
            self.predictor.predict([article])
        article["cleaned_body"] = original
        with self.assertRaises(ValueError):
            self.predictor.predict([article])

    def test_quote_cannot_borrow_a_different_sentence_from_same_article(self):
        first, second = "Apple reported that its profit fell.", "Apple said that revenue increased."
        article = row(first + " " + second)
        article["evidence"] = [{"field": "body", "start": 0, "end": len(first),
                               "text": first, "cleaned_quote": second}]
        with self.assertRaisesRegex(ValueError, "this original quote"):
            self.predictor.predict([article])

    def test_mapped_title_preserved_when_body_cleanup_would_trim_headline(self):
        title = "Fastenal’s Q4 Earnings Call: Our Top 5 Analyst Questions"
        article = row("Fastenal reported that its earnings increased.", ticker="FAST", company_name="Fastenal",
                      title=title, cleaned_title=title, target_name_aliases=["Fastenal"],
                      evidence=[{"source": "title", "start": 0, "end": len(title), "quote": title,
                                 "cleaned_start": 0, "cleaned_end": len(title), "cleaned_quote": title}])
        article["cleaned_body"] = sentiment.sanitize_article(article)["body"]
        standalone = sentiment.sanitize_article({"title": "", "body": title})["body"]
        self.assertNotEqual(standalone, title)
        result = self.predictor.predict([article])[0]
        self.assertEqual(result["status"], "analyzed")
        self.assertEqual(result["contexts"][0]["actual_input"], title)
        self.assertEqual(result["contexts"][0]["source_validation"]["method"],
                         "full_article_cleaned_slice_and_original_offset_map_v1")

    def test_mapped_offsets_reject_changed_source_cross_sentence_or_partial_coordinates(self):
        first = "Apple reported that its earnings increased."
        second = "Apple said that its profit fell."
        article = row(first + " " + second)
        article["evidence"] = [{"source": "body", "start": 0, "end": len(first), "quote": first,
                               "cleaned_start": 0, "cleaned_end": len(first), "cleaned_quote": first}]
        self.assertEqual(self.predictor.predict([article])[0]["status"], "analyzed")
        for issue in ("source", "other_sentence", "partial", "boolean", "raw_endpoint"):
            bad = copy.deepcopy(article)
            evidence = bad["evidence"][0]
            if issue == "source":
                bad["cleaned_body"] += " fabricated material"
            elif issue == "other_sentence":
                evidence.update(cleaned_start=len(first) + 1, cleaned_end=len(bad["body"]), cleaned_quote=second)
            elif issue == "partial":
                del evidence["cleaned_end"]
            elif issue == "boolean":
                evidence["cleaned_start"] = False
            else:
                evidence.update(start=1, quote=first[1:])
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                self.predictor.predict([bad])

    def test_mapped_removed_widget_validates_noncontiguous_original_offsets(self):
        original = "Apple(AAPL +3.5%) reported that its profit has increased."
        clean = "Apple reported that its profit has increased."
        article = row(original, cleaned_body=clean)
        article["evidence"][0].update(cleaned_quote=clean, cleaned_start=0, cleaned_end=len(clean))
        result = self.predictor.predict([article])[0]
        self.assertEqual(result["contexts"][0]["quote"], original)
        self.assertEqual(result["contexts"][0]["actual_input"], clean)

    def test_missing_evidence_language_or_target_never_fakes_neutral(self):
        examples = [row(evidence=[]), row(language="ja"), row("The company reported that its profit has increased.")]
        results = self.predictor.predict(examples)
        self.assertEqual([r["status"] for r in results], ["missing_evidence", "unsupported_language", "ambiguous_target_context"])
        for result in results:
            self.assertIsNone(result["label"])
            self.assertIsNone(result["probabilities"])
            self.assertNotIn("pending", [context["status"] for context in result["contexts"]])
        self.assertEqual(self.predictor._runtimes, {})

    def test_company_name_boundaries_prevent_substring_match(self):
        result = self.predictor.predict([row("Appleton said that its profit has increased.")])[0]
        self.assertEqual(result["status"], "ambiguous_target_context")

    def test_multiple_companies_and_uncertain_attribution_are_null(self):
        examples = [row("Apple gained while Samsung lost profits."), row(), row()]
        examples[1]["evidence"][0]["attribution"] = "unclear"
        examples[2]["evidence"][0]["target_tickers"] = ["AAPL", "005930"]
        for result in self.predictor.predict(examples):
            self.assertEqual(result["status"], "ambiguous_target_context")
            self.assertIsNone(result["label"])
        self.assertEqual(self.predictor._runtimes, {})

    def test_unsupported_language_is_independent_of_market_and_region(self):
        language, audit = sentiment.detect_language({"region": "domestic", "market": "KOSPI"}, "苹果公司发布财务报告")
        self.assertIsNone(language)
        self.assertEqual(audit["method"], "script_function_words_heuristic_v1")
        self.assertEqual(sentiment.detect_language({}, "Apple said that its sales have increased.")[0], "en")
        self.assertEqual(sentiment.detect_language({}, "삼성전자 매출이 증가했습니다.")[0], "ko")
        self.assertIsNone(sentiment.detect_language({}, "Apple earnings")[0])
        self.assertIsNone(sentiment.detect_language({"language": "ja"}, "Apple said that its sales have increased.")[0])

    def test_cross_language_roles_cached_and_no_broadcast_between_targets(self):
        kr = row("삼성전자의 영업이익이 증가했습니다.", article_id="kr", ticker="005930", company_name="삼성전자",
                 language="ko-KR", target_name_aliases=[], other_company_names=["Apple"])
        negative = row("Apple reported that its profit fell.")
        positive = row()
        results = self.predictor.predict([kr, negative, positive])
        self.assertEqual([x["label"] for x in results], ["POSITIVE", "NEGATIVE", "POSITIVE"])
        self.assertEqual(results[0]["model"]["repo"], "fixture/ko")
        self.assertEqual(results[1]["model"]["repo"], "fixture/en")
        cached = self.predictor._runtimes["sentiment_en"]
        self.predictor.predict([positive])
        self.assertIs(cached, self.predictor._runtimes["sentiment_en"])

    def test_opposing_context_predictions_preserved_but_aggregate_is_null(self):
        first, second = "Apple said that its revenue increased.", "Apple said that its profit fell."
        body = first + "\n" + second
        article = row(body)
        article["evidence"] = [{"field": "body", "start": 0, "end": len(first), "text": first, "cleaned_quote": first},
                               {"field": "body", "start": len(first) + 1, "end": len(body), "text": second, "cleaned_quote": second}]
        result = self.predictor.predict([article])[0]
        self.assertEqual(result["status"], "conflicting_polarity")
        self.assertIsNone(result["label"])
        self.assertIsNone(result["probabilities"])
        self.assertEqual([c["label"] for c in result["contexts"]], ["POSITIVE", "NEGATIVE"])

    def test_duplicate_span_cannot_overweight_context_and_tie_is_null(self):
        article = row()
        article["evidence"] *= 3
        result = self.predictor.predict([article])[0]
        self.assertEqual(len(result["contexts"]), 1)
        result = self.predictor.predict([row("Apple said that its results tie.")])[0]
        self.assertEqual(result["status"], "tied_probabilities")
        self.assertIsNone(result["label"])
        self.assertIsNotNone(result["contexts"][0]["probabilities"])

    def test_long_context_is_not_truncated_or_inferred(self):
        article = row("Apple " + "results " * 600)
        result = self.predictor.predict([article])[0]
        self.assertEqual(result["status"], "context_too_long")
        self.assertEqual(result["contexts"][0]["actual_input"], article["body"])
        self.assertFalse(result["contexts"][0]["truncated"])
        self.assertEqual(self.predictor._runtimes["sentiment_en"].calls, [])

    def test_model_hash_binds_weights_tokenizer_config_and_checked_before_load(self):
        provenance = self.predictor.provenance
        self.assertIn("weights.bin", provenance["models"]["sentiment_en"]["files_sha256"])
        self.assertEqual(provenance["models"]["sentiment_en"]["label_indexes"], {"positive": 0, "negative": 1, "neutral": 2})
        self.assertIn("../ner/matcher.py", provenance["code_sha256"])
        self.assertIn("kg_grounded_news.py", provenance["code_sha256"])
        (self.folder / "sentiment_en/weights.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.predictor.predict([row()])

    def test_invalid_probability_and_output_count_are_errors(self):
        for values in ([float("nan"), 0, 1], [0, 0, 0], [1.1, -0.1, 0], [0, 1]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                sentiment._probabilities(values)
        runtime = self.predictor._runtime("sentiment_en")
        runtime.predict = lambda texts: []
        with self.assertRaisesRegex(ValueError, "number of predictions"):
            self.predictor.predict([row()])


if __name__ == "__main__":
    unittest.main()
