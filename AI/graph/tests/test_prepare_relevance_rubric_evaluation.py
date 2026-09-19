"""Synthetic next-unseen evaluation; no real corpus or reference outputs read."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_relevance_rubric_evaluation as wrapper
import test_prepare_relevance_classifier_evaluation as fixtures


class RubricEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ClassifierEvaluationTests()
        self.fixture.setUp()
        f = self.fixture.fixture
        self.previous_dir = f.root / "previous-classifier"
        self.fixture.prepare(out=self.previous_dir)
        self.previous = self.previous_dir / "new_inputs.jsonl"
        self.previous_rows = list(wrapper.pilot.rows(self.previous))
        original_by_id = {row["record_id"]: row for row in f.source_rows}
        original = original_by_id[self.previous_rows[0]["article_id"]]
        original["body_sha256"] = wrapper.parent.digest(original["body"])
        for kind in ("raw", "clean", "url", "stored"):
            row = f.article("classifier-copy-" + kind)
            if kind in {"raw", "clean"}:
                row["body"] = original["body"] + ("\n관련 뉴스\n제거되는 가상 꼬리" if kind == "clean" else "")
            elif kind == "url":
                row["url"] = original["url"] + "?utm_source=tracking"
            else:
                row["body_sha256"] = original["body_sha256"]
            f.source_rows.append(row)
        f.save_source()
        self.options = {**self.fixture.options, "classifier_inputs": self.previous,
                        "expected_counts": {**self.fixture.options["expected_counts"], "classifier_reviews": 32, "known_reviews": 36}}
        self.out = f.root / "rubric-evaluation"

    def tearDown(self):
        self.fixture.tearDown()

    def prepare(self, out=None, **changes):
        return wrapper.prepare_from_paths(out or self.out, **{**self.options, **changes})

    def test_next_sample_excludes_all_prior32_identity_kinds_and_preserves_old_code(self):
        protected = [Path(wrapper.prior.__file__), Path(wrapper.parent.__file__), self.previous,
                     self.previous_dir / "manifest.json", self.previous_dir / "evaluation_receipt.json"]
        before = {path: path.read_bytes() for path in protected}
        old_counts = copy.deepcopy(wrapper.prior.EXPECTED_COUNTS)
        receipt = self.prepare()
        self.assertEqual(receipt["actual_exclusion_counts"], self.options["expected_counts"])
        self.assertTrue(receipt["exclusion_counts_verified_before_parent_publication"])
        self.assertTrue(receipt["prior_classifier_input_manifest_receipt_and_row_hashes_verified"])
        self.assertEqual(receipt["selection_seed"], wrapper.parent.SEED)
        inputs = list(wrapper.pilot.rows(self.out / "new_inputs.jsonl"))
        self.assertEqual(len(inputs), 32)
        self.assertFalse({row["article_id"] for row in inputs} & {row["article_id"] for row in self.previous_rows})
        self.assertFalse(any(row["article_id"].startswith("classifier-copy-") for row in inputs))
        self.assertEqual(list(wrapper.pilot.rows(self.out / "review_inputs_01.jsonl")), inputs[:16])
        self.assertEqual(list(wrapper.pilot.rows(self.out / "review_inputs_02.jsonl")), inputs[16:])
        self.assertEqual(receipt["regions"], {"domestic": 16, "overseas": 16})
        self.assertTrue(all(row["company_context"] is None and 200 <= len(row["body"]) <= 6000 for row in inputs))
        for row, binding in zip(inputs, receipt["rows"]):
            self.assertEqual(wrapper.prior.row_sha(row), binding["input_sha256"])
        self.assertEqual(before, {path: path.read_bytes() for path in protected})
        self.assertEqual(old_counts, wrapper.prior.EXPECTED_COUNTS)
        self.assertEqual(receipt, json.loads((self.out / "evaluation_receipt.json").read_text(encoding="utf8")))
        for name, info in receipt["files"].items():
            self.assertEqual(wrapper.pilot.sha(self.out / name), info["sha256"])
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.prepare()

    def test_previous_input_manifest_and_row_hashes_fail_closed(self):
        receipt_path = self.previous_dir / "evaluation_receipt.json"
        old_bytes = receipt_path.read_bytes()
        receipt = json.loads(old_bytes)
        receipt["rows"][0]["input_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "canonical row hashes"):
            self.prepare()
        receipt_path.write_bytes(old_bytes)
        with self.previous.open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_prior_receipt_must_be_complete_and_have_previous_exclusion_counts(self):
        path = self.previous_dir / "evaluation_receipt.json"
        receipt = json.loads(path.read_text(encoding="utf8"))
        receipt["complete"] = False
        path.write_text(json.dumps(receipt), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "no complete receipt"):
            self.prepare()
        receipt["complete"] = True
        receipt["actual_exclusion_counts"]["known_reviews"] += 1
        path.write_text(json.dumps(receipt), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "exclusion receipt differs"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_wrong_hydrated_count_fails_before_parent_prepare(self):
        expected = {**self.options["expected_counts"], "holdout": 2}
        # The prior receipt must agree on the earlier holdout count so this
        # specifically reaches the independent current hydration count check.
        path = self.previous_dir / "evaluation_receipt.json"
        receipt = json.loads(path.read_text(encoding="utf8"))
        receipt["actual_exclusion_counts"]["holdout"] = 2
        path.write_text(json.dumps(receipt), encoding="utf8")
        with mock.patch.object(wrapper.parent, "prepare", wraps=wrapper.parent.prepare) as prepare:
            with self.assertRaisesRegex(ValueError, "Unexpected holdout exclusion count before publication"):
                self.prepare(expected_counts=expected)
            prepare.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_production_contract_and_immutable_module_pins(self):
        self.assertEqual(wrapper.EXPECTED_COUNTS, {"base_reviews": 612, "general_reviews": 32, "context_reviews": 10,
                                                  "classifier_reviews": 32, "known_reviews": 686, "holdout": 11969})
        self.assertEqual(wrapper.DEFAULT_OUT, wrapper.ARTIFACTS / "relevance_rubric_v1_20260917_v1/evaluation_data")
        self.assertEqual(wrapper.pilot.sha(Path(wrapper.prior.__file__)), wrapper.PRIOR_WRAPPER_SHA256)
        self.assertEqual(wrapper.pilot.sha(Path(wrapper.parent.__file__)), wrapper.prior.PARENT_SHA256)
        with mock.patch.object(wrapper, "PRIOR_WRAPPER_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "Pinned prior wrapper"):
                self.prepare()

    def test_source_order_and_unused_label_do_not_change_next_unseen_sample(self):
        self.prepare()
        expected = (self.out / "new_inputs.jsonl").read_bytes()
        f = self.fixture.fixture
        f.source_rows.reverse()
        for row in f.source_rows:
            row["companies"].reverse()
            row["expected_label"] = "unused-change"
        f.save_source()
        other = f.root / "rubric-repeat"
        self.prepare(out=other)
        self.assertEqual(expected, (other / "new_inputs.jsonl").read_bytes())

    def test_dependency_mutation_prevents_final_wrapper_receipt(self):
        real_prepare = wrapper.parent.prepare
        f = self.fixture.fixture
        def changed(**kwargs):
            result = real_prepare(**kwargs)
            with f.reviews.open("a", encoding="utf8") as stream:
                stream.write("\n")
            return result
        with mock.patch.object(wrapper.parent, "prepare", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "Dependency changed"):
                self.prepare()
        self.assertTrue((self.out / "manifest.json").exists())
        self.assertFalse((self.out / "evaluation_receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
