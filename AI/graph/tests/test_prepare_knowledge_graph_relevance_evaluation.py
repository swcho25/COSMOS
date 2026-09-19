"""Synthetic graph-selection sampling; no production article/reference reads."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_knowledge_graph_relevance_evaluation as wrapper
import test_prepare_relevance_rubric_evaluation as fixtures


class GraphEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RubricEvaluationTests()
        self.fixture.setUp()
        self.source_fixture = self.fixture.fixture.fixture
        f = self.source_fixture
        # Ensure three successive unseen cohorts fit without reducing quotas.
        f.source_rows.extend(f.article("extra-" + region + str(i), region)
                             for region in wrapper.parent.REGIONS for i in range(40))
        f.save_source()
        self.previous_dir = f.root / "previous-rubric"
        self.fixture.prepare(out=self.previous_dir)
        self.previous = self.previous_dir / "new_inputs.jsonl"
        self.previous_rows = list(wrapper.pilot.rows(self.previous))
        original_by_id = {row["record_id"]: row for row in f.source_rows}
        original = original_by_id[self.previous_rows[0]["article_id"]]
        original["body_sha256"] = wrapper.parent.digest(original["body"])
        for kind in ("raw", "clean", "url", "stored"):
            row = f.article("rubric-copy-" + kind)
            if kind in {"raw", "clean"}:
                row["body"] = original["body"] + ("\n관련 뉴스\n제거되는 가상 꼬리" if kind == "clean" else "")
            elif kind == "url":
                row["url"] = original["url"] + "?utm_source=tracking"
            else:
                row["body_sha256"] = original["body_sha256"]
            f.source_rows.append(row)
        f.save_source()
        self.options = {**self.fixture.options, "rubric_inputs": self.previous,
                        "expected_counts": {**self.fixture.options["expected_counts"], "rubric_reviews": 32, "known_reviews": 68}}
        self.out = f.root / "graph-evaluation"

    def tearDown(self):
        self.fixture.tearDown()

    def prepare(self, out=None, **changes):
        return wrapper.prepare_from_paths(out or self.out, **{**self.options, **changes})

    def test_new_cohort_excludes_rubric32_all_identity_forms_and_preserves_prior(self):
        protected = [Path(wrapper.rubric.__file__), Path(wrapper.prior.__file__), Path(wrapper.parent.__file__),
                     self.previous, self.previous_dir / "manifest.json", self.previous_dir / "evaluation_receipt.json"]
        before = {path: path.read_bytes() for path in protected}
        old_counts = copy.deepcopy(wrapper.rubric.EXPECTED_COUNTS)
        receipt = self.prepare()
        self.assertEqual(receipt["actual_exclusion_counts"], self.options["expected_counts"])
        self.assertTrue(receipt["prior_classifier_and_rubric_input_manifest_receipt_and_row_hashes_verified"])
        self.assertTrue(receipt["exclusion_counts_verified_before_parent_publication"])
        inputs = list(wrapper.pilot.rows(self.out / "new_inputs.jsonl"))
        self.assertEqual(len(inputs), 32)
        old_ids = {row["article_id"] for row in self.previous_rows + self.fixture.previous_rows}
        self.assertFalse({row["article_id"] for row in inputs} & old_ids)
        self.assertFalse(any(row["article_id"].startswith(("rubric-copy-", "classifier-copy-")) for row in inputs))
        self.assertEqual(list(wrapper.pilot.rows(self.out / "review_inputs_01.jsonl")), inputs[:16])
        self.assertEqual(list(wrapper.pilot.rows(self.out / "review_inputs_02.jsonl")), inputs[16:])
        self.assertEqual(receipt["regions"], {"domestic": 16, "overseas": 16})
        self.assertEqual(receipt["selection_seed"], wrapper.parent.SEED)
        self.assertTrue(all(row["company_context"] is None and 200 <= len(row["body"]) <= 6000 for row in inputs))
        for row, binding in zip(inputs, receipt["rows"]):
            self.assertEqual(wrapper.prior.row_sha(row), binding["input_sha256"])
        self.assertEqual(before, {path: path.read_bytes() for path in protected})
        self.assertEqual(old_counts, wrapper.rubric.EXPECTED_COUNTS)
        self.assertEqual(receipt, json.loads((self.out / "evaluation_receipt.json").read_text(encoding="utf8")))
        for filename, info in receipt["files"].items():
            self.assertEqual(wrapper.pilot.sha(self.out / filename), info["sha256"])
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.prepare()

    def test_rubric_input_manifest_and_canonical_row_bindings_are_checked(self):
        path = self.previous_dir / "evaluation_receipt.json"
        old_bytes = path.read_bytes()
        receipt = json.loads(old_bytes)
        receipt["rows"][0]["input_sha256"] = "0" * 64
        path.write_text(json.dumps(receipt), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "canonical row hashes"):
            self.prepare()
        path.write_bytes(old_bytes)
        with self.previous.open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_rubric_receipt_must_be_complete_and_match_earlier_inventory(self):
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

    def test_classifier_receipt_is_not_trusted_only_through_rubric_receipt(self):
        path = Path(self.options["classifier_inputs"]).with_name("evaluation_receipt.json")
        receipt = json.loads(path.read_text(encoding="utf8"))
        receipt["rows"][0]["input_sha256"] = "0" * 64
        path.write_text(json.dumps(receipt), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "classifier canonical row hashes"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_current_hydration_count_is_verified_before_publication(self):
        real_indexes = wrapper.parent.exclusion_indexes
        def indexes(*args, **kwargs):
            reviewed, holdout, info = real_indexes(*args, **kwargs)
            holdout["ids"].add("unexpected-holdout")
            return reviewed, holdout, info
        with mock.patch.object(wrapper.parent, "exclusion_indexes", side_effect=indexes), \
                mock.patch.object(wrapper.parent, "prepare", wraps=wrapper.parent.prepare) as prepare:
            with self.assertRaisesRegex(ValueError, "Unexpected holdout exclusion count before publication"):
                self.prepare()
            prepare.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_production_contract_pins_and_duplicate_sources(self):
        self.assertEqual(wrapper.EXPECTED_COUNTS, {"base_reviews": 612, "general_reviews": 32, "context_reviews": 10,
            "classifier_reviews": 32, "rubric_reviews": 32, "known_reviews": 718, "holdout": 11969})
        self.assertEqual(wrapper.DEFAULT_OUT, wrapper.ARTIFACTS / "kg_news_selection_v1_20260917_v1/evaluation_data")
        self.assertEqual(wrapper.pilot.sha(Path(wrapper.rubric.__file__)), wrapper.RUBRIC_WRAPPER_SHA256)
        with mock.patch.object(wrapper, "RUBRIC_WRAPPER_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "Pinned rubric wrapper"):
                self.prepare()
        with self.assertRaisesRegex(ValueError, "duplicate classifier/rubric"):
            self.prepare(rubric_inputs=self.options["classifier_inputs"])
        self.assertFalse(self.out.exists())

    def test_unchanged_seed_order_and_unused_labels_do_not_change_sample(self):
        self.prepare()
        expected = (self.out / "new_inputs.jsonl").read_bytes()
        f = self.source_fixture
        f.source_rows.reverse()
        for row in f.source_rows:
            row["companies"].reverse()
            row["expected_label"] = "ignored-changed-verdict"
        f.save_source()
        self.prepare(out=f.root / "repeat")
        self.assertEqual(expected, (f.root / "repeat/new_inputs.jsonl").read_bytes())

    def test_dependency_mutation_prevents_final_receipt(self):
        real_prepare = wrapper.parent.prepare
        def changed(**kwargs):
            result = real_prepare(**kwargs)
            with self.previous.open("a", encoding="utf8") as stream:
                stream.write("\n")
            return result
        with mock.patch.object(wrapper.parent, "prepare", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "Dependency changed"):
                self.prepare()
        self.assertTrue((self.out / "manifest.json").exists())
        self.assertFalse((self.out / "evaluation_receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
