"""CPU contracts: separate semantic uncertainty from failed teacher executions."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_cascade_semantic_labels as labels
import knowledge_graph_batch_inference as batch
import knowledge_graph_news_preview as preview
import knowledge_graph_relevance_policy as policy


class SemanticLabelsTests(unittest.TestCase):
    def setUp(self):
        self.row = {"article_id": "synthetic-1", "ticker": "TEST", "company_name": "Target",
                    "title": "Target reports sales", "body": "Target sells parts. Other hires its former worker.",
                    "published_at": "2020-01-01", "source_url": None}
        self.protocol = {"policy_version": policy.VERSION, "model_repo": labels.MODEL_REPO,
                         "model_revision": labels.MODEL_REVISION,
                         "model_receipt_sha256": labels.MODEL_RECEIPT_SHA256,
                         "prompt_sha256": batch.prompt_sha256(), "code_hashes": batch.code_hashes(),
                         "max_input_tokens": batch.MAX_INPUT_TOKENS,
                         "max_new_tokens": batch.MAX_NEW_TOKENS}
        self.protocol_sha = hashlib.sha256((preview.canonical(self.protocol) + "\n").encode()).hexdigest()

    def stage(self, relation, ids=None, *, complete=True, raw=None):
        raw = raw if raw is not None else json.dumps({"relation": relation, "evidence_ids": [1] if ids is None else ids})
        prediction, errors = policy.parse(raw, self.row)
        if not complete:
            errors.append("generation_incomplete")
        return {"raw_output": raw, "prediction": prediction, "errors": errors,
                "input_tokens": 100, "output_tokens": 15, "generation_complete": complete}

    def result(self, first, second):
        decision, relation, evidence, reason = policy.combine(first, second)
        return {"stages": {"screen": first, "verify": second}, "selection": {
            "article_id": self.row["article_id"], "ticker": self.row["ticker"],
            "input_sha256": preview.canonical_input_sha256(self.row), "decision": decision,
            "relation": relation, "evidence": evidence, "reason": reason, "policy_version": policy.VERSION,
            "model_provenance": {"type": "local_llm_inference", "model_repo": labels.MODEL_REPO,
                                 "model_revision": labels.MODEL_REVISION, "protocol_sha256": self.protocol_sha}}}

    def derive(self, first, second):
        return labels.derive_label(self.row, self.result(first, second), self.protocol)

    def test_direct_indirect_subtype_disagreement_is_semantic_include(self):
        record = self.derive(self.stage("direct"), self.stage("indirect"))
        self.assertEqual(record["source_decision"], "review")
        self.assertEqual(record["label"], "include")
        self.assertFalse(record["old_policy_parity"])
        self.assertFalse(record["graph_materialization_ready"])
        self.assertIsNone(record["evidence"])

    def test_missing_second_incidental_is_unusable(self):
        record = self.derive(self.stage("incidental", []), None)
        self.assertIsNone(record["label"])
        self.assertFalse(record["usable_for_training"])

    def test_two_incidental_with_missing_second_quote_is_decision_only(self):
        record = self.derive(self.stage("incidental"), self.stage("incidental", []))
        self.assertEqual(record["label"], "exclude")
        self.assertFalse(record["evidence_valid"])
        self.assertFalse(record["human_gold"])

    def test_fabricated_reference_is_not_recovered(self):
        record = self.derive(self.stage("direct"), self.stage("direct", [99999]))
        self.assertIsNone(record["label"])

    def test_invalid_json_is_not_semantic_review(self):
        record = self.derive(self.stage("direct", raw="no json"), None)
        self.assertIsNone(record["label"])

    def test_incomplete_json_generation_is_unusable(self):
        record = self.derive(self.stage("direct", complete=False), None)
        self.assertIsNone(record["label"])

    def test_valid_unclear_screen_is_semantic_review(self):
        record = self.derive(self.stage("unclear", []), None)
        self.assertEqual(record["label"], "review")
        self.assertEqual(record["derivation_reason"], "semantic_unclear_screen")

    def test_valid_unclear_verify_is_semantic_review(self):
        record = self.derive(self.stage("direct"), self.stage("unclear", []))
        self.assertEqual(record["label"], "review")
        self.assertFalse(record["evidence_valid"])

    def test_include_exclude_conflict_stays_review(self):
        record = self.derive(self.stage("indirect"), self.stage("incidental"))
        self.assertEqual(record["label"], "review")

    def test_original_replay_rejects_tampered_decision(self):
        result = self.result(self.stage("direct"), self.stage("direct"))
        result["selection"]["decision"] = "review"
        with self.assertRaises(ValueError):
            labels.derive_label(self.row, result, self.protocol)

    def test_changed_input_rejected(self):
        result = self.result(self.stage("direct"), self.stage("direct"))
        row = dict(self.row, body="Changed body")
        with self.assertRaises(ValueError):
            labels.derive_label(row, result, self.protocol)

    def test_result_protocol_binding_rejected(self):
        result = self.result(self.stage("direct"), self.stage("direct"))
        result["selection"]["model_provenance"]["protocol_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            labels.derive_label(self.row, result, self.protocol)

    def test_code_and_model_pins_rejected(self):
        for key in ("model_revision", "prompt_sha256", "model_receipt_sha256"):
            protocol = copy.deepcopy(self.protocol)
            protocol[key] = "wrong"
            with self.assertRaises(ValueError):
                labels.validate_protocol(protocol)
        protocol = copy.deepcopy(self.protocol)
        protocol["code_hashes"]["knowledge_graph_relevance_policy.py"] = "wrong"
        with self.assertRaises(ValueError):
            labels.validate_protocol(protocol)

    def test_duplicates_and_coverage_rejected(self):
        result = self.result(self.stage("direct"), self.stage("direct"))
        with self.assertRaises(ValueError):
            labels.derive_labels([self.row, self.row], [result, result], self.protocol)
        with self.assertRaises(ValueError):
            labels.derive_labels([self.row], [], self.protocol)

    def test_none_is_preserved_by_index_adapter(self):
        result = self.result(self.stage("incidental", []), None)
        self.assertEqual(labels.validate_labels([self.row], [result], self.protocol), [None])


if __name__ == "__main__":
    unittest.main()
