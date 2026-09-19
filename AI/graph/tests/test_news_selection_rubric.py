"""Generic rule boundary tests; no real article IDs, tickers or case exceptions."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import news_selection_rubric as p


def fixture():
    row = {"article_id": "fiction", "ticker": "FICTION", "company_name": "가온",
           "title": "가온의 새 계약", "body": "가온이 오늘 새 부품 공급 계약을 발표했다. 계약 규모는 10억원이다.",
           "published_at": "2021-01-01", "company_context": None, "source_url": "https://example.test/fiction",
           "source_split": "train", "case_kind": "synthetic"}
    def axis(value):
        return {"value": value, "evidence": [{"source": "body", "quote": row["body"]}]}
    review = {"schema": p.VERSION, "article_id": row["article_id"], "ticker": row["ticker"],
        "input_sha256": p.input_sha(row), "rubric_sha256": p.base.sha(p.RUBRIC), "reviewer_id": "a",
        "review_context": "fresh_independent_ai", "event_summary": "가온의 새 부품 계약 발표",
        "economic_path": "부품 공급 계약과 회사 매출의 연결이 원문에 제시됐다.", "reason": "당일 계약 발표가 있다.",
        "human_gold": False, "full_article_text_read": True, "model_outputs_seen": False, "other_review_seen": False,
        "reviewed_title_chars": len(row["title"]), "reviewed_body_chars": len(row["body"]),
        "relation": axis("direct"), "novelty": axis("new_announcement"), "economic": axis("specific_change"),
        "security_scope": axis("not_security_specific"), "claim_status": axis("asserted_fact"), "materiality": axis("described")}
    return row, review


class Rules(unittest.TestCase):
    def test_candidate_is_never_gnn_gold(self):
        row, review = fixture()
        result = p.validate(review, row)
        self.assertEqual(result["candidate_status"], "candidate")
        self.assertFalse(result["ready_for_gnn_training"])
        self.assertFalse(result["usable_as_training_gold"])
        self.assertEqual(len(result["pending_checks"]), 5)

    def test_novelty_unknown_is_not_false_exclusion(self):
        row, review = fixture()
        review["novelty"] = {"value": "unclear", "evidence": []}
        self.assertEqual(p.validate(review, row)["candidate_status"], "review")

    def test_repeated_background_is_excluded_not_new(self):
        row, review = fixture()
        review["novelty"]["value"] = "repeated_or_background"
        self.assertEqual(p.validate(review, row)["candidate_status"], "exclude")

    def test_current_development_of_old_origin_can_be_candidate(self):
        row, review = fixture()
        review["novelty"]["value"] = "new_development"
        review["claim_status"]["value"] = "reported_or_projected"
        self.assertEqual(p.validate(review, row)["candidate_status"], "candidate")

    def test_mere_mention_and_no_economic_information_exclude(self):
        row, review = fixture()
        review["relation"]["value"] = "mention"
        review["economic"]["value"] = "none"
        review["novelty"]["value"] = "unclear"
        self.assertEqual(p.validate(review, row)["candidate_status"], "exclude")

    def test_generic_comparison_is_review_not_candidate(self):
        row, review = fixture()
        review["economic"]["value"] = "generic_or_context"
        self.assertEqual(p.validate(review, row)["candidate_status"], "review")

    def test_unestablished_materiality_does_not_mean_irrelevant(self):
        row, review = fixture()
        review["materiality"] = {"value": "not_established", "evidence": []}
        self.assertEqual(p.validate(review, row)["candidate_status"], "candidate")

    def test_security_specific_mismatch_or_unknown_requires_review(self):
        row, review = fixture()
        for value in ("mismatched", "unknown"):
            review["security_scope"]["value"] = value
            self.assertEqual(p.validate(review, row)["candidate_status"], "review")
        review["security_scope"]["value"] = "matching"
        self.assertEqual(p.validate(review, row)["candidate_status"], "candidate")

    def test_asserted_values_require_quotes_and_literal_sources(self):
        row, review = fixture()
        for axis in ("relation", "novelty", "economic", "claim_status", "materiality"):
            bad = copy.deepcopy(review); bad[axis]["evidence"] = []
            with self.assertRaises(ValueError): p.validate(bad, row)
        bad = copy.deepcopy(review); bad["economic"]["evidence"][0]["quote"] = "미래에 주가가 오른다"
        with self.assertRaises(ValueError): p.validate(bad, row)

    def test_incompatible_target_event_claims_rejected(self):
        row, review = fixture(); review["relation"]["value"] = "mention"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            p.validate(review, row)

    def test_fresh_blinding_and_human_gold_attestations_enforced(self):
        row, review = fixture()
        for field, wrong in (("human_gold", True), ("full_article_text_read", False),
                             ("model_outputs_seen", True), ("other_review_seen", True),
                             ("reviewed_body_chars", True)):
            bad = dict(review, **{field: wrong})
            with self.assertRaises(ValueError): p.validate(bad, row)
        review.update(review_context="development_relabel", model_outputs_seen=True, other_review_seen=True)
        self.assertEqual(p.validate(review, row)["candidate_status"], "candidate")
        with self.assertRaises(ValueError): p.validate(review, row, context="fresh_independent_ai")

    def test_input_and_rubric_hashes_pin_exact_originals(self):
        row, review = fixture()
        with self.assertRaises(ValueError): p.validate(review, dict(row, body=row["body"] + " "))
        with self.assertRaises(ValueError): p.validate(dict(review, rubric_sha256="bad"), row)

    def test_context_and_nonstring_identity_cannot_enter_article_only_contract(self):
        row, review = fixture()
        with self.assertRaises(ValueError): p.validate(dict(review, article_id=1), row)
        with self.assertRaises(ValueError):
            p.validate(review, dict(row, company_context={"business": "external facts"}))

    def test_decision_agreement_does_not_erase_axis_disagreement(self):
        row, one = fixture(); two = copy.deepcopy(one); two["reviewer_id"] = "b"
        two["materiality"] = {"value": "not_established", "evidence": []}
        records, summary = p.combine([row], [one], [two])
        self.assertEqual(summary["decision_agreement_count"], 1)
        self.assertEqual(summary["all_axes_agreement_count"], 0)
        self.assertEqual(records[0]["candidate_status"], "review")
        self.assertEqual(records[0]["agreement_status"], "disputed")

    def test_agreement_is_reference_only_and_incomplete_sets_reject(self):
        row, one = fixture(); two = dict(one, reviewer_id="b")
        records, summary = p.combine([row], [one], [two])
        self.assertEqual(records[0]["agreement_status"], "agreed_candidate")
        self.assertTrue(records[0]["human_review_required"])
        self.assertFalse(summary["accuracy_measured"])
        for inputs, a, b in (([row, row], [one], [two]), ([row], [one], [one]), ([row], [one], [])):
            with self.assertRaises(ValueError): p.combine(inputs, a, b)

    def test_serialized_run_and_immutable_output(self):
        row, one = fixture(); two = dict(one, reviewer_id="b")
        root = p.HERE / "artifacts" / ("rubric_test_" + uuid.uuid4().hex)
        root.mkdir()
        try:
            files = [root / name for name in ("input.jsonl", "a.jsonl", "b.jsonl")]
            for path, value in zip(files, (row, one, two)):
                path.write_text(p.base.canonical(value) + "\n", encoding="utf8")
            p.run(*files, root / "out")
            with self.assertRaises(ValueError): p.run(*files, root / "out")
            files[0].write_text('{"article_id":"a","article_id":"b"}', encoding="utf8")
            with self.assertRaises(ValueError): p.read_rows(files[0])
        finally:
            assert root.resolve().parent == (p.HERE / "artifacts").resolve()
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
