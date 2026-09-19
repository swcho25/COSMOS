"""Evaluation preserves abstentions, provenance and real/synthetic separation."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_local_relevance as evaluation
import local_news_relevance as runner


class LocalRelevanceEvaluationTests(unittest.TestCase):
    def test_sentence_id_roundtrip_and_invalid_id_evidence_failure(self):
        import relevance_sentence_evidence as evidence
        article = self.article()
        article["body"] += " 마지막 문장도 원문 그대로 남아 있다."
        catalog = evidence.build_sentence_catalog(article)
        ref = self.reference(article)
        raw_prediction = {key: value for key, value in self.prediction().items() if key != "evidence_sentences"}
        raw_prediction["evidence_ids"] = [catalog[-1]["id"]]
        protocol = {"max_input_tokens": 6144, "max_new_tokens": 768, "evidence_mode": "sentence_ids"}
        for ids, expected_errors in (([catalog[-1]["id"]], []), ([99999], ["unknown_evidence_id"])):
            raw_prediction["evidence_ids"] = ids
            raw = json.dumps(raw_prediction, ensure_ascii=False)
            prediction, errors = runner.validate_prediction(raw, article, "sentence_ids")
            self.assertEqual(errors, expected_errors)
            result = self.result(article)
            result.update(raw_output=raw, prediction=prediction, validation_errors=errors,
                          evidence_mode="sentence_ids", effective_selection="보류" if errors else "채택")
            evaluation.validate_result(result, article, protocol)
            summary, audits, _ = evaluation.compare({evaluation.key(article): article},
                {evaluation.key(article): ref}, {evaluation.key(article): result}, protocol)
            self.assertEqual(audits[0]["quote_failure"], bool(errors))
            if not errors:
                self.assertEqual(prediction["evidence_sentences"], [catalog[-1]["text"]])

    def article(self, suffix="real", kind="real"):
        return {"article_id": suffix, "ticker": "005930", "company_name": "삼성전자",
                "title": "삼성전자 공급 계약", "body": "삼성전자는 신규 공급 계약을 체결했다고 발표했다.",
                "published_at": "2021-06-15", "company_context": None,
                "source_split": "train" if kind == "real" else "diagnostic", "case_kind": kind}

    def prediction(self):
        return {"label": "직접관련", "event_summary": "삼성전자가 공급 계약을 발표했다.",
                "evidence_sentences": ["삼성전자는 신규 공급 계약을 체결했다고 발표했다."],
                "economic_path": "계약에 따른 공급과 매출이다.", "time_status": "신규", "selection": "채택"}

    def result(self, article, prediction=None):
        prediction = self.prediction() if prediction is None else prediction
        raw = json.dumps(prediction, ensure_ascii=False)
        value, errors = runner.validate_prediction(raw, article)
        return {"article_id": article["article_id"], "ticker": article["ticker"],
                "input_sha256": hashlib.sha256(runner.canonical(article).encode("utf8")).hexdigest(),
                "case_kind": article["case_kind"], "source_split": article["source_split"],
                "prediction": value, "raw_output": raw, "validation_errors": errors,
                "effective_selection": "보류" if errors else value["selection"],
                "input_tokens": 100, "output_tokens": 50, "generation_complete": True,
                "elapsed_seconds": 2.0, "peak_cuda_allocated_bytes": 2 * 1024 ** 3}

    def reference(self, article, selection="채택"):
        return {"article_id": article["article_id"], "ticker": article["ticker"],
                "expected_label": None, "expected_selection": selection,
                "reference_type": "prior_ai_review", "reason": "Existing AI reference; not human gold"}

    @staticmethod
    def indexed(*rows):
        return {evaluation.key(row): row for row in rows}

    def test_real_synthetic_separate_three_way_matrix_and_complete_review_body(self):
        real, synthetic = self.article(), self.article("synthetic", "synthetic")
        ref_real, ref_synthetic = self.reference(real, "제외"), self.reference(synthetic, "보류")
        fabricated = {**self.prediction(), "evidence_sentences": ["삼성전자 매출이 두 배 늘었다."]}
        result_real, result_synthetic = self.result(real), self.result(synthetic, fabricated)
        summary, audits, packets = evaluation.compare(
            self.indexed(real, synthetic), self.indexed(ref_real, ref_synthetic), self.indexed(result_real, result_synthetic))
        a, b = summary["by_case_kind"]["real"], summary["by_case_kind"]["synthetic"]
        self.assertEqual(a["effective_selection_matrix"]["제외"]["채택"], 1)
        self.assertEqual(b["effective_selection_matrix"]["보류"]["보류"], 1)
        self.assertEqual(b["raw_selection_matrix"]["보류"]["채택"], 1)
        self.assertEqual(b["invalid_count"], 1)
        self.assertEqual(b["quote_failure_count"], 1)
        self.assertEqual(b["invalid_forced_abstention_count"], 1)
        self.assertEqual(b["valid_abstention_count"], 0)
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0]["body"], real["body"])
        self.assertEqual(packets[0]["prior_reference"], ref_real)
        self.assertEqual(a["label_reference_pairs"], 0)
        self.assertFalse(summary["reference_is_gold"])
        self.assertIsNone(summary["population_accuracy"])
        self.assertIsNone(summary["gnn_performance"])
        self.assertEqual(summary["runtime_all_pairs"]["output_tokens_per_elapsed_second"], 25)
        self.assertEqual(summary["runtime_all_pairs"]["peak_cuda_allocated_gib"], 2)

    def test_pair_coverage_rejects_both_missing_and_extra(self):
        article = self.article()
        indexed = self.indexed(article)
        with self.assertRaisesRegex(ValueError, "reference pair mismatch"):
            evaluation.require_same_pairs(indexed, {}, indexed)
        with self.assertRaisesRegex(ValueError, "results pair mismatch"):
            evaluation.require_same_pairs(indexed, indexed, {**indexed, ("extra", "T"): {}})

    def test_corrupt_input_hash_and_forged_success_flags_are_not_counted(self):
        article = self.article()
        result = self.result(article)
        for changes in ({"input_sha256": "0" * 64}, {"effective_selection": "제외"},
                        {"validation_errors": ["fake"]}, {"case_kind": "synthetic"},
                        {"elapsed_seconds": float("nan")}, {"input_tokens": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evaluation.validate_result({**result, **changes}, article)

    def test_valid_abstention_is_distinct_from_invalid_output(self):
        article = self.article()
        prediction = {**self.prediction(), "label": "판단보류", "selection": "보류", "time_status": "불명",
                      "economic_path": "", "evidence_sentences": []}
        summary, audits, packets = evaluation.compare(self.indexed(article), self.indexed(self.reference(article, "보류")),
                                                      self.indexed(self.result(article, prediction)))
        group = summary["by_case_kind"]["real"]
        self.assertEqual(group["valid_abstention_count"], 1)
        self.assertEqual(group["invalid_count"], 0)
        self.assertEqual(packets, [])

    def test_synthetic_label_and_time_disagreements_are_visible(self):
        article = self.article("synthetic", "synthetic")
        reference = {**self.reference(article), "expected_label": "간접관련", "expected_time_status": "과거"}
        summary, audits, packets = evaluation.compare(self.indexed(article), self.indexed(reference), self.indexed(self.result(article)))
        group = summary["by_case_kind"]["synthetic"]
        self.assertEqual(group["label_matrix"]["간접관련"]["직접관련"], 1)
        self.assertEqual(group["time_status_matrix"]["과거"]["신규"], 1)
        self.assertIn("label_reference_disagreement", packets[0]["review_reasons"])
        self.assertIn("time_status_reference_disagreement", packets[0]["review_reasons"])

    def test_overlength_and_incomplete_generations_stay_visible(self):
        article = self.article()
        result = self.result(article)
        overlength = {**result, "input_tokens": 6145, "output_tokens": 0, "raw_output": "", "prediction": None,
                      "generation_complete": False, "effective_selection": "보류", "validation_errors": [evaluation.OVERLENGTH]}
        protocol = {"max_input_tokens": 6144, "max_new_tokens": 768}
        summary, _, _ = evaluation.compare(self.indexed(article), self.indexed(self.reference(article)), self.indexed(overlength), protocol)
        self.assertEqual(summary["by_case_kind"]["real"]["overlength_count"], 1)
        self.assertIsNone(summary["runtime_all_pairs"]["seconds_per_output_token"])
        incomplete = {**result, "generation_complete": False, "validation_errors": ["generation_incomplete"], "effective_selection": "보류"}
        self.assertEqual(evaluation.validate_result(incomplete, article, protocol), ["generation_incomplete"])

    def test_multi_reference_files_hashes_duplicate_detection_and_immutable_output(self):
        parent = Path(__file__).resolve().parent
        root = parent / (".relevance-evaluation-test-" + uuid.uuid4().hex)
        root.mkdir()
        try:
            a, b = self.article(), self.article("synthetic", "synthetic")
            paths = [root / name for name in ("input.jsonl", "ref-real.jsonl", "ref-synthetic.jsonl", "results.jsonl")]
            for path, records in zip(paths, ([a, b], [self.reference(a)], [self.reference(b)], [self.result(a), self.result(b)])):
                evaluation.write_jsonl(path, records)
            before = {path: path.read_bytes() for path in paths}
            out = root / "evaluation"
            summary = evaluation.evaluate(inputs=[paths[0]], reference=paths[1:3], results=paths[3], out=out)
            self.assertEqual(len(summary["sources"]["reference"]), 2)
            self.assertEqual(summary["sources"]["inputs"][0]["sha256"], evaluation.sha(paths[0]))
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertEqual((out / "review_packet.jsonl").read_text(encoding="utf8"), "")
            with self.assertRaises(FileExistsError):
                evaluation.evaluate(inputs=[paths[0]], reference=paths[1:3], results=paths[3], out=out)
            with self.assertRaisesRegex(ValueError, "Duplicate reference"):
                evaluation.index_rows([paths[1], paths[1]], "reference")
        finally:
            if root.resolve().parent != parent:
                raise RuntimeError("Test cleanup escaped test directory")
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
