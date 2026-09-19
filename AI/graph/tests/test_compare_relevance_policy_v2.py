"""Fictional, CPU-only comparison integrity tests; no evaluation articles read."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare_relevance_policy_v2 as compare
import local_news_relevance as base
import relevance_policy_v2 as policy
import run_relevance_policy_v2 as revised
import run_relevance_two_stage as baseline


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.directory = self.parent / (".compare-v2-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.rows = [{"article_id": "fiction-" + str(i), "ticker": "FICT", "company_name": "가상회사",
                      "published_at": "2021-05-20", "title": "가상회사 계약 발표", "body": "가상회사는 오늘 계약을 발표했다.",
                      "company_context": None, "case_kind": "synthetic", "source_split": "diagnostic"} for i in range(4)]
        self.args = SimpleNamespace(inputs=self.directory / "inputs.jsonl", baseline=self.directory / "baseline",
                                    revised=self.directory / "revised", references=[self.directory / "refs.jsonl"],
                                    out=self.directory / "comparison")
        self.write_rows(self.args.inputs, self.rows)
        self.refs = [{"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": self.digest(row),
                      "human_gold": False, "reference_type": "independent_ai_review", "model_outputs_seen": False,
                      "full_article_text_read": True, "expected_selection": choice, "expected_label": "직접관련",
                      "expected_time_status": "신규", "evidence_sentences": [row["body"]]}
                     for row, choice in zip(self.rows, ("채택", "채택", "제외", "보류"))]
        self.write_rows(self.args.references[0], self.refs)
        self.arm(self.args.baseline, baseline, "relevance-two-stage-1", ("adopt", "invalid", "adopt", "hold"))
        self.arm(self.args.revised, revised, "relevance-two-stage-policy-v2-1", ("adopt", "hold", "exclude", "adopt"))

    def tearDown(self):
        if self.directory.resolve().parent != self.parent or not self.directory.name.startswith(".compare-v2-"):
            raise RuntimeError("Cleanup escaped test workspace")
        shutil.rmtree(self.directory)

    @staticmethod
    def digest(row):
        return hashlib.sha256(base.canonical(row).encode()).hexdigest()

    @staticmethod
    def write_rows(path, rows):
        path.write_text("".join(base.canonical(row) + "\n" for row in rows), encoding="utf8")

    def stage(self, row, decision):
        raw = "{" if decision == "invalid" else base.canonical({"decision": decision, "evidence_ids": [] if decision == "hold" else [2]})
        prediction, errors = policy.parse(raw, row)
        return {"raw_output": raw, "prediction": prediction, "validation_errors": errors, "generation_complete": True,
                "input_tokens": 100, "output_tokens": 20, "elapsed_seconds": 1.0, "peak_cuda_allocated_bytes": 1024}

    def arm(self, folder, runner, schema, choices):
        folder.mkdir()
        protocol = {key: "same-fiction-setting" for key in compare.SETTINGS}
        protocol.update(schema=schema, policy="two_stage", code_hashes=runner.code_hashes(), input_sha256=base.sha(self.args.inputs),
                        rows=4, stage2_receives_stage1_output=False, retry_policy="none", truncation=False,
                        max_input_tokens=6144, max_new_tokens=768)
        base.write_json(folder / "protocol.json", protocol)
        records = []
        for row, choice in zip(self.rows, choices):
            stages = {"screen": self.stage(row, choice), "verify": self.stage(row, "adopt") if choice == "adopt" else None}
            record = {"article_id": row["article_id"], "ticker": row["ticker"], "case_kind": row["case_kind"],
                      "source_split": row["source_split"], "policy": "two_stage", "input_sha256": self.digest(row), "stages": stages,
                      "validation_errors": runner.combined_errors(stages), "effective_selection": runner.final_selection(stages),
                      "peak_cuda_allocated_bytes": 1024}
            for field in ("elapsed_seconds", "input_tokens", "output_tokens"):
                record[field] = sum(s[field] for s in stages.values() if s)
            records.append(record)
        self.write_rows(folder / "results.jsonl", records)

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            compare.compare(self.args)
        return json.loads((self.args.out / "summary.json").read_text(encoding="utf8"))

    def test_abstentions_and_invalids_stay_in_denominator_with_descriptive_counts(self):
        summary = self.execute()
        self.assertEqual(summary["pairs"], 4)
        self.assertEqual(summary["baseline"]["invalid_pairs"], 1)
        self.assertEqual(summary["baseline"]["reference_by_model"]["채택"]["보류"], 1)
        self.assertEqual(summary["baseline"]["adopted_reference_exclude"], 1)
        self.assertEqual(summary["revised"]["adopted_reference_hold"], 1)
        self.assertEqual(summary["revised"]["missed_reference_adopt"], 1)
        self.assertEqual(summary["changed_choices"], 2)
        self.assertFalse(summary["human_gold"])
        self.assertFalse(summary["population_accuracy_claim"])
        for path, digest in summary["source_sha256"].items():
            self.assertEqual(base.sha(path), digest)
        self.assertEqual(summary["code_hashes"]["revised"], revised.code_hashes())
        preserved = base.sha(self.args.out / "summary.json")
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(base.sha(self.args.out / "summary.json"), preserved)

    def test_missing_duplicate_pairs_and_tampered_raw_result_are_rejected(self):
        path = self.args.revised / "results.jsonl"
        original = base.read_rows(path)
        tampered = copy.deepcopy(original)
        tampered[0]["stages"]["verify"]["raw_output"] = "{}"
        for records in (original[:-1], original + original[:1], tampered):
            with self.subTest(records=len(records)):
                self.write_rows(path, records)
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertFalse(self.args.out.exists())

    def test_schema_dependency_and_equal_runtime_settings_are_checked(self):
        path = self.args.revised / "protocol.json"
        original = json.loads(path.read_text(encoding="utf8"))
        for change in ({"schema": "relevance-two-stage-1"}, {"code_hashes": baseline.code_hashes()},
                       {"input_sha256": "changed"}, {"gpu": "different GPU"}, {"verification_seed_policy": "changed"},
                       {"stage2_receives_stage1_output": True}, {"retry_policy": "retry"}, {"truncation": True}):
            with self.subTest(change=change):
                base.write_json(path, {**original, **change})
                with self.assertRaises(ValueError):
                    self.execute()

    def test_ai_provenance_hash_quotes_and_relevance_labels_cannot_be_forged(self):
        inputs = compare.index_unique(self.rows)
        for change in ({"human_gold": True}, {"reference_type": "human"}, {"model_outputs_seen": True},
                       {"full_article_text_read": False}, {"input_sha256": "changed"},
                       {"evidence_sentences": ["기사에 없는 문장"]}, {"evidence_sentences": []},
                       {"expected_label": "positive"}, {"expected_time_status": "future"}, {"label": "판단보류"},
                       {"title_sha256": "changed"}, {"reviewed_body_chars": 1}, {"input_file_sha256": "changed"}):
            refs = copy.deepcopy(self.refs)
            refs[0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                compare.validate_references(refs, inputs, self.args.inputs)
        refs = copy.deepcopy(self.refs)
        refs[-1]["evidence_sentences"] = []
        for ref in refs:
            ref["label"] = ref.pop("expected_label")
            ref["time_status"] = ref.pop("expected_time_status")
        compare.validate_references(refs, inputs, self.args.inputs)

    def test_reference_pair_coverage_and_input_changes_are_rejected(self):
        for records in (self.refs[:-1], self.refs + self.refs[:1]):
            self.write_rows(self.args.references[0], records)
            with self.assertRaises(ValueError):
                self.execute()
        self.write_rows(self.args.references[0], self.refs)
        self.write_rows(self.args.inputs, [{**row, "body": row["body"] + " 추가 원문."} for row in self.rows])
        with self.assertRaises(ValueError):
            self.execute()

    def test_concurrent_reference_change_is_rejected_before_output(self):
        real_measure = compare.measure
        def changing_measure(records, refs):
            value = real_measure(records, refs)
            with self.args.references[0].open("a", encoding="utf8") as stream:
                stream.write("\n")
            return value
        with mock.patch.object(compare, "measure", side_effect=changing_measure), self.assertRaisesRegex(ValueError, "changed while comparing"):
            self.execute()
        self.assertFalse(self.args.out.exists())


if __name__ == "__main__":
    unittest.main()
