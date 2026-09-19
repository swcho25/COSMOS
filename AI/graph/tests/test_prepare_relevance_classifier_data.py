"""Synthetic checks for provenance, feature leakage and grouped stratification."""
import copy
import json
from pathlib import Path
import random
import shutil
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_relevance_classifier_data as data


class ClassifierDataTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".classifier-data-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.paths = {name: self.root / (name + (".json" if name in {"summary", "input_manifest"} else ".jsonl"))
                      for name in data.DEFAULT_PATHS}
        self.inputs, self.original, self.selections = [], [], []
        for i in range(100):
            row = self.article(str(i))
            label = "채택" if i < 69 else "제외"
            ai_choice = "보류" if i < 12 else ("제외" if i == 12 else label)
            input_hash = data.digest(data.canonical(row))
            assessment = {"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": input_hash,
                          "ai_selection": ai_choice, "ai_reason": "AI_TEXT_MUST_NOT_BECOME_A_FEATURE",
                          "ai_evidence_quotes": ["AI_QUOTE_MUST_NOT_BECOME_A_FEATURE"]}
            exported = {"schema": "cosmos-assisted-selection-1", "review_mode": "ai_assisted_selection",
                        "independent_human_gold": False, "article_id": row["article_id"], "ticker": row["ticker"],
                        "input_sha256": input_hash, "ai_assessment": assessment,
                        "ai_assessment_sha256": data.digest(data.canonical(assessment)),
                        "human_selection": label, "human_selected_at": "2026-09-16T17:00:00Z"}
            selected = {name: value for name, value in exported.items() if name not in {"schema", "ai_assessment"}}
            selected.update({"title": row["title"], "company_name": row["company_name"],
                             "ai_selection": ai_choice, "ai_reason": assessment["ai_reason"],
                             "ai_evidence_quotes": assessment["ai_evidence_quotes"], "user_reason_collected": False})
            self.inputs.append(row)
            self.original.append(exported)
            self.selections.append(selected)
        self.write_rows("inputs", self.inputs)
        self.write_rows("original", self.original)
        self.write_rows("selections", self.selections)
        self.write_rows("differences", self.selections[:13])
        self.repin()
        self.out = self.root / "out"

    def tearDown(self):
        if self.root.resolve().parent != self.parent:
            raise RuntimeError("Cleanup path escaped test workspace")
        shutil.rmtree(self.root)

    @staticmethod
    def article(identity, ticker="T", body=None, url=None):
        return {"article_id": identity, "ticker": ticker, "company_name": "Synthetic Company",
                "published_at": "2021-04-01", "title": "Synthetic article " + identity,
                "body": body or ("Unique neutral article " + identity + ". ") * 12,
                "source_url": url or "https://example.invalid/" + identity,
                "company_context": None, "source_split": "train", "case_kind": "real"}

    @staticmethod
    def record(row, label):
        return {"sample_id": data.sample_id(row), "row": row, "label": label, "differs_from_ai": False}

    def write_rows(self, name, rows):
        self.paths[name].write_text("".join(data.canonical(row) + "\n" for row in rows), encoding="utf-8")

    def repin(self):
        hashes = {name: data.sha(self.paths[name]) for name in ("inputs", "original", "selections", "differences")}
        summary = {"source_inputs_sha256": hashes["inputs"], "source_export_sha256": hashes["original"],
                   "file_sha256": {"user_selections_original.jsonl": hashes["original"],
                                   "selection_manifest.jsonl": hashes["selections"],
                                   "differences_from_ai.jsonl": hashes["differences"]},
                   "review_mode": "ai_assisted_selection", "independent_human_gold": False,
                   "user_reasons_collected": False, "counts": {"채택": 69, "제외": 31, "보류": 0},
                   "differences_from_ai": 13,
                   "validation": {"human_selected": 100, "pending": 0, "independent_human_gold": False}}
        self.paths["summary"].write_text(data.canonical(summary), encoding="utf-8")
        self.paths["input_manifest"].write_text(data.canonical({"files": {"human_inputs.jsonl": {"sha256": hashes["inputs"]}}}), encoding="utf-8")
        self.hashes = {name: data.sha(path) for name, path in self.paths.items()}

    def verified_records(self):
        return data.validate_sources(self.paths, self.hashes, 100, {"채택": 69, "제외": 31})[0]

    def test_full_freeze_exact_counts_features_and_source_text(self):
        manifest = data.prepare(self.paths, self.out, expected_hashes=self.hashes)
        self.assertEqual(manifest["split_counts"], {"train": {"채택": 55, "제외": 25}, "dev": {"채택": 14, "제외": 6}})
        self.assertTrue(manifest["allocation"]["exact_target_achieved"])
        self.assertFalse(manifest["allocation"]["disagreement_used_in_split"])
        self.assertFalse(manifest["independent_human_gold"])
        self.assertFalse(manifest["hold_gold_available"])
        self.assertEqual(manifest["disagreement_count"], 13)
        by_id = {data.sample_id(row): row for row in self.inputs}
        all_features = {}
        for split, size in (("train", 80), ("dev", 20)):
            features = [json.loads(line) for line in (self.out / (split + "_inputs.jsonl")).read_text(encoding="utf-8").splitlines()]
            labels = [json.loads(line) for line in (self.out / (split + "_labels.jsonl")).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(features), size)
            self.assertEqual([row["sample_id"] for row in features], [row["sample_id"] for row in labels])
            for row in features:
                self.assertEqual(set(row), {"sample_id", "title", "body", "company_name"})
                self.assertEqual(row["body"], by_id[row["sample_id"]]["body"])
                self.assertNotIn("AI_TEXT", data.canonical(row))
                self.assertNotIn(row["sample_id"], all_features)
                all_features[row["sample_id"]] = row
            self.assertTrue(all(set(row) == {"sample_id", "label"} for row in labels))
        self.assertEqual(len(all_features), 100)
        for name, info in manifest["files"].items():
            self.assertEqual(data.sha(self.out / name), info["sha256"])
        self.assertEqual({name: data.sha(path) for name, path in self.paths.items()}, self.hashes)

    def test_split_is_input_order_and_disagreement_flag_invariant(self):
        records = self.verified_records()
        first = data.split_records(records)
        second_records = copy.deepcopy(records)
        random.Random(810).shuffle(second_records)
        for record in second_records:
            record["differs_from_ai"] = not record["differs_from_ai"]
        second = data.split_records(second_records)
        self.assertEqual(first, second)

    def test_transitive_id_url_and_normalized_body_links_stay_together(self):
        rows = [
            self.article("shared", "A", "first body", "https://example.invalid/a"),
            self.article("shared", "B", "second body", "https://example.invalid/linked"),
            self.article("third", "C", "cafe\u0301 \n text", "https://EXAMPLE.invalid/linked?utm_source=ads#part"),
            self.article("fourth", "D", "caf\u00e9 text", "https://example.invalid/other"),
            self.article("fifth", "E"), self.article("sixth", "F"),
        ]
        records = [self.record(row, "채택" if i % 2 else "제외") for i, row in enumerate(rows)]
        split, groups, info = data.split_records(records, dev_size=3)
        self.assertEqual(len({groups[record["sample_id"]] for record in records[:4]}), 1)
        self.assertEqual(len({split[record["sample_id"]] for record in records[:4]}), 1)
        self.assertEqual(info["group_count"], 3)
        self.assertFalse(info["exact_target_achieved"])
        self.assertNotEqual(info["actual_dev_rows"], 3)
        data.validate_no_leakage(records, split)
        broken = dict(split)
        broken[records[0]["sample_id"]] = "dev" if broken[records[1]["sample_id"]] == "train" else "train"
        with self.assertRaisesRegex(ValueError, "leakage"):
            data.validate_no_leakage(records, broken)

    def test_one_duplicate_component_is_not_silently_split(self):
        records = [self.record(self.article(str(i), body="identical body"), "채택" if i % 2 else "제외") for i in range(6)]
        with self.assertRaisesRegex(ValueError, "one duplicate group"):
            data.split_records(records, dev_size=2)

    def test_pinned_source_change_rejected_before_output(self):
        with self.paths["inputs"].open("a", encoding="utf-8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "Pinned source"):
            data.prepare(self.paths, self.out, expected_hashes=self.hashes)
        self.assertFalse(self.out.exists())

    def test_per_pair_hash_still_checked_when_file_hashes_match(self):
        self.selections[0]["input_sha256"] = "0" * 64
        self.write_rows("selections", self.selections)
        self.repin()
        with self.assertRaisesRegex(ValueError, "Per-pair"):
            self.verified_records()

    def test_original_human_selection_cannot_be_replaced_by_ai_choice(self):
        self.original[0]["human_selection"] = "제외"
        self.write_rows("original", self.original)
        self.repin()
        with self.assertRaisesRegex(ValueError, "human label"):
            self.verified_records()

    def test_disagreement_coverage_is_verified_without_split_bias(self):
        self.write_rows("differences", self.selections[:12])
        self.repin()
        with self.assertRaisesRegex(ValueError, "Disagreement coverage"):
            self.verified_records()

    def test_original_assessment_provenance_hash_cannot_drift(self):
        self.original[0]["ai_assessment"]["ai_reason"] = "Changed source provenance"
        self.write_rows("original", self.original)
        self.repin()
        with self.assertRaisesRegex(ValueError, "Per-pair"):
            self.verified_records()

    def test_missing_and_duplicate_pairs_rejected(self):
        self.write_rows("original", self.original[:-1])
        self.repin()
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.verified_records()
        self.write_rows("original", self.original + self.original[:1])
        self.repin()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.verified_records()

    def test_unreviewed_hold_is_not_silently_treated_as_exclusion(self):
        self.original[0]["human_selection"] = "보류"
        self.selections[0]["human_selection"] = "보류"
        self.write_rows("original", self.original)
        self.write_rows("selections", self.selections)
        self.repin()
        with self.assertRaisesRegex(ValueError, "human label"):
            self.verified_records()

    def test_existing_artifacts_are_not_overwritten(self):
        self.out.mkdir()
        marker = self.out / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            data.prepare(self.paths, self.out, expected_hashes=self.hashes)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_source_change_during_preparation_fails_before_writes(self):
        real_sha = data.sha
        with mock.patch.object(data, "sha", side_effect=lambda path: "f" * 64 if Path(path) == self.paths["inputs"] else real_sha(path)):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                data.prepare(self.paths, self.out, expected_hashes=self.hashes)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
