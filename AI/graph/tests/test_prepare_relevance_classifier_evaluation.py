"""Synthetic exclusion checks and path-only production inventory checks."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_relevance_classifier_evaluation as wrapper
import test_prepare_relevance_followup as fixtures


class ClassifierEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FollowupPreparationTests()
        self.fixture.setUp()
        f = self.fixture
        self.inventory = f.root / "review_paths.json"
        self.inventory.write_text(json.dumps([str(f.reviews.resolve())]), encoding="utf8")
        names = wrapper.parent.company_names(f.companies)
        self.added_paths = []
        for kind, filename in (("general", "new_inputs.jsonl"), ("context", "inputs.jsonl")):
            directory = f.root / kind
            directory.mkdir()
            row = f.article("prior-" + kind)
            row["body_sha256"] = wrapper.parent.digest(row["body"])
            f.source_rows.append(row)
            value = wrapper.parent.make_input(row, wrapper.pilot.minimal_clean(row), "005930", names)[0]
            path = directory / filename
            f.write_rows(path, [value])
            path.with_name("manifest.json").write_text(json.dumps({"files": {filename: {"sha256": wrapper.pilot.sha(path)}}}), encoding="utf8")
            self.added_paths.append(path)
            for mode in ("raw", "clean", "url", "stored"):
                duplicate = f.article(kind + "-copy-" + mode)
                if mode in {"raw", "clean"}:
                    duplicate["body"] = row["body"] + ("\n관련 뉴스\n가상으로 제거되는 꼬리" if mode == "clean" else "")
                elif mode == "url":
                    duplicate["url"] = row["url"] + "?utm_source=tracking"
                else:
                    duplicate["body_sha256"] = row["body_sha256"]
                f.source_rows.append(duplicate)
        f.save_source()
        self.options = dict(source=f.source, companies=f.companies, review_paths_file=self.inventory,
            general_inputs=self.added_paths[0], context_inputs=self.added_paths[1], legacy_articles=f.legacy,
            legacy_metadata=f.metadata, fallback_source=None,
            expected_counts={"base_reviews": 2, "general_reviews": 1, "context_reviews": 1, "known_reviews": 4, "holdout": 1},
            expected_inventory_sha256=wrapper.pilot.sha(self.inventory), expected_base_paths=[f.reviews])

    def tearDown(self):
        self.fixture.tearDown()

    def prepare(self, out=None, **changes):
        options = {**self.options, **changes}
        return wrapper.prepare_from_paths(out or self.fixture.out, **options)

    def test_production_paths_cover_human100_and_previous42_without_reading_news(self):
        # Only path-list metadata, file existence and producer bytes are read.
        base, added, combined = wrapper.review_inventory(wrapper.PRIOR_STUDY / "review_paths.json",
            wrapper.PRIOR_STUDY / "general_data/new_inputs.jsonl", wrapper.PRIOR_STUDY / "context_data/inputs.jsonl",
            expected_sha256=wrapper.INVENTORY_SHA256, expected_base_paths=wrapper.BASE_REVIEW_PATHS)
        self.assertEqual((len(base), len(added), len(combined)), (10, 2, 12))
        self.assertIn((wrapper.ARTIFACTS / "relevance_two_stage_data_20260917_v1/human_inputs.jsonl").resolve(), base)
        self.assertIn((wrapper.ARTIFACTS / "relevance_policy_v2_data_20260917_v1/new_inputs.jsonl").resolve(), base)
        self.assertEqual(wrapper.EXPECTED_COUNTS, {"base_reviews": 612, "general_reviews": 32, "context_reviews": 10,
                                                 "known_reviews": 654, "holdout": 11969})
        self.assertEqual(wrapper.pilot.sha(Path(wrapper.parent.__file__)), wrapper.PARENT_SHA256)
        self.assertEqual(wrapper.parent.SEED, "local-relevance-followup-20260916-v2:")

    def test_wrapper_validates_exclusions_packets_and_receipt_without_mutating_parent(self):
        f = self.fixture
        protected = [Path(wrapper.parent.__file__), f.source, f.reviews, self.inventory, *self.added_paths]
        before = {path: path.read_bytes() for path in protected}
        receipt = self.prepare()
        self.assertEqual(receipt["actual_exclusion_counts"], self.options["expected_counts"])
        self.assertTrue(receipt["exclusion_counts_verified_before_parent_publication"])
        self.assertEqual(receipt["selection_seed"], wrapper.parent.SEED)
        inputs = list(wrapper.pilot.rows(f.out / "new_inputs.jsonl"))
        packet1 = list(wrapper.pilot.rows(f.out / "review_inputs_01.jsonl"))
        packet2 = list(wrapper.pilot.rows(f.out / "review_inputs_02.jsonl"))
        self.assertEqual((len(inputs), len(packet1), len(packet2)), (32, 16, 16))
        self.assertEqual(packet1 + packet2, inputs)
        for row in inputs:
            self.assertIsNone(row["company_context"])
            self.assertFalse(row["article_id"].startswith(("prior", "general-copy", "context-copy", "review-", "holdout-")))
            self.assertTrue(200 <= len(row["body"]) <= 6000)
            self.assertEqual(set(row), set(wrapper.pilot.INPUT_FIELDS))
        summary = json.loads((f.out / "manifest.json").read_text(encoding="utf8"))
        self.assertGreater(summary["overlap_match_counts_nonexclusive"]["known_review_stored_source_body_hash"], 0)
        self.assertGreater(summary["overlap_match_counts_nonexclusive"]["known_review_body"], 0)
        self.assertGreater(summary["overlap_match_counts_nonexclusive"]["known_review_url"], 0)
        self.assertEqual(receipt, json.loads((f.out / "evaluation_receipt.json").read_text(encoding="utf8")))
        self.assertEqual(receipt["parent_manifest_sha256"], wrapper.pilot.sha(f.out / "manifest.json"))
        for name, info in receipt["files"].items():
            self.assertEqual(info["sha256"], wrapper.pilot.sha(f.out / name))
        self.assertEqual(before, {path: path.read_bytes() for path in protected})
        self.assertNotIn("never-copy", (f.out / "new_inputs.jsonl").read_text(encoding="utf8"))
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.prepare()

    def test_exclusion_count_failure_prevents_parent_publication(self):
        expected = {**self.options["expected_counts"], "holdout": 2}
        with mock.patch.object(wrapper.parent, "prepare", wraps=wrapper.parent.prepare) as prepare:
            with self.assertRaisesRegex(ValueError, "Unexpected holdout exclusion count before publication"):
                self.prepare(expected_counts=expected)
            prepare.assert_not_called()
        self.assertFalse(self.fixture.out.exists())

    def test_inventory_pin_parent_pin_and_prior_manifest_tamper_fail_closed(self):
        with mock.patch.object(wrapper, "PARENT_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "Pinned parent"):
                self.prepare()
        with self.assertRaisesRegex(ValueError, "inventory checksum"):
            self.prepare(expected_inventory_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "coverage changed"):
            self.prepare(expected_base_paths=[])
        with self.added_paths[1].open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            self.prepare()
        self.assertFalse(self.fixture.out.exists())

    def test_missing_review_raw_body_fails_closed(self):
        f = self.fixture
        f.write_rows(f.reviews, [{"record_id": "unrecoverable-review"}, {"article": 0}])
        with self.assertRaisesRegex(ValueError, "bodies could not all be recovered"):
            self.prepare()
        self.assertFalse(f.out.exists())

    def test_same_seed_next_unseen_result_is_order_and_unused_label_invariant(self):
        f = self.fixture
        self.prepare()
        expected = (f.out / "new_inputs.jsonl").read_bytes()
        f.source_rows.reverse()
        for row in f.source_rows:
            row["companies"].reverse()
            row["expected_label"] = "changed but never used"
        f.save_source()
        second = f.root / "second-output"
        self.prepare(out=second)
        self.assertEqual(expected, (second / "new_inputs.jsonl").read_bytes())

    def test_failed_postpublication_check_does_not_publish_wrapper_completion(self):
        f = self.fixture
        real_prepare = wrapper.parent.prepare
        def prepare_then_mutate(**kwargs):
            result = real_prepare(**kwargs)
            with f.reviews.open("a", encoding="utf8") as stream:
                stream.write("\n")
            return result
        with mock.patch.object(wrapper.parent, "prepare", side_effect=prepare_then_mutate):
            with self.assertRaisesRegex(ValueError, "Dependency changed"):
                self.prepare()
        self.assertTrue((f.out / "manifest.json").exists())
        self.assertFalse((f.out / "evaluation_receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
