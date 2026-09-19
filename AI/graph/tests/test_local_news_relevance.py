"""Schema, source evidence, leakage boundaries and restart integrity (no GPU)."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import uuid
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_news_relevance as relevance


class LocalRelevanceTests(unittest.TestCase):
    def test_results_without_original_protocol_cannot_acquire_a_new_contract(self):
        fixture_root = Path(__file__).resolve().parents[1] / "artifacts"
        out = fixture_root / ("protocol-test-" + uuid.uuid4().hex)
        out.mkdir()
        self.assertTrue(out.resolve().is_relative_to(fixture_root.resolve()))
        try:
            (out / "results.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "without their original protocol"):
                relevance.pin_protocol(out, {"prompt_sha256": "new-prompt"})
            self.assertFalse((out / "protocol.json").exists())
        finally:
            (out / "results.jsonl").unlink(missing_ok=True)
            out.rmdir()

    def setUp(self):
        self.row = {"article_id": "synthetic-unit", "ticker": "SYN-001", "company_name": "가상기업",
                    "published_at": "2021-06-15T09:00:00+09:00", "title": "가상기업 신규 공급 계약 발표",
                    "body": "가상기업은 15일 신규 공급 계약을 체결했다고 발표했다. 계약 물량은 다음 달부터 출하한다.",
                    "company_context": None, "case_kind": "synthetic", "source_split": "diagnostic"}
        self.valid = {"label": "직접관련", "event_summary": "가상기업이 신규 공급 계약을 발표했다.",
                      "evidence_sentences": ["가상기업은 15일 신규 공급 계약을 체결했다고 발표했다."],
                      "economic_path": "계약한 제품의 출하와 매출로 연결된다.", "time_status": "신규", "selection": "채택"}

    def prediction(self, **changes):
        return relevance.validate_prediction(json.dumps({**self.valid, **changes}, ensure_ascii=False), self.row)

    def result(self):
        return {"article_id": self.row["article_id"], "ticker": self.row["ticker"],
                "input_sha256": hashlib.sha256(relevance.canonical(self.row).encode()).hexdigest(),
                "case_kind": "synthetic", "source_split": "diagnostic", "input_tokens": 200,
                "output_tokens": 150, "generation_complete": True,
                "raw_output": json.dumps(self.valid, ensure_ascii=False), "prediction": copy.deepcopy(self.valid),
                "validation_errors": [], "effective_selection": "채택"}

    def test_valid_current_event_with_literal_quote(self):
        prediction, errors = self.prediction()
        self.assertEqual(prediction, self.valid)
        self.assertEqual(errors, [])

    def test_non_json_and_extra_schema_keys_are_rejected(self):
        for raw in ["```json\n{}\n```", "not JSON", "{", "null", "[]"]:
            with self.subTest(raw=raw):
                self.assertTrue(relevance.validate_prediction(raw, self.row)[1])
        _, errors = self.prediction(stock_return=0.2)
        self.assertIn("invalid_schema_keys", errors)

    def test_invalid_enum_and_field_types_are_rejected(self):
        for change in [{"label": "관련"}, {"time_status": "미래"}, {"selection": "승인"},
                       {"event_summary": []}, {"economic_path": {}}, {"evidence_sentences": "본문"},
                       {"evidence_sentences": [None]}]:
            with self.subTest(change=change):
                self.assertTrue(self.prediction(**change)[1])

    def test_fabricated_translated_and_fragment_quotes_are_rejected(self):
        for quote in ["가상기업의 영업이익이 크게 늘었다.", "The company announced a new contract.", "가상기업"]:
            with self.subTest(quote=quote):
                self.assertTrue(self.prediction(evidence_sentences=[quote])[1])

    def test_context_quote_does_not_count_as_article_evidence(self):
        self.row["company_context"] = {"source": "fixture", "as_of": "2021-06-01", "business": "가상기업은 반도체 제조사다."}
        _, errors = self.prediction(evidence_sentences=["가상기업은 반도체 제조사다."])
        self.assertIn("evidence_not_in_article", errors)

    def test_past_uncertain_or_unsupported_adoption_is_rejected(self):
        for change in [{"time_status": "과거"}, {"time_status": "불명"}, {"label": "단순언급"},
                       {"label": "판단보류"}, {"evidence_sentences": []}, {"economic_path": "  "}]:
            with self.subTest(change=change):
                self.assertTrue(self.prediction(**change)[1])
        self.assertIn("uncertain_requires_abstention", self.prediction(label="판단보류", selection="제외")[1])
        self.assertEqual(self.prediction(label="판단보류", selection="보류", evidence_sentences=[], economic_path="")[1], [])

    def test_model_messages_drop_reference_price_and_other_pair_results(self):
        self.row.update(expected_label="LEAK_LABEL", expected_selection="LEAK_SELECTION",
                        future_return="LEAK_RETURN", evaluation={"label": "LEAK_REVIEW"},
                        other_company_decisions=["LEAK_PEER"])
        messages = relevance.messages_for(self.row, "system instructions")
        payload = json.loads(messages[1]["content"])
        self.assertEqual(set(payload), {"published_at", "title", "body", "ticker", "company_name", "company_context"})
        self.assertNotIn("LEAK_", messages[1]["content"])
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")

    def test_real_records_must_be_train_2020_through_2022(self):
        self.row.update(case_kind="real", source_split="train")
        relevance.validate_input(self.row)
        for change in [{"source_split": "validation"}, {"source_split": "test"}, {"published_at": "2023-01-01"}]:
            row = {**self.row, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                relevance.validate_input(row)

    def test_same_day_future_context_and_timezone_comparison(self):
        self.row["company_context"] = {"source": "fixture", "as_of": "2021-06-15T09:01:00+09:00", "business": "부품 제조"}
        with self.assertRaisesRegex(ValueError, "Future"):
            relevance.validate_input(self.row)
        self.row["company_context"]["as_of"] = "2021-06-15T00:00:00Z"
        relevance.validate_input(self.row)

    def test_context_unknown_keys_are_rejected(self):
        self.row["company_context"] = {"source": "fixture", "as_of": "2021-06-01", "business": "부품 제조", "future_return": .15}
        with self.assertRaises(ValueError):
            relevance.messages_for(self.row, "prompt")

    def test_nested_reference_under_allowed_context_key_is_rejected(self):
        self.row["company_context"] = {"source": "fixture", "as_of": "2021-06-01",
                                       "business": {"expected_selection": "LEAK_VALUE"}}
        with self.assertRaises(ValueError):
            relevance.messages_for(self.row, "prompt")

    def test_published_timestamp_is_validated_without_company_context(self):
        self.row.update(case_kind="real", source_split="train", published_at="2021-not-a-date")
        with self.assertRaises(ValueError):
            relevance.validate_input(self.row)

    def test_resume_revalidates_input_provenance_raw_and_effective_selection(self):
        result = self.result()
        relevance.validate_resume_result(result, self.row, 6144, 768)
        for change in [{"input_sha256": "0" * 64}, {"source_split": "test"}, {"case_kind": "real"},
                       {"effective_selection": "제외"}, {"validation_errors": ["fake"]},
                       {"raw_output": "{}"}, {"generation_complete": False}, {"output_tokens": 769}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                relevance.validate_resume_result({**result, **change}, self.row, 6144, 768)

    def test_resume_incomplete_generation_stays_held(self):
        result = self.result()
        result.update(generation_complete=False, validation_errors=["generation_incomplete"], effective_selection="보류")
        relevance.validate_resume_result(result, self.row, 6144, 768)
        with self.assertRaises(ValueError):
            relevance.validate_resume_result({**result, "effective_selection": "채택"}, self.row, 6144, 768)

    def test_resume_overlength_cannot_contain_any_generated_text(self):
        result = self.result()
        result.update(input_tokens=6145, output_tokens=0, raw_output="", prediction=None,
                      generation_complete=False, validation_errors=["input_too_long_full_article_not_processed"], effective_selection="보류")
        relevance.validate_resume_result(result, self.row, 6144, 768)
        with self.assertRaises(ValueError):
            relevance.validate_resume_result({**result, "raw_output": "pretend output"}, self.row, 6144, 768)


if __name__ == "__main__":
    unittest.main()
