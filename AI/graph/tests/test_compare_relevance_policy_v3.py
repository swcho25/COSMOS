"""Fictional CPU regression tests; no real articles, references or model loads."""
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
import compare_relevance_policy_v3 as compare
import local_news_relevance as base


class ComparisonV3Tests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.directory = self.parent / (".compare-v3-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.before = [{"article_id": "fiction-" + str(i), "ticker": "FICT", "company_name": "가상회사",
                        "published_at": "2021-05-20", "title": "가상회사 계약 발표", "body": "가상회사는 오늘 계약을 발표했다. 다음 문장도 모두 유지한다.",
                        "company_context": None, "case_kind": "synthetic", "source_split": "diagnostic", "source_url": "fiction://" + str(i)}
                       for i in range(5)]
        self.after = copy.deepcopy(self.before)
        self.args = SimpleNamespace(before_inputs=self.directory / "before.jsonl", after_inputs=self.directory / "after.jsonl",
                                    before_run=self.directory / "before_run", after_run=self.directory / "after_run",
                                    references=[self.directory / "refs.jsonl"], mode="general", out=self.directory / "out")
        self.reset()

    def tearDown(self):
        if self.directory.resolve().parent != self.parent or not self.directory.name.startswith(".compare-v3-"):
            raise RuntimeError("Cleanup escaped test workspace")
        shutil.rmtree(self.directory)

    @staticmethod
    def write_rows(path, rows):
        path.write_text("".join(base.canonical(row) + "\n" for row in rows), encoding="utf8")

    @staticmethod
    def digest(row):
        return hashlib.sha256(base.canonical(row).encode()).hexdigest()

    def stage(self, runner, row, choice):
        raw = "{" if choice == "invalid" else base.canonical({"decision": "adopt" if choice in ("extra", "overlength", "incomplete") else choice,
                                                               "evidence_ids": [] if choice == "hold" else [2]})
        if choice == "extra":
            raw = base.canonical({"decision": "adopt", "evidence_ids": [2], "summary": "INVENTED", "evidence_sentences": ["FORGED"]})
        prediction, errors = runner.policy.parse(raw, row)
        complete = choice not in ("incomplete", "overlength")
        if choice == "incomplete":
            errors.append(compare.COMPLETE_ERROR)
        if choice == "overlength":
            raw, prediction, errors = "", None, [compare.LENGTH_ERROR]
        result = {"raw_output": raw, "prediction": prediction, "validation_errors": errors, "generation_complete": complete,
                  "input_tokens": 6145 if choice == "overlength" else 100, "output_tokens": 0 if choice == "overlength" else 20,
                  "elapsed_seconds": 1.0, "peak_cuda_allocated_bytes": 1024}
        if runner is compare.v3:
            result["ignored_output_fields"] = runner.policy.ignored_fields(raw)
        return result

    def arm(self, folder, path, rows, runner, choices):
        folder.mkdir(exist_ok=True)
        protocol = {"schema": "relevance-two-stage-1" if runner is compare.v1 else "relevance-two-stage-policy-v3-1",
                    "policy": "two_stage", "code_hashes": runner.code_hashes(), "input_sha256": base.sha(path), "rows": len(rows),
                    "model_repo": "fiction/model", "model_revision": "a" * 40, "receipt_sha256": "b" * 64, "prompt_sha256": "c" * 64,
                    "attn_implementation": "sdpa_explicit_kv", "quantization": "NF4 double quantization bf16",
                    "seed_policy": compare.SEED, "verification_seed_policy": compare.VERIFY_SEED,
                    "stage2_receives_stage1_output": False, "retry_policy": "none", "truncation": False,
                    "max_input_tokens": 6144, "max_new_tokens": 768, "gpu": "fiction GPU",
                    "versions": {key: "fiction-version" for key in ("torch", "transformers", "bitsandbytes", "accelerate")},
                    "do_sample": runner is compare.v1, "temperature": 0.7 if runner is compare.v1 else None,
                    "top_p": 0.8 if runner is compare.v1 else None, "top_k": 20 if runner is compare.v1 else None}
        base.write_json(folder / "protocol.json", protocol)
        records = []
        for row, choice in zip(rows, choices):
            first = self.stage(runner, row, choice)
            stages = {"screen": first, "verify": self.stage(runner, row, choice) if runner.requires_verification(first) else None}
            record = {"article_id": row["article_id"], "ticker": row["ticker"], "case_kind": row["case_kind"],
                      "source_split": row["source_split"], "policy": "two_stage", "input_sha256": self.digest(row), "stages": stages,
                      "validation_errors": runner.combined_errors(stages), "effective_selection": runner.final_selection(stages),
                      "peak_cuda_allocated_bytes": 1024}
            for field in ("elapsed_seconds", "input_tokens", "output_tokens"):
                record[field] = sum(stage[field] for stage in stages.values() if stage)
            records.append(record)
        self.write_rows(folder / "results.jsonl", records)

    def reset(self):
        self.write_rows(self.args.before_inputs, self.before)
        self.write_rows(self.args.after_inputs, self.after)
        before_runner = compare.v1 if self.args.mode == "general" else compare.v3
        self.arm(self.args.before_run, self.args.before_inputs, self.before, before_runner, ("adopt", "invalid", "adopt", "hold", "overlength"))
        self.arm(self.args.after_run, self.args.after_inputs, self.after, compare.v3, ("adopt", "adopt", "exclude", "extra", "overlength"))
        ref_rows = self.before if self.args.mode == "general" else self.after
        self.refs = [{"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": self.digest(row),
                      "human_gold": False, "reference_type": "independent_ai_review", "model_outputs_seen": False,
                      "full_article_text_read": True, "expected_selection": choice, "label": "직접관련",
                      "time_status": "신규", "evidence_sentences": [row["body"]], "reason": "가상 참고판정"}
                     for row, choice in zip(ref_rows, ("채택", "채택", "제외", "보류", "채택"))]
        self.write_rows(self.args.references[0], self.refs)

    def context_mode(self):
        self.args.mode = "context"
        for row in self.after:
            row.update(company_name="가상회사 주식회사", company_context={"source": "가상 당시 공시", "as_of": "2021-05-01", "business": "기사에 언급된 계약 사업"})
        self.reset()

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return compare.compare(self.args)

    def test_general_all_rows_counts_constants_and_ignored_fields(self):
        result = self.execute()
        self.assertEqual(result["pairs"], 5)
        self.assertEqual(result["before"]["invalid_pairs"], 2)
        self.assertEqual(result["before"]["format_or_evidence_invalid_pairs"], 1)
        self.assertEqual(result["after"]["input_length_exceeded_pairs"], 1)
        self.assertEqual(result["after"]["denominator_all_pairs"], 5)
        self.assertEqual(result["after"]["adopted_reference_adopt"], 2)
        self.assertEqual(result["after"]["adopted_reference_exclude"], 0)
        self.assertEqual(result["after"]["adopted_reference_hold"], 1)
        self.assertEqual(result["after"]["missed_reference_adopt"], 1)
        self.assertEqual(result["changed_choices"], 3)
        ignored = result["after"]["ignored_output_fields"]
        self.assertEqual((ignored["pairs"], ignored["stages"], ignored["field_occurrences"]), (1, 2, 4))
        self.assertEqual(ignored["by_field"], {"evidence_sentences": 2, "summary": 2})
        self.assertEqual(result["all_constant_baselines"]["채택"]["adopted_reference_adopt"], 3)
        self.assertEqual(result["all_constant_baselines"]["보류"]["choice_agreement_count"], 1)
        self.assertFalse(result["human_gold"])
        self.assertFalse(result["policy_only_effect_identified"])
        pairs = compare.read_rows(self.args.out / "pair_comparison.jsonl")
        self.assertEqual(len(pairs), 5)
        restored = pairs[3]["after"]["stages"]["verify"]["prediction"]["evidence_sentences"]
        self.assertNotIn("FORGED", restored)
        for path, digest in result["source_sha256"].items():
            self.assertEqual(base.sha(path), digest)
        with self.assertRaises(FileExistsError):
            self.execute()

    def test_context_uses_after_reference_hash_and_only_allowed_fields(self):
        self.context_mode()
        wrong = copy.deepcopy(self.refs)
        wrong[0]["input_sha256"] = self.digest(self.before[0])
        self.write_rows(self.args.references[0], wrong)
        with self.assertRaisesRegex(ValueError, "Reference input hash"):
            self.execute()
        self.write_rows(self.args.references[0], self.refs)
        result = self.execute()
        self.assertEqual(result["reference_hash_condition"], "after_inputs")
        self.assertEqual(result["input_change_counts"]["company_context"], 5)
        self.assertEqual(result["input_change_counts"]["company_name"], 5)
        self.assertEqual(result["generation_settings"]["before"], result["generation_settings"]["after"])

    def test_context_forbids_article_changes_metadata_injection_future_and_unsourced_name(self):
        self.context_mode()
        for change in ({"body": "변조"}, {"title": "변조"}, {"published_at": "2021-05-21"},
                       {"source_url": "changed"}, {"price": 12}, {"company_context": None},
                       {"company_context": {"source": "fiction", "as_of": "2022-01-01"}}):
            rows = copy.deepcopy(self.after)
            rows[0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                compare.validate_inputs(self.before, rows, "context")
        # A registry miss may remain unchanged/null, but cannot acquire a new name.
        rows = copy.deepcopy(self.after)
        rows[0] = copy.deepcopy(self.before[0])
        _, _, counts = compare.validate_inputs(self.before, rows, "context")
        self.assertEqual(counts["after_context_present"], 4)

    def test_general_requires_full_equal_rows_null_context_and_identical_pair_order(self):
        for rows in (self.after[:-1], list(reversed(self.after)), self.after + self.after[:1]):
            with self.assertRaises(ValueError):
                compare.validate_inputs(self.before, rows, "general")
        for change in ({"company_name": "different"}, {"company_context": {}}, {"body": "changed"}, {"ticker": 1}):
            rows = copy.deepcopy(self.after)
            rows[0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                compare.validate_inputs(self.before, rows, "general")
        rows = copy.deepcopy(self.after)
        del rows[0]["company_context"]
        with self.assertRaises(ValueError):
            compare.validate_inputs(rows, rows, "general")

    def test_shared_model_runtime_caps_and_generation_changes_are_pinned(self):
        path = self.args.after_run / "protocol.json"
        original = compare.read_json(path.read_text(encoding="utf8"))
        for change in ({"model_revision": "different"}, {"receipt_sha256": "d" * 64}, {"gpu": "other"},
                       {"max_input_tokens": 6000}, {"max_new_tokens": 769}, {"versions": {**original["versions"], "torch": "other"}},
                       {"do_sample": 0}, {"temperature": 0.0}, {"top_k": 0}, {"verification_seed_policy": "different"},
                       {"stage2_receives_stage1_output": True}, {"truncation": True}, {"retry_policy": "one"},
                       {"unknown_setting": 1}, {"schema": "relevance-two-stage-1"}):
            base.write_json(path, {**original, **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.execute()
        baseline_path = self.args.before_run / "protocol.json"
        baseline = compare.read_json(baseline_path.read_text(encoding="utf8"))
        base.write_json(path, original)
        base.write_json(baseline_path, {**baseline, "temperature": 0.6})
        with self.assertRaises(ValueError):
            self.execute()

    def test_code_hash_and_input_file_hash_cannot_be_relabelled(self):
        path = self.args.after_run / "protocol.json"
        original = compare.read_json(path.read_text(encoding="utf8"))
        for change in ({"input_sha256": "0" * 64}, {"code_hashes": compare.v1.code_hashes()},
                       {"code_hashes": {**original["code_hashes"], "previous_policy": "0" * 64}}):
            base.write_json(path, {**original, **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.execute()

    def test_missing_results_raw_tampering_ignored_audit_and_skips_are_rejected(self):
        path = self.args.after_run / "results.jsonl"
        original = compare.read_rows(path)
        variants = [original[:-1], original + original[:1]]
        for field, value in (("raw_output", "{}"), ("ignored_output_fields", []), ("prediction", {})):
            rows = copy.deepcopy(original)
            rows[3]["stages"]["verify"][field] = value
            variants.append(rows)
        rows = copy.deepcopy(original)
        rows[0]["stages"]["verify"] = None
        variants.append(rows)
        rows = copy.deepcopy(original)
        rows[4]["effective_selection"] = "채택"
        variants.append(rows)
        for rows in variants:
            self.write_rows(path, rows)
            with self.assertRaises(ValueError):
                self.execute()
            self.assertFalse(self.args.out.exists())

    def test_incomplete_generation_is_separate_from_format_and_length(self):
        self.arm(self.args.after_run, self.args.after_inputs, self.after, compare.v3, ("incomplete", "adopt", "exclude", "extra", "overlength"))
        result = self.execute()["after"]
        self.assertEqual(result["generation_incomplete_pairs"], 1)
        self.assertEqual(result["format_or_evidence_invalid_pairs"], 0)
        self.assertEqual(result["input_length_exceeded_pairs"], 1)
        self.assertEqual(result["invalid_pairs"], 2)

    def test_ai_reference_provenance_exact_quotes_labels_and_coverage(self):
        for change in ({"human_gold": True}, {"model_outputs_seen": True}, {"full_article_text_read": False},
                       {"label": "positive"}, {"time_status": "과거"}, {"label": "판단보류"},
                       {"evidence_sentences": ["번역·변조한 인용"]}, {"evidence_sentences": []}):
            refs = copy.deepcopy(self.refs)
            refs[0].update(change)
            self.write_rows(self.args.references[0], refs)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.execute()
        for refs in (self.refs[:-1], self.refs + self.refs[:1]):
            self.write_rows(self.args.references[0], refs)
            with self.assertRaises(ValueError):
                self.execute()

    def test_duplicate_json_keys_and_nonfinite_values_rejected_before_replay(self):
        for text in ('{"decision":"hold","decision":"adopt"}', '{"seconds":NaN}', '{"seconds":Infinity}'):
            with self.assertRaises(ValueError):
                compare.read_json(text)
        path = self.args.after_run / "protocol.json"
        original = path.read_text(encoding="utf8")
        path.write_text(original.rstrip()[:-1] + ',"do_sample":false}', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            self.execute()

    def test_live_run_and_concurrent_reference_changes_produce_no_output(self):
        lock = self.args.after_run / "inference.lock"
        lock.write_text("fiction", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "unlocked"):
            self.execute()
        lock.unlink()
        real_measure = compare.measure
        def changing(records, refs):
            value = real_measure(records, refs)
            with self.args.references[0].open("a", encoding="utf8") as stream:
                stream.write("\n")
            return value
        with mock.patch.object(compare, "measure", side_effect=changing), self.assertRaisesRegex(ValueError, "changed while comparing"):
            self.execute()
        self.assertFalse(self.args.out.exists())


if __name__ == "__main__":
    unittest.main()
