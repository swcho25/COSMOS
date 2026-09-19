"""Only complete, immutable, non-gold AI reviews produce human-review candidates."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_relevance_review_candidates as builder
import evaluate_local_relevance as evaluation
import local_news_relevance as runner


class ReviewCandidateTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".review-candidate-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.original = self.root / "inputs.jsonl"
        self.inputs = self.root / "combined_inputs.jsonl"
        self.refs = [self.root / ("independent_reference_%02d.jsonl" % n) for n in (1, 2)]
        self.freeze = self.root / "independent_review_freeze.json"
        self.inference = self.root / "inference"
        self.inference.mkdir()
        self.results = self.inference / "results.jsonl"
        self.protocol_path = self.inference / "protocol.json"
        self.out = self.root / "candidates"
        self.real = [self.article(str(n)) for n in range(40)]
        self.synthetic = [self.article("synthetic-" + str(n), "synthetic") for n in range(8)]
        self.write_rows(self.original, self.real)
        self.write_rows(self.inputs, self.synthetic + self.real)
        originals = builder.read_frozen_jsonl(self.original)
        original_hash = evaluation.sha(self.original)
        self.references = []
        for n, article in enumerate(self.real):
            original = originals[evaluation.key(article)]
            reference = {"article_id": article["article_id"], "ticker": article["ticker"],
                         "expected_selection": evaluation.SELECTIONS[n % 3],
                         "expected_label": "직접관련", "expected_time_status": "신규",
                         "reason": "Fixture AI text review only", "evidence_sentences": [article["body"]],
                         "reference_type": "independent_ai_review", "human_gold": False}
            if n < 20:
                reference.update(input_sha256=original["canonical_sha256"], input_file_sha256=original_hash,
                                 input_row_number=original["line_number"], full_input_read=True,
                                 body_sha256=builder.hash_text(article["body"]),
                                 reviewed_body_chars=len(article["body"]), reviewed_title_chars=len(article["title"]),
                                 model_predictions_consulted=False, prior_evaluations_consulted=False)
            else:
                reference.update(input_sha256=original_hash, input_row_sha256=original["raw_line_sha256"],
                                 input_row_hash_scope=builder.ROW_HASH_SCOPE, source_line=original["line_number"],
                                 full_article_text_read=True, prompt_sha256="fixture-prompt-sha")
            self.references.append(reference)
        self.save_references()
        self.protocol = {"input_sha256": evaluation.sha(self.inputs), "rows": 48,
                         "prompt_sha256": "fixture-prompt-sha", "code_sha256": evaluation.sha(runner.__file__),
                         "max_input_tokens": 6144, "max_new_tokens": 768, "evidence_mode": "verbatim"}
        self.write_json(self.protocol_path, self.protocol)
        self.records = []
        for n, article in enumerate(self.real):
            selection = "채택" if n in (0, 1, 2, 4) else "보류" if n == 5 else "제외"
            self.records.append(self.result(article, selection, bad_quote=n == 4))
        self.records.extend(self.result(article, "채택") for article in self.synthetic)
        self.write_rows(self.results, self.records)
        self.pin()

    def tearDown(self):
        if self.root.resolve().parent != self.parent:
            raise RuntimeError("Test cleanup escaped test directory")
        shutil.rmtree(self.root)

    @staticmethod
    def article(identifier, kind="real"):
        return {"article_id": identifier, "ticker": "005930", "company_name": "삼성전자",
                "published_at": "2021-06-15", "title": "삼성전자 공급 계약 " + identifier,
                "body": "삼성전자는 신규 공급 계약을 체결했다고 발표했다.", "company_context": None,
                "source_url": "https://example.invalid/" + identifier,
                "source_split": "train" if kind == "real" else "diagnostic", "case_kind": kind}

    @staticmethod
    def result(article, selection, bad_quote=False):
        prediction = {"label": "판단보류" if selection == "보류" else "직접관련",
                      "event_summary": "공급 계약 발표", "economic_path": "신규 공급과 매출",
                      "time_status": "불명" if selection == "보류" else "신규", "selection": selection,
                      "evidence_sentences": ["원문에 전혀 없는 근거 문장이다." if bad_quote else article["body"]]}
        raw = runner.canonical(prediction)
        validated, errors = runner.validate_prediction(raw, article)
        return {"article_id": article["article_id"], "ticker": article["ticker"],
                "input_sha256": builder.hash_text(runner.canonical(article)), "case_kind": article["case_kind"],
                "source_split": article["source_split"], "prediction": validated, "raw_output": raw,
                "validation_errors": errors, "effective_selection": "보류" if errors else selection,
                "input_tokens": 100, "output_tokens": 50, "generation_complete": True,
                "elapsed_seconds": 2.0, "peak_cuda_allocated_bytes": 1024}

    @staticmethod
    def write_rows(path, rows):
        Path(path).write_text("".join(runner.canonical(row) + "\n" for row in rows), encoding="utf8")

    @staticmethod
    def write_json(path, value):
        Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")

    def save_references(self):
        self.write_rows(self.refs[0], self.references[:20])
        self.write_rows(self.refs[1], self.references[20:])

    def pin(self):
        self.write_json(self.freeze, {"files": {path.name: evaluation.sha(path)
                                                for path in (self.original, self.inputs, *self.refs)},
                                     "human_gold": False, "model_outputs_seen": False,
                                     "source_quotes_checked": True, "review_pairs": 40})

    def build(self):
        return builder.build(inputs=self.inputs, results=self.results, reference=self.refs, out=self.out)

    def test_complete_real_only_partition_and_full_provenance_preserves_sources(self):
        sources = (self.original, self.inputs, *self.refs, self.freeze, self.results, self.protocol_path)
        before = {path: path.read_bytes() for path in sources}
        audit = self.build()
        self.assertEqual((audit["real_pairs"], audit["review_agreed_candidates"], audit["disputed_candidates"]), (40, 1, 2))
        self.assertEqual(audit["counts"], {"review_agreed": 1, "disputed": 2, "not_model_adopted": 37})
        self.assertEqual(audit["invalid_model_results"], 1)
        self.assertEqual(audit["synthetic_input_pairs_excluded"], 8)
        self.assertEqual(audit["synthetic_result_pairs_ignored"], 8)
        self.assertEqual(len(audit["pairs"]), 40)
        self.assertEqual(audit["results"]["sha256"], evaluation.sha(self.results))
        self.assertEqual({path: path.read_bytes() for path in sources}, before)
        agreed = list(evaluation.read_rows(self.out / "review_agreed_candidates.jsonl"))
        disputed = list(evaluation.read_rows(self.out / "disputed_candidates.jsonl"))
        self.assertEqual([row["article_id"] for row in agreed], ["0"])
        self.assertEqual([row["article_id"] for row in disputed], ["1", "2"])
        for row in agreed + disputed:
            self.assertFalse(row["human_reviewed"])
            self.assertFalse(row["human_gold"])
            self.assertFalse(row["gnn_labels_generated"])
            self.assertTrue(row["ai_assisted_candidate"])
            self.assertNotIn("gnn_label", row)
            self.assertEqual(row["body"], self.real[int(row["article_id"])]["body"])
        self.assertIsNone(audit["ensemble_accuracy"])
        self.assertIsNone(audit["population_accuracy"])
        with self.assertRaises(FileExistsError):
            self.build()

    def test_missing_real_or_duplicate_results_fail_before_output(self):
        for records, message in ((self.records[1:], "results pair mismatch"),
                                 (self.records + [self.records[0]], "Duplicate model result")):
            with self.subTest(message=message):
                self.write_rows(self.results, records)
                with self.assertRaisesRegex(ValueError, message):
                    self.build()
                self.assertFalse(self.out.exists())

    def test_synthetic_results_and_synthetic_reference_are_not_required(self):
        self.write_rows(self.results, self.records[:40])
        audit = self.build()
        self.assertEqual(audit["synthetic_result_pairs_ignored"], 0)
        self.assertEqual(audit["real_result_pairs"], 40)

    def test_frozen_reference_modification_and_human_gold_are_rejected(self):
        with self.refs[0].open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "Frozen source changed"):
            self.build()
        self.save_references()
        self.references[0]["human_gold"] = True
        self.save_references()
        self.pin()
        with self.assertRaisesRegex(ValueError, "explicitly marked non-gold"):
            self.build()
        self.assertFalse(self.out.exists())

    def test_both_reference_hash_schemas_and_source_quotes_are_verified(self):
        scenarios = [(0, "input_sha256", "0" * 64, "canonical row/file"),
                     (20, "input_row_sha256", "0" * 64, "line/file"),
                     (20, "prompt_sha256", "different", "different decision prompt"),
                     (0, "evidence_sentences", ["실제 기사에는 없는 인용문이다."], "quote is not in"),
                     (0, "future_outcomes_consulted", True, "price outcomes")]
        base = copy.deepcopy(self.references)
        for index, field, value, message in scenarios:
            with self.subTest(field=field, index=index):
                self.references = copy.deepcopy(base)
                self.references[index][field] = value
                self.save_references()
                self.pin()
                with self.assertRaisesRegex(ValueError, message):
                    self.build()
                self.assertFalse(self.out.exists())

    def test_forged_effective_adoption_and_input_hash_fail_closed(self):
        base = copy.deepcopy(self.records)
        for index, field, value in ((4, "effective_selection", "채택"), (0, "input_sha256", "0" * 64)):
            with self.subTest(field=field):
                records = copy.deepcopy(base)
                records[index][field] = value
                self.write_rows(self.results, records)
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse(self.out.exists())

    def test_concurrent_results_change_cannot_receive_a_hash_for_unread_rows(self):
        validate = evaluation.validate_result
        changed = False

        def append_after_read(*args, **kwargs):
            nonlocal changed
            result = validate(*args, **kwargs)
            if not changed:
                with self.results.open("a", encoding="utf8") as stream:
                    stream.write("\n")
                changed = True
            return result

        with patch.object(evaluation, "validate_result", side_effect=append_after_read):
            with self.assertRaisesRegex(ValueError, "results changed during"):
                self.build()
        self.assertFalse(self.out.exists())

    def test_sentence_ids_reuse_resolution_and_require_pinned_resolver_code(self):
        import relevance_sentence_evidence as evidence
        articles = {evaluation.key(row): row for row in self.real + self.synthetic}
        for record in self.records:
            article = articles[evaluation.key(record)]
            source_id = next(entry["id"] for entry in evidence.build_sentence_catalog(article)
                             if entry["source"] == "body")
            raw_prediction = copy.deepcopy(record["prediction"])
            raw_prediction.pop("evidence_sentences")
            raw_prediction["evidence_ids"] = [9999] if article["article_id"] == "4" else [source_id]
            raw = runner.canonical(raw_prediction)
            prediction, errors = runner.validate_prediction(raw, article, "sentence_ids")
            record.update(raw_output=raw, prediction=prediction, validation_errors=errors,
                          effective_selection="보류" if errors else prediction["selection"], evidence_mode="sentence_ids")
        self.write_rows(self.results, self.records)
        self.protocol.update(evidence_mode="sentence_ids", evidence_code_sha256="0" * 64)
        self.write_json(self.protocol_path, self.protocol)
        with self.assertRaisesRegex(ValueError, "Evidence resolver differs"):
            self.build()
        self.assertFalse(self.out.exists())
        self.protocol["evidence_code_sha256"] = evaluation.sha(evidence.__file__)
        self.write_json(self.protocol_path, self.protocol)
        audit = self.build()
        self.assertEqual((audit["review_agreed_candidates"], audit["disputed_candidates"]), (1, 2))
        self.assertEqual(audit["invalid_model_results"], 1)
        self.assertEqual(audit["evidence_resolver"]["sha256"], evaluation.sha(evidence.__file__))
        self.assertEqual(audit["model_protocol"]["evidence_mode"], "sentence_ids")
        adopted = next(evaluation.read_rows(self.out / "review_agreed_candidates.jsonl"))
        self.assertEqual(adopted["relevance_prediction"]["evidence_sentences"], [self.real[0]["body"]])

    def test_v4_attention_hash_required_and_checked_by_evaluator_and_builder(self):
        attention_path = self.root / "local_relevance_attention.py"
        # A non-importable fixture proves verification hashes code without loading it.
        attention_path.write_text("raise RuntimeError('Must not execute attention code')\n", encoding="utf8")
        synthetic_ref = self.root / "synthetic_reference.jsonl"
        self.write_rows(synthetic_ref, [{"article_id": row["article_id"], "ticker": row["ticker"],
                                        "expected_selection": "채택", "expected_label": "직접관련"}
                                       for row in self.synthetic])
        evaluation_out = self.root / "evaluation"

        def evaluate():
            return evaluation.evaluate(inputs=[self.inputs], reference=[*self.refs, synthetic_ref],
                                       results=self.results, out=evaluation_out)

        self.protocol["attn_implementation"] = "sdpa_explicit_kv"
        with patch.object(evaluation, "ATTENTION_CODE_PATH", attention_path):
            for value, message in ((None, "missing its code hash"), ("0" * 64, "Attention implementation differs")):
                if value is not None:
                    self.protocol["attention_code_sha256"] = value
                self.write_json(self.protocol_path, self.protocol)
                for action in (self.build, evaluate):
                    with self.subTest(action=action.__name__, value=value):
                        with self.assertRaisesRegex(ValueError, message):
                            action()
                self.assertFalse(self.out.exists())
                self.assertFalse(evaluation_out.exists())
            digest = evaluation.sha(attention_path)
            self.protocol["attention_code_sha256"] = digest
            self.write_json(self.protocol_path, self.protocol)
            audit, summary = self.build(), evaluate()
        self.assertEqual(audit["model_protocol"]["attention_code_sha256"], digest)
        self.assertEqual(audit["attention_implementation"]["sha256"], digest)
        self.assertEqual(summary["run"]["attn_implementation"], "sdpa_explicit_kv")
        self.assertEqual(summary["sources"]["attention_implementation"]["sha256"], digest)


if __name__ == "__main__":
    unittest.main()
