"""CPU-only synthetic evaluator checks; no articles, models or reference runs read."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import shutil
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_knowledge_graph_relevance as evaluation
import knowledge_graph_news_preview as preview
import knowledge_graph_relevance_policy as policy
import run_knowledge_graph_relevance as runner


class GraphReferenceEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.test_parent = (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        self.root = self.test_parent / ("test_kg_evaluator_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True, exist_ok=False)
        self.addCleanup(self.cleanup_workspace)
        self.inputs_path = self.root / "inputs.jsonl"
        self.run_path = self.root / "run"
        self.run_path.mkdir()
        self.a_path, self.b_path = self.root / "a.jsonl", self.root / "b.jsonl"
        self.out = self.root / "evaluation"
        self.rows = [{"article_id": "fiction-" + str(i), "ticker": "FICTION",
                      "company_name": "가상기업", "published_at": "2021-01-01",
                      "title": "가상기업 자료 " + str(i),
                      "body": "가상기업은 2015년 계약을 체결했다.", "source_url": None,
                      "company_context": None, "case_kind": "synthetic"} for i in range(6)]
        self.write_rows(self.inputs_path, self.rows)
        self.protocol = {"schema": "cosmos-kg-relevance-run-1", "policy_version": policy.VERSION,
                         "input_sha256": preview.file_sha256(self.inputs_path),
                         "code_hashes": runner.code_hashes(), "pairs": len(self.rows),
                         "model_repo": "synthetic/no-model-loaded", "model_revision": "synthetic",
                         "model_receipt_sha256": "0" * 64,
                         "max_input_tokens": 100, "max_new_tokens": 100,
                         "stage2_receives_stage1_output": False, "truncation": False,
                         "human_gold": False, "weight_training_performed": False}
        self.write_json(self.run_path / "protocol.json", self.protocol)
        self.model_relations = ("direct", "incidental", "unclear", "direct", None, "incidental")
        self.results = [self.make_result(row, relation) for row, relation in zip(self.rows, self.model_relations)]
        self.write_results()
        self.refs_a = [self.make_reference(row, label, "reviewer-a") for row, label in zip(
            self.rows, ("include", "exclude", "include", "exclude", "review", "include"))]
        self.refs_b = [self.make_reference(row, label, "reviewer-b") for row, label in zip(
            self.rows, ("include", "exclude", "exclude", "exclude", "review", "include"))]
        self.save_references()

    def cleanup_workspace(self):
        resolved = self.root.resolve()
        if resolved.parent != self.test_parent or not resolved.name.startswith("test_kg_evaluator_"):
            raise ValueError("Test cleanup escaped its artifact directory")
        shutil.rmtree(resolved)

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf8")

    @staticmethod
    def write_rows(path, rows):
        path.write_text("".join(preview.canonical(row) + "\n" for row in rows), encoding="utf8")

    def stage(self, row, relation):
        raw = "{" if relation is None else json.dumps({"relation": relation,
                                                       "evidence_ids": [] if relation == "unclear" else [2]})
        prediction, errors = policy.parse(raw, row)
        return {"raw_output": raw, "prediction": prediction, "errors": errors,
                "input_tokens": 20, "output_tokens": 10, "generation_complete": True,
                "elapsed_seconds": 0.0, "peak_cuda_allocated_bytes": 0}

    def make_result(self, row, relation):
        first = self.stage(row, relation)
        second = self.stage(row, relation) if runner.needs_second(first) else None
        decision, derived_relation, evidence, reason = policy.combine(first, second)
        selection = {"article_id": row["article_id"], "ticker": row["ticker"],
                     "input_sha256": preview.canonical_input_sha256(row),
                     "decision": decision, "relation": derived_relation, "evidence": evidence,
                     "reason": reason, "policy_version": policy.VERSION,
                     "model_provenance": {"type": "local_llm_inference",
                                          "model_repo": self.protocol["model_repo"],
                                          "model_revision": self.protocol["model_revision"],
                                          "protocol_sha256": preview.file_sha256(self.run_path / "protocol.json")}}
        return {"selection": selection, "stages": {"screen": first, "verify": second},
                "finished_at": "2026-01-01T00:00:00+00:00"}

    def make_reference(self, row, decision, identity):
        return {"article_id": row["article_id"], "ticker": row["ticker"],
                "input_sha256": preview.canonical_input_sha256(row), "decision": decision,
                "relation": {"include": "direct", "exclude": "incidental", "review": "unclear"}[decision],
                "evidence": [] if decision == "review" else [{"source": "body", "quote": row["body"]}],
                "reason": "Synthetic fixture only; does not assert semantic correctness.",
                "policy_version": policy.VERSION,
                "model_provenance": {"type": "independent_ai_reference", "reviewer_id": identity,
                                     "model_outputs_seen": False, "other_review_seen": False,
                                     "full_article_text_read": True, "human_gold": False,
                                     "title_chars": len(row["title"]), "body_chars": len(row["body"])}}

    def write_results(self):
        self.write_rows(self.run_path / "results.jsonl", self.results)
        self.write_rows(self.run_path / "selections.jsonl", [result["selection"] for result in self.results])
        self.refresh_summary()

    def refresh_summary(self):
        self.complete = {"complete": True, "pairs": len(self.results),
                         "counts": {"include": 2, "exclude": 2, "review": 2},
                         "invalid_pairs": 1, "stage_count": 10,
                         "human_gold": False, "weight_training_performed": False,
                         "service_published": False,
                         "files": {name: preview.file_sha256(self.run_path / name) for name in
                                   ("protocol.json", "results.jsonl", "selections.jsonl")}}
        self.write_json(self.run_path / "summary.json", self.complete)

    def save_references(self):
        self.write_rows(self.a_path, self.refs_a)
        self.write_rows(self.b_path, self.refs_b)

    def evaluate(self):
        with redirect_stdout(io.StringIO()):
            return evaluation.evaluate(self.inputs_path, self.run_path, self.a_path, self.b_path, self.out)

    def assert_rejected(self):
        with self.assertRaises(ValueError):
            self.evaluate()
        self.assertFalse(self.out.exists(), "Invalid evidence must not publish an evaluation")

    def test_all_pairs_and_disputed_references_retained_without_accuracy_claim(self):
        result = self.evaluate()
        self.assertEqual(result["input_pairs"], 6)
        self.assertEqual(result["model_counts"], {"include": 2, "exclude": 2, "review": 2})
        self.assertEqual(result["reference_consensus_counts"], {"include": 2, "exclude": 2, "disputed": 1, "review": 1})
        self.assertEqual(sum(item["count"] for item in result["joint_counts"]), 6)
        self.assertEqual(result["model_include_reference_breakdown"], {"include": 1, "exclude": 1})
        self.assertEqual(result["agreement_with_a"], 3)
        self.assertEqual(result["agreement_with_b"], 3)
        self.assertEqual(result["all_include_baseline_false_links_against_agreed_exclude"], 2)
        self.assertEqual(result["model_false_links_against_agreed_exclude"], 1)
        self.assertEqual(result["model_missed_links_against_agreed_include"], 1)
        self.assertEqual(result["format_or_length_failure_pairs"], 1)
        self.assertEqual(result["metrics_are"], "AI_reference_agreement_not_accuracy")
        self.assertIs(result["human_gold"], False)
        self.assertIs(result["production_approved"], False)
        paired = preview.read_jsonl(self.out / "paired.jsonl")
        self.assertEqual(len(paired), 6)
        self.assertEqual(sum(row["reference_status"] == "disputed" for row in paired), 1)

    def test_duplicate_missing_and_unknown_reference_rows_rejected(self):
        original = copy.deepcopy(self.refs_a)
        for mutation in (original + [copy.deepcopy(original[0])], original[:-1],
                         [dict(original[0], article_id="unknown")] + original[1:]):
            with self.subTest(rows=len(mutation), first=mutation[0]["article_id"]):
                self.write_rows(self.a_path, mutation)
                self.assert_rejected()

    def test_reference_provenance_and_source_hash_rejected(self):
        original = copy.deepcopy(self.refs_a)
        for field, value in (("model_outputs_seen", True), ("other_review_seen", True),
                             ("human_gold", True), ("full_article_text_read", False),
                             ("title_chars", 999), ("body_chars", 999)):
            with self.subTest(field=field):
                self.refs_a = copy.deepcopy(original)
                self.refs_a[0]["model_provenance"][field] = value
                self.save_references()
                self.assert_rejected()
        self.refs_a = copy.deepcopy(original)
        self.refs_a[0]["input_sha256"] = "0" * 64
        self.save_references()
        self.assert_rejected()

    def test_same_reviewer_cannot_count_as_two_references(self):
        for item in self.refs_b:
            item["model_provenance"]["reviewer_id"] = "reviewer-a"
        self.save_references()
        self.assert_rejected()

    def test_input_bytes_change_rejected(self):
        self.rows[0]["body"] += " Changed."
        self.write_rows(self.inputs_path, self.rows)
        self.assert_rejected()

    def test_inference_file_bytes_change_rejected(self):
        with (self.run_path / "results.jsonl").open("a", encoding="utf8") as stream:
            stream.write("\n")
        self.assert_rejected()

    def test_invalid_raw_output_cannot_keep_a_valid_stored_prediction(self):
        self.results[0]["stages"]["screen"]["raw_output"] = "{"
        self.write_results()  # Valid new file hashes cannot bypass raw parsing.
        self.assert_rejected()

    def test_result_coverage_must_match_every_input(self):
        self.results[-1] = copy.deepcopy(self.results[0])
        self.write_results()
        self.assert_rejected()

    def test_missing_raw_result_cannot_be_hidden_by_complete_selection_list(self):
        self.write_rows(self.run_path / "results.jsonl", self.results[:-1])
        self.refresh_summary()
        self.assert_rejected()

    def test_raw_results_and_published_selection_must_match(self):
        selections = [copy.deepcopy(result["selection"]) for result in self.results]
        selections[0]["reason"] = "Changed separately from the raw results."
        self.write_rows(self.run_path / "selections.jsonl", selections)
        self.refresh_summary()
        self.assert_rejected()

    def test_required_run_hashes_cannot_be_omitted(self):
        for omitted in ("protocol.json", "results.jsonl", "selections.jsonl"):
            with self.subTest(omitted=omitted):
                self.refresh_summary()
                self.complete["files"].pop(omitted)
                self.write_json(self.run_path / "summary.json", self.complete)
                self.assert_rejected()

    def test_failure_count_must_match_validated_raw_stages(self):
        self.complete["invalid_pairs"] = 0
        self.write_json(self.run_path / "summary.json", self.complete)
        self.assert_rejected()

    def test_protocol_pair_count_must_match_inputs(self):
        self.protocol["pairs"] = 5
        self.write_json(self.run_path / "protocol.json", self.protocol)
        # Pin the changed protocol so provenance hash alone cannot catch the count.
        for result in self.results:
            result["selection"]["model_provenance"]["protocol_sha256"] = preview.file_sha256(self.run_path / "protocol.json")
        self.write_results()
        self.assert_rejected()

    def test_unrelated_reference_policy_cannot_be_used_as_same_task(self):
        for item in self.refs_a:
            item["policy_version"] = "future_price_training_eligibility"
        self.save_references()
        self.assert_rejected()

    def test_model_provenance_must_match_run_protocol(self):
        for result in self.results:
            result["selection"]["model_provenance"]["protocol_sha256"] = "0" * 64
        self.write_results()
        self.assert_rejected()

    def test_completed_evaluation_cannot_be_overwritten(self):
        self.evaluate()
        digest = preview.file_sha256(self.out / "summary.json")
        with self.assertRaises(ValueError):
            self.evaluate()
        self.assertEqual(digest, preview.file_sha256(self.out / "summary.json"))


if __name__ == "__main__":
    unittest.main()
