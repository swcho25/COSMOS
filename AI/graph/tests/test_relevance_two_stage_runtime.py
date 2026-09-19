"""CPU-only two-stage replay/comparison contracts using fictional articles.

No actual pilot inputs, references, outputs, torch, or models are loaded. Mock
tensor objects exercise EOS and overlength handling without GPU inference.
"""
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
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare_relevance_two_stage as comparison
import local_news_relevance as base
import relevance_two_stage_policy as policy
import run_relevance_followup as baseline_runner
import run_relevance_two_stage as runner


VERIFY_SEED = "sha256('20260916:verify:' + canonical([article_id,ticker])) first 8 hex"


class RelevanceTwoStageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".two-stage-runtime-test-" + uuid.uuid4().hex)
        self.root.mkdir()

    def tearDown(self):
        if self.root.resolve().parent != self.parent or not self.root.name.startswith(".two-stage-runtime-test-"):
            raise RuntimeError("Refusing cleanup outside this test's workspace directory")
        shutil.rmtree(self.root)

    @staticmethod
    def article(suffix="one"):
        # 'real' exercises real-cohort provenance branches with invented text.
        return {"article_id": "fiction-" + suffix, "ticker": "FICTION-001", "company_name": "가상기계",
                "title": "가상기계가 신규 공급 계약 발표", "body": "오늘 가상기계는 부품 공급 계약을 발표했다.",
                "published_at": "2021-05-20", "case_kind": "real", "source_split": "train", "company_context": None}

    @staticmethod
    def row_sha(row):
        return hashlib.sha256(base.canonical(row).encode("utf8")).hexdigest()

    @staticmethod
    def write(path, rows):
        path.write_text("".join(base.canonical(row) + "\n" for row in rows), encoding="utf8")

    def stage(self, row, decision="adopt", *, raw=None, complete=True, overlength=False):
        raw = base.canonical({"decision": decision, "evidence_ids": [] if decision == "hold" else [2]}) if raw is None else raw
        prediction, errors = policy.parse(raw, row)
        if not complete:
            errors.append("generation_incomplete")
        stage = {"raw_output": raw, "prediction": prediction, "validation_errors": errors,
                 "input_tokens": 100, "output_tokens": 20, "generation_complete": complete,
                 "elapsed_seconds": 1.25, "peak_cuda_allocated_bytes": 1024}
        if overlength:
            stage.update(raw_output="", prediction=None, validation_errors=["input_too_long_full_article_not_processed"],
                         input_tokens=6145, output_tokens=0, generation_complete=False)
        return stage

    def result(self, row, first="adopt", second="adopt", *, stage1=None, stage2=None):
        one = self.stage(row, first) if stage1 is None else stage1
        two = None
        if runner.requires_verification(one):
            two = self.stage(row, second) if stage2 is None else stage2
        stages = {"screen": one, "verify": two}
        value = {"article_id": row["article_id"], "ticker": row["ticker"], "case_kind": row["case_kind"],
                 "source_split": row["source_split"], "policy": "two_stage", "input_sha256": self.row_sha(row),
                 "stages": stages, "effective_selection": runner.final_selection(stages),
                 "validation_errors": runner.combined_errors(stages), "finished_at": "fictional-test-time",
                 "peak_cuda_allocated_bytes": max(s["peak_cuda_allocated_bytes"] for s in stages.values() if s)}
        for field in ("elapsed_seconds", "input_tokens", "output_tokens"):
            value[field] = sum(s[field] for s in stages.values() if s is not None)
        return value

    def baseline(self, row, selection="채택"):
        raw = base.canonical({"label": {"채택": "직접관련", "제외": "단순언급", "보류": "판단보류"}[selection],
            "event_summary": "가상기계의 신규 공급 계약이다.", "evidence_ids": [2] if selection != "보류" else [],
            "economic_path": "본문의 공급 계약." if selection == "채택" else "",
            "time_status": "신규" if selection == "채택" else "불명", "selection": selection})
        prediction, errors = baseline_runner.engine("baseline")[1](raw, row)
        return {"article_id": row["article_id"], "ticker": row["ticker"], "case_kind": row["case_kind"],
                "source_split": row["source_split"], "policy": "baseline", "input_sha256": self.row_sha(row),
                "raw_output": raw, "prediction": prediction, "validation_errors": errors, "fact_audit": None,
                "effective_selection": "보류" if errors else prediction["selection"],
                "generation_complete": True, "input_tokens": 100, "output_tokens": 20,
                "elapsed_seconds": 1.25, "peak_cuda_allocated_bytes": 1024}

    def reference(self, row, selection="채택"):
        return {"article_id": row["article_id"], "ticker": row["ticker"], "human_gold": False,
                "reference_type": "independent_ai_review", "expected_selection": selection,
                "expected_label": "직접관련" if selection == "채택" else "판단보류", "expected_time_status": "신규",
                "evidence_sentences": [row["body"]], "reason": "Invented fixture, not a real article verdict.",
                "model_outputs_seen": False, "full_article_text_read": True, "input_sha256": self.row_sha(row),
                "body_sha256": hashlib.sha256(row["body"].encode("utf8")).hexdigest(),
                "title_sha256": hashlib.sha256(row["title"].encode("utf8")).hexdigest(),
                "reviewed_body_chars": len(row["body"]), "reviewed_title_chars": len(row["title"])}

    def protocol(self, path, count, mode):
        value = {"schema": "relevance-two-stage-1" if mode == "two_stage" else "relevance-followup-1", "policy": mode,
            "input_sha256": base.sha(path), "rows": count,
            "code_hashes": runner.code_hashes() if mode == "two_stage" else baseline_runner.code_hashes("baseline"),
            "model_repo": "fiction/model", "model_revision": "fiction-revision", "receipt_sha256": "fiction-receipt",
            "prompt_sha256": base.sha(Path(base.__file__).with_name("news_relevance_prompt.txt")),
            "temperature": 0.7, "top_p": 0.8, "top_k": 20, "max_input_tokens": 6144, "max_new_tokens": 768,
            "attn_implementation": "sdpa_explicit_kv", "quantization": "fiction-nf4", "do_sample": True,
            "seed_policy": "sha256('20260916:' + canonical([article_id,ticker])) first 8 hex",
            "versions": {"torch": "fixture"}, "gpu": "fixture-no-gpu", "truncation": False}
        if mode == "two_stage":
            value.update(verification_seed_policy=VERIFY_SEED, stage2_receives_stage1_output=False, retry_policy="none")
        return value

    def fixture(self, articles=None, results=None, refs=None, suffix="case"):
        directory = self.root / suffix
        directory.mkdir()
        articles = [self.article()] if articles is None else articles
        args = SimpleNamespace(inputs=directory / "inputs.jsonl", reference=[directory / "reference.jsonl"],
            baseline=directory / "baseline" / "results.jsonl", two_stage=directory / "two_stage" / "results.jsonl",
            freeze=directory / "reference_freeze.json", out=directory / "comparison")
        self.write(args.inputs, articles)
        refs = [self.reference(row) for row in articles] if refs is None else refs
        for ref in refs:
            ref["input_file_sha256"] = base.sha(args.inputs)
        self.write(args.reference[0], refs)
        for mode, path in (("baseline", args.baseline), ("two_stage", args.two_stage)):
            path.parent.mkdir()
            rows = [self.baseline(row) for row in articles] if mode == "baseline" else (
                [self.result(row) for row in articles] if results is None else results)
            self.write(path, rows)
            base.write_json(path.with_name("protocol.json"), self.protocol(args.inputs, len(articles), mode))
        fixed = {"fixed_before_fresh_article_reference_or_output_inspection": True, "code_hashes": runner.code_hashes(),
                 "baseline_code_hashes": baseline_runner.code_hashes("baseline"),
                 "prompt_sha256": base.sha(Path(base.__file__).with_name("news_relevance_prompt.txt")),
                 "input_sha256": base.sha(args.inputs)}
        base.write_json(args.freeze.with_name("policy_freeze.json"), fixed)
        self.refreeze(args)
        return args

    def refreeze(self, args):
        base.write_json(args.freeze, {"human_gold": False, "model_outputs_seen_by_reviewers": False,
            "inference_not_started": True, "pairs": len(base.read_rows(args.inputs)),
            "files": {str(path.resolve()): base.sha(path) for path in [args.inputs, *args.reference]},
            "policy_freeze_sha256": base.sha(args.freeze.with_name("policy_freeze.json"))})

    @staticmethod
    def run_compare(args):
        with contextlib.redirect_stdout(io.StringIO()):
            comparison.compare(args)
        return json.loads((args.out / "summary.json").read_text(encoding="utf8"))

    def test_stage_replay_checks_raw_restored_quotes_errors_and_final_decision(self):
        row = self.article()
        protocol = {"max_input_tokens": 6144, "max_new_tokens": 768}
        original = self.result(row)
        runner.validate_result(original, row, protocol)
        for field, changed in (("raw_output", "{}"), ("prediction", {}), ("validation_errors", ["invented"])):
            result = copy.deepcopy(original)
            result["stages"]["verify"][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                runner.validate_result(result, row, protocol)
        changed = copy.deepcopy(original)
        changed["stages"]["verify"]["prediction"]["evidence_sentences"] = ["fabricated"]
        with self.assertRaises(ValueError):
            runner.validate_result(changed, row, protocol)
        for changes in ({"effective_selection": "제외"}, {"validation_errors": ["invented"]}, {"policy": "baseline"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runner.validate_result({**original, **changes}, row, protocol)

    def test_second_stage_required_only_after_valid_complete_adopt(self):
        row = self.article()
        protocol = {"max_input_tokens": 6144, "max_new_tokens": 768}
        for first in (self.stage(row, "exclude"), self.stage(row, "hold"), self.stage(row, raw="{"),
                      self.stage(row, complete=False), self.stage(row, overlength=True)):
            self.assertFalse(runner.requires_verification(first))
            result = self.result(row, stage1=first)
            self.assertIsNone(result["stages"]["verify"])
            runner.validate_result(result, row, protocol)
            result["stages"]["verify"] = self.stage(row)
            with self.assertRaises(ValueError):
                runner.validate_result(result, row, protocol)
        result = self.result(row)
        result["stages"]["verify"] = None
        with self.assertRaises(ValueError):
            runner.validate_result(result, row, protocol)

    def test_incomplete_second_pass_or_invalid_wire_never_becomes_adopted(self):
        row = self.article()
        protocol = {"max_input_tokens": 6144, "max_new_tokens": 768}
        for second in (self.stage(row, complete=False), self.stage(row, overlength=True),
                       self.stage(row, raw=self.baseline(row)["raw_output"])):
            result = self.result(row, stage2=second)
            self.assertEqual(result["effective_selection"], "보류")
            self.assertTrue(all(e.startswith("verify:") for e in result["validation_errors"]))
            runner.validate_result(result, row, protocol)
            with self.assertRaises(ValueError):
                runner.validate_result({**result, "effective_selection": "채택"}, row, protocol)
        for second, expected in (("adopt", "채택"), ("exclude", "제외"), ("hold", "보류")):
            result = self.result(row, second=second)
            self.assertEqual(result["effective_selection"], expected)
            runner.validate_result(result, row, protocol)

    def test_changed_article_pair_or_provenance_cannot_replay(self):
        row = self.article()
        result = self.result(row)
        protocol = {"max_input_tokens": 6144, "max_new_tokens": 768}
        for changes in ({"body": row["body"] + " 수정."}, {"title": "다른 제목"}, {"company_context": {}},
                        {"ticker": "OTHER"}, {"article_id": "other"}, {"source_split": "test"}, {"case_kind": "synthetic"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runner.validate_result(result, {**row, **changes}, protocol)

    def test_runtime_metrics_and_aggregate_totals_cannot_be_forged(self):
        row = self.article()
        protocol = {"max_input_tokens": 6144, "max_new_tokens": 768}
        result = self.result(row)
        for changes in ({"input_tokens": True}, {"input_tokens": -1}, {"output_tokens": 769}, {"output_tokens": 1.5},
                        {"elapsed_seconds": float("nan")}, {"elapsed_seconds": -1}, {"generation_complete": 1},
                        {"peak_cuda_allocated_bytes": -1}):
            stage = {**result["stages"]["screen"], **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runner.validate_stage(stage, row, protocol)
        for field in ("input_tokens", "output_tokens", "elapsed_seconds", "peak_cuda_allocated_bytes"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                runner.validate_result({**result, field: result[field] + 1}, row, protocol)
        overlength = self.stage(row, overlength=True)
        for change in ({"generation_complete": True}, {"raw_output": "pretend output"}, {"output_tokens": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.validate_stage({**overlength, **change}, row, protocol)

    def test_resume_protocol_cannot_be_recreated_or_changed_after_results(self):
        directory = self.root / "resume"
        directory.mkdir()
        self.write(directory / "results.jsonl", [self.result(self.article())])
        with self.assertRaises(RuntimeError):
            base.pin_protocol(directory, {"policy": "two_stage"})
        original = {"policy": "two_stage", "input_sha256": "original", "code_hashes": runner.code_hashes()}
        base.write_json(directory / "protocol.json", original)
        base.pin_protocol(directory, original)
        for field, value in (("input_sha256", "changed"), ("code_hashes", {}), ("policy", "baseline")):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                base.pin_protocol(directory, {**original, field: value})

    def test_comparison_retains_holds_invalids_and_lost_adoptions_in_full_denominator(self):
        rows = [self.article(str(i)) for i in range(6)]
        results = [self.result(rows[0]), self.result(rows[1], first="hold"), self.result(rows[2], second="exclude"),
                   self.result(rows[3], stage2=self.stage(rows[3], raw="{")), self.result(rows[4]),
                   self.result(rows[5], stage1=self.stage(rows[5], overlength=True))]
        refs = [self.reference(row, "보류" if i == 4 else "채택") for i, row in enumerate(rows)]
        args = self.fixture(rows, results, refs)
        source_paths = [args.inputs, *args.reference, args.baseline, args.two_stage, args.freeze,
                        args.freeze.with_name("policy_freeze.json")]
        before = {p: p.read_bytes() for p in source_paths}
        summary = self.run_compare(args)
        self.assertEqual(summary["pairs"], 6)
        first, final = summary["metrics"]["screen_only"], summary["metrics"]["two_stage"]
        self.assertEqual(first["pairs"], 6)
        self.assertEqual(first["model_adopted"], 4)
        self.assertEqual(final["pairs"], 6)
        self.assertEqual(final["invalid_results"], 2)
        self.assertEqual(final["model_adopted"], 2)
        self.assertEqual(final["reference_adopted_but_not_selected"], 4)
        self.assertEqual(final["matrix_rows_reference_columns_prediction"]["채택"], {"채택": 1, "제외": 1, "보류": 3})
        self.assertEqual(summary["second_pass_count"], 4)
        self.assertEqual(summary["second_pass_changed_adoptions"], 2)
        self.assertFalse(summary["human_gold"])
        self.assertEqual(summary["human_review_completed"], 0)
        self.assertIsNone(summary["population_accuracy"])
        self.assertIsNone(summary["gnn_performance"])
        candidates = base.read_rows(args.out / "review_candidates.jsonl")
        conflicts = base.read_rows(args.out / "disputed_adoptions.jsonl")
        self.assertEqual((len(candidates), len(conflicts)), (1, 1))
        for item in candidates + conflicts:
            self.assertTrue(item["candidate_only"])
            for field in ("human_reviewed", "gnn_labels_generated", "service_published"):
                self.assertIs(item[field], False)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        for name, digest in summary["output_sha256"].items():
            self.assertEqual(base.sha(args.out / name), digest)
        with self.assertRaises(FileExistsError):
            self.run_compare(args)

    def test_missing_extra_duplicate_results_or_references_fail_before_output(self):
        for index, (target, mutation) in enumerate((
                ("two_stage", "missing"), ("two_stage", "extra"), ("two_stage", "duplicate"),
                ("baseline", "missing"), ("reference", "missing"), ("reference", "extra"), ("reference", "duplicate"))):
            rows = [self.article("one"), self.article("two")]
            args = self.fixture(rows, suffix="pairs-" + str(index))
            path = args.reference[0] if target == "reference" else getattr(args, target)
            records = base.read_rows(path)
            if mutation == "missing":
                records.pop()
            elif mutation == "duplicate":
                records.append(records[0])
            else:
                records.append({**records[0], "article_id": "extra"})
            self.write(path, records)
            self.refreeze(args)
            with self.subTest(target=target, mutation=mutation), self.assertRaises(ValueError):
                self.run_compare(args)
            self.assertFalse(args.out.exists())

    def test_reference_task_type_gold_and_full_text_attestations_cannot_be_mixed(self):
        changes = [{"human_gold": True}, {"human_gold": 0}, {"reference_type": "stock_return_direction"},
                   {"expected_label": "supported"}, {"expected_time_status": "positive_return"},
                   {"evidence_sentences": ["not in source"]}, {"model_outputs_seen": True},
                   {"model_outputs_seen": None}, {"full_article_text_read": 1},
                   {"input_sha256": "wrong"}, {"input_file_sha256": "wrong"}]
        for index, change in enumerate(changes):
            args = self.fixture(suffix="reference-" + str(index))
            refs = base.read_rows(args.reference[0])
            refs[0].update(change)
            self.write(args.reference[0], refs)
            self.refreeze(args)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_compare(args)
            self.assertFalse(args.out.exists())

    def test_supplied_reviewed_text_hashes_and_lengths_must_match_full_input(self):
        for index, change in enumerate(({"body_sha256": "wrong"}, {"title_sha256": "wrong"},
                                        {"reviewed_body_chars": 1}, {"reviewed_title_chars": 1})):
            args = self.fixture(suffix="reviewed-text-" + str(index))
            refs = base.read_rows(args.reference[0])
            refs[0].update(change)
            self.write(args.reference[0], refs)
            self.refreeze(args)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_compare(args)
            self.assertFalse(args.out.exists())

    def test_runtime_input_policy_and_freeze_inventory_changes_rejected(self):
        for index, mutation in enumerate(("input", "reference", "policy_hash", "policy_code", "baseline_code", "prompt", "policy_input", "inventory", "seen")):
            args = self.fixture(suffix="freeze-" + str(index))
            fixed_path = args.freeze.with_name("policy_freeze.json")
            if mutation in ("input", "reference"):
                path = args.inputs if mutation == "input" else args.reference[0]
                path.write_text(path.read_text(encoding="utf8") + "\n", encoding="utf8")
            elif mutation in ("policy_code", "baseline_code", "prompt", "policy_input"):
                fixed = json.loads(fixed_path.read_text(encoding="utf8"))
                field = {"policy_code": "code_hashes", "baseline_code": "baseline_code_hashes", "prompt": "prompt_sha256", "policy_input": "input_sha256"}[mutation]
                fixed[field] = {} if field.endswith("hashes") else "changed"
                base.write_json(fixed_path, fixed)
                self.refreeze(args)
            else:
                frozen = json.loads(args.freeze.read_text(encoding="utf8"))
                if mutation == "policy_hash":
                    frozen["policy_freeze_sha256"] = "changed"
                elif mutation == "inventory":
                    frozen["files"].pop(str(args.reference[0].resolve()))
                else:
                    frozen["model_outputs_seen_by_reviewers"] = True
                base.write_json(args.freeze, frozen)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.run_compare(args)
            self.assertFalse(args.out.exists())

    def test_two_stage_protocol_schema_code_context_and_retry_contracts_pinned(self):
        for index, change in enumerate(({"schema": "other"}, {"policy": "baseline"}, {"code_hashes": {}},
                {"input_sha256": "other"}, {"rows": 2}, {"stage2_receives_stage1_output": True}, {"retry_policy": "retry"})):
            args = self.fixture(suffix="protocol-" + str(index))
            path = args.two_stage.with_name("protocol.json")
            protocol = json.loads(path.read_text(encoding="utf8"))
            protocol.update(change)
            base.write_json(path, protocol)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_compare(args)

    def test_verification_seed_policy_cannot_be_missing_or_changed(self):
        for index, value in enumerate((None, "different seed")):
            args = self.fixture(suffix="verify-seed-" + str(index))
            path = args.two_stage.with_name("protocol.json")
            protocol = json.loads(path.read_text(encoding="utf8"))
            if value is None:
                protocol.pop("verification_seed_policy")
            else:
                protocol["verification_seed_policy"] = value
            base.write_json(path, protocol)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_compare(args)
            self.assertFalse(args.out.exists())

    def test_uncontrolled_model_sampling_attention_and_budget_changes_rejected(self):
        changes = {"model_repo": "different", "model_revision": "different", "receipt_sha256": "different",
                   "prompt_sha256": "different", "temperature": 0.1, "top_p": 0.9, "top_k": 3,
                   "max_input_tokens": 6500, "max_new_tokens": 800, "attn_implementation": "eager",
                   "do_sample": False, "seed_policy": "different", "quantization": "fp16",
                   "versions": {}, "gpu": "different", "truncation": True}
        for index, (field, value) in enumerate(changes.items()):
            args = self.fixture(suffix="control-" + str(index))
            path = args.two_stage.with_name("protocol.json")
            protocol = json.loads(path.read_text(encoding="utf8"))
            protocol[field] = value
            base.write_json(path, protocol)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_compare(args)

    def fake_generation(self, row, *, raw=None, count=100, final_token=9, stage=1):
        raw = base.canonical({"decision": "adopt", "evidence_ids": [2]}) if raw is None else raw
        calls = {"generation": [], "messages": [], "encode": [], "seeds": []}

        class Input:
            shape = (1, count)
            def to(self, device):
                return self

        class Output:
            def numel(self):
                return 20
            def __getitem__(self, index):
                return SimpleNamespace(item=lambda: final_token)

        class Generated:
            def __getitem__(self, index):
                return Output()

        class Tokenizer:
            eos_token_id = 9
            def apply_chat_template(self, messages, **kwargs):
                calls["messages"].append(messages)
                return "complete fake chat"
            def __call__(self, text, **kwargs):
                calls["encode"].append(kwargs)
                return {"input_ids": Input()}
            def decode(self, output, **kwargs):
                return raw

        class Model:
            generation_config = SimpleNamespace(eos_token_id=[9, 10])
            def generate(self, **kwargs):
                calls["generation"].append(kwargs)
                return Generated()

        torch = SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: None, reset_peak_memory_stats=lambda: None,
            max_memory_allocated=lambda: 1024), manual_seed=calls["seeds"].append, inference_mode=contextlib.nullcontext)
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.generate_stage(Model(), Tokenizer(), torch, row, "Fictional prompt", stage,
                SimpleNamespace(max_input_tokens=6144, max_new_tokens=768))
        return result, calls

    def test_generation_uses_eos_to_hold_truncated_json_even_if_parse_succeeds(self):
        for final_token, expected_errors in ((9, []), (10, []), (8, ["generation_incomplete"])):
            result, calls = self.fake_generation(self.article(), final_token=final_token)
            self.assertEqual(result["validation_errors"], expected_errors)
            self.assertEqual(result["generation_complete"], not expected_errors)
            self.assertEqual(runner.requires_verification(result), not expected_errors)
            self.assertEqual(len(calls["generation"]), 1)
            runner.validate_stage(result, self.article(), {"max_input_tokens": 6144, "max_new_tokens": 768})

    def test_overlength_full_article_is_never_truncated_or_generated(self):
        row = self.article()
        row["body"] += "\n" + "본문 마지막까지 보존한다. " * 1000
        result, calls = self.fake_generation(row, count=6145)
        self.assertEqual(calls["generation"], [])
        self.assertEqual(calls["seeds"], [])
        self.assertIs(calls["encode"][0]["truncation"], False)
        self.assertFalse(result["generation_complete"])
        self.assertEqual(result["validation_errors"], ["input_too_long_full_article_not_processed"])
        catalog = json.loads(calls["messages"][0][1]["content"])["evidence_catalog"]
        self.assertEqual("".join(s["text"] for s in catalog if s["source"] == "body"), row["body"])

    def test_second_generation_receives_same_article_without_first_answer_or_secrets(self):
        row = self.article()
        row.update(stage1_output="FIRST_SECRET", expected_selection="REF_SECRET", future_return="PRICE_SECRET")
        _, first = self.fake_generation(row, stage=1)
        _, second = self.fake_generation(row, stage=2)
        messages1, messages2 = first["messages"][0], second["messages"][0]
        self.assertEqual(messages1[1], messages2[1])
        self.assertNotEqual(messages1[0], messages2[0])
        self.assertEqual([m["role"] for m in messages2], ["system", "user"])
        self.assertNotIn("SECRET", base.canonical(messages2))
        self.assertNotEqual(first["seeds"], second["seeds"])


if __name__ == "__main__":
    unittest.main()
