"""CPU-only follow-up contracts; all articles/references are fictional fixtures.

No model/GPU loading, real pilot inputs, or held-out reference files are used.
Temporary directories are ordinary workspace mkdirs for Windows compatibility.
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
import compare_relevance_followup as comparison
import local_news_relevance as base
import run_relevance_followup as followup


class RelevanceFollowupTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".followup-test-" + uuid.uuid4().hex)
        self.root.mkdir()

    def tearDown(self):
        if self.root.resolve().parent != self.parent or not self.root.name.startswith(".followup-test-"):
            raise RuntimeError("Test cleanup escaped its workspace directory")
        shutil.rmtree(self.root)

    def article(self, suffix="one", kind="real"):
        # 'real' here tests production provenance branches using invented text.
        return {"article_id": "fiction-" + suffix, "ticker": "FICTION-001", "company_name": "가상기계",
                "title": "가상기계의 신규 공급 계약 발표", "body": "오늘 가상기계가 부품 공급 계약을 발표했다.",
                "published_at": "2021-05-20", "company_context": None,
                "case_kind": kind, "source_split": "train" if kind == "real" else "diagnostic"}

    def raw(self, policy, decision="채택"):
        if policy == "baseline":
            value = {"label": {"채택": "직접관련", "제외": "단순언급", "보류": "판단보류"}[decision],
                     "event_summary": "가상기계 관련 정보를 검토했다.", "evidence_ids": [2] if decision != "보류" else [],
                     "economic_path": "원문에 공급 계약이 명시됐다." if decision == "채택" else "",
                     "time_status": "신규" if decision == "채택" else "불명", "selection": decision}
        else:
            value = {"role": {"채택": "current_actor", "제외": "incidental", "보류": "unclear"}[decision],
                     "newness": "new" if decision == "채택" else "unclear",
                     "economic_channel": "supply" if decision == "채택" else "none",
                     "attribution_evidence_ids": [2] if decision != "보류" else [],
                     "time_evidence_ids": [2] if decision == "채택" else [],
                     "economic_evidence_ids": [2] if decision == "채택" else [],
                     "event_summary": "가상기계 관련 정보를 검토했다."}
        return json.dumps(value, ensure_ascii=False)

    def result(self, row, policy, decision="채택", raw=None):
        raw = self.raw(policy, decision) if raw is None else raw
        _, validate, module = followup.engine(policy)
        prediction, errors = validate(raw, row)
        return {"article_id": row["article_id"], "ticker": row["ticker"],
                "case_kind": row["case_kind"], "source_split": row["source_split"],
                "input_sha256": hashlib.sha256(base.canonical(row).encode()).hexdigest(),
                "policy": policy, "prediction": prediction,
                "fact_audit": module.parse_audit(raw, row)[0] if module else None,
                "raw_output": raw, "validation_errors": errors,
                "effective_selection": "보류" if errors else prediction["selection"],
                "input_tokens": 100, "output_tokens": 50, "generation_complete": True,
                "elapsed_seconds": 1.25, "peak_cuda_allocated_bytes": 1024, "finished_at": "2026-09-16T00:00:00+00:00"}

    def reference(self, row, decision="채택"):
        return {"article_id": row["article_id"], "ticker": row["ticker"], "human_gold": False,
                "reference_type": "independent_ai_review" if row["case_kind"] == "real" else "precommitted_synthetic_diagnostic",
                "expected_selection": decision, "expected_label": "직접관련", "expected_time_status": "신규",
                "evidence_sentences": [row["body"]], "reason": "Fictional fixture, not a measured real-news judgment."}

    def write(self, path, rows):
        path.write_text("".join(base.canonical(row) + "\n" for row in rows), encoding="utf8")

    def protocol(self, policy, inputs, count):
        return {"schema": "relevance-followup-1", "policy": policy,
                "input_sha256": base.sha(inputs), "rows": count, "code_hashes": followup.code_hashes(policy),
                "model_repo": "fiction/model", "model_revision": "fiction-revision", "receipt_sha256": "fiction-receipt",
                "prompt_sha256": "fiction-prompt", "temperature": 0.7, "top_p": 0.8, "top_k": 20,
                "max_input_tokens": 6144, "max_new_tokens": 768, "attn_implementation": "sdpa_explicit_kv",
                "do_sample": True, "seed_policy": "fiction-fixed-seed", "quantization": "fiction-nf4",
                "truncation": False, "versions": {"torch": "fixture-version"}, "gpu": "fixture-device"}

    def fixture(self, articles=None, baseline=None, grounded=None, references=None, suffix="case"):
        root = self.root / suffix
        root.mkdir()
        articles = [self.article()] if articles is None else articles
        input_path, ref_path = root / "inputs.jsonl", root / "references.jsonl"
        self.write(input_path, articles)
        self.write(ref_path, [self.reference(row) for row in articles] if references is None else references)
        paths = {}
        for policy, supplied in (("baseline", baseline), ("grounded", grounded)):
            directory = root / policy
            directory.mkdir()
            paths[policy] = directory / "results.jsonl"
            self.write(paths[policy], [self.result(row, policy) for row in articles] if supplied is None else supplied)
            base.write_json(directory / "protocol.json", self.protocol(policy, input_path, len(articles)))
        return SimpleNamespace(inputs=input_path, reference=[ref_path], baseline=paths["baseline"], grounded=paths["grounded"],
                               out=root / "comparison", cohort="development")

    def run_compare(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            comparison.compare(args)
        return json.loads((args.out / "summary.json").read_text(encoding="utf8"))

    def freeze_fresh(self, args, *, update_references=True):
        args.cohort = "fresh"
        args.freeze = args.inputs.parent / "fresh_reference_freeze.json"
        articles = {base.pair_key(row): row for row in base.read_rows(args.inputs)}
        if update_references:
            for path in args.reference:
                refs = base.read_rows(path)
                for ref in refs:
                    row = articles[base.pair_key(ref)]
                    ref.update(input_sha256=hashlib.sha256(base.canonical(row).encode("utf8")).hexdigest(),
                               input_file_sha256=base.sha(args.inputs), model_outputs_seen=False, full_article_text_read=True,
                               reviewed_body_chars=len(row["body"]), reviewed_title_chars=len(row["title"]),
                               body_sha256=hashlib.sha256(row["body"].encode("utf8")).hexdigest())
                self.write(path, refs)
        policy = args.freeze.with_name("experiment_freeze.json")
        base.write_json(policy, {"policy_fixed_before_new_article_content_and_reference_inspection": True,
            "fresh_predictions_inspected": False, "fresh_reference_decisions_inspected": False,
            "fresh_article_content_inspected_by_policy_authors": False,
            "code_hashes": followup.code_hashes("grounded"), "sources": {str(args.inputs.resolve()): base.sha(args.inputs)}})
        base.write_json(args.freeze, {"pairs": len(articles), "human_gold": False,
            "model_outputs_seen_by_reviewers": False, "new_model_inference_not_started": True,
            "input_reference_hashes_and_quotes_verified": True,
            "files": {str(path.resolve()): base.sha(path) for path in [args.inputs, *args.reference]},
            "policy_freeze_sha256": base.sha(policy)})

    def test_engines_share_full_whitelisted_article_but_different_wire_contracts(self):
        row = self.article()
        row["body"] += "\n" + "생략하면 안 되는 가상 본문 " * 1000
        row.update(expected_selection="REFERENCE_SECRET", future_return="RETURN_SECRET")
        prompt = "원문 경제 관련성 기준을 유지한다."
        payloads = []
        for policy in ("baseline", "grounded"):
            make_messages, _, _ = followup.engine(policy)
            messages = make_messages(row, prompt)
            self.assertTrue(messages[0]["content"].startswith(prompt))
            self.assertNotIn("SECRET", messages[1]["content"])
            payload = json.loads(messages[1]["content"])
            for field in ("title", "body"):
                self.assertEqual("".join(x["text"] for x in payload["evidence_catalog"] if x["source"] == field), row[field])
            payloads.append(payload)
        self.assertEqual(payloads[0], payloads[1])
        baseline_messages = followup.engine("baseline")[0](row, prompt)
        grounded_messages = followup.engine("grounded")[0](row, prompt)
        self.assertNotEqual(baseline_messages[0], grounded_messages[0])

    def test_each_policy_revalidates_raw_output_and_fact_audit(self):
        row = self.article()
        for policy in ("baseline", "grounded"):
            protocol = {"policy": policy, "max_input_tokens": 6144, "max_new_tokens": 768}
            result = self.result(row, policy)
            followup.validate_result(result, row, protocol)
            changes = [{"raw_output": "{}"}, {"effective_selection": "제외"}, {"validation_errors": ["fake"]},
                       {"prediction": {}}, {"fact_audit": {"fabricated": True}}, {"policy": "other"}]
            for changed in changes:
                with self.subTest(policy=policy, changed=changed), self.assertRaises(ValueError):
                    followup.validate_result({**result, **changed}, row, protocol)

    def test_wrong_policy_wire_format_fails_closed_and_cannot_be_cached_as_adopted(self):
        row = self.article()
        for policy, other in (("baseline", "grounded"), ("grounded", "baseline")):
            result = self.result(row, policy, raw=self.raw(other))
            protocol = {"policy": policy, "max_input_tokens": 6144, "max_new_tokens": 768}
            self.assertIn("invalid_schema_keys", result["validation_errors"])
            self.assertEqual(result["effective_selection"], "보류")
            followup.validate_result(result, row, protocol)
            with self.assertRaises(ValueError):
                followup.validate_result({**result, "effective_selection": "채택"}, row, protocol)

    def test_changed_body_pair_or_provenance_cannot_reuse_result(self):
        row = self.article()
        for policy in ("baseline", "grounded"):
            result = self.result(row, policy)
            protocol = {"policy": policy, "max_input_tokens": 6144, "max_new_tokens": 768}
            with self.assertRaises(ValueError):
                followup.validate_result(result, {**row, "body": row["body"] + " 새 정보."}, protocol)
            for changes in ({"ticker": "OTHER"}, {"article_id": "OTHER"}, {"case_kind": "synthetic"},
                            {"source_split": "test"}, {"input_sha256": "0" * 64}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    followup.validate_result({**result, **changes}, row, protocol)

    def test_policy_code_hash_and_input_file_hash_changes_rejected(self):
        args = self.fixture()
        inputs = comparison.indexed([args.inputs])
        for policy, path in (("baseline", args.baseline), ("grounded", args.grounded)):
            protocol_path = path.with_name("protocol.json")
            original = json.loads(protocol_path.read_text(encoding="utf8"))
            for field in original["code_hashes"]:
                changed = copy.deepcopy(original); changed["code_hashes"][field] = "altered"
                base.write_json(protocol_path, changed)
                with self.subTest(policy=policy, field=field), self.assertRaises(ValueError):
                    comparison.load_results(path, inputs, args.inputs)
            base.write_json(protocol_path, original)
        with args.inputs.open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaises(ValueError):
            comparison.load_results(args.baseline, inputs, args.inputs)

    def test_missing_extra_and_duplicate_result_pairs_rejected(self):
        args = self.fixture([self.article("one"), self.article("two")])
        inputs = comparison.indexed([args.inputs])
        original = base.read_rows(args.grounded)
        for changed in (original[:1], original + [self.result(self.article("extra"), "grounded")], original + original[:1]):
            self.write(args.grounded, changed)
            with self.subTest(count=len(changed)), self.assertRaises(ValueError):
                comparison.load_results(args.grounded, inputs, args.inputs)

    def test_missing_extra_and_duplicate_reference_pairs_rejected(self):
        row = self.article()
        inputs, reference = {base.pair_key(row): row}, self.reference(row)
        for refs in ({}, {**{base.pair_key(reference): reference}, ("extra", "X"): reference}):
            with self.assertRaises(ValueError):
                comparison.validate_references(inputs, refs)
        path = self.root / "duplicate_reference.jsonl"
        self.write(path, [reference, reference])
        with self.assertRaises(ValueError):
            comparison.indexed([path])

    def test_real_reference_requires_explicit_false_gold_and_exact_evidence(self):
        row = self.article()
        inputs, ref = {base.pair_key(row): row}, self.reference(row)
        comparison.validate_references(inputs, {base.pair_key(ref): ref})
        for change in ({"human_gold": True}, {"human_gold": None}, {"human_gold": "false"},
                       {"human_gold": 0}, {"expected_selection": "up"}, {"evidence_sentences": ["원문에 없는 문장"]}):
            changed = {**ref, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                comparison.validate_references(inputs, {base.pair_key(changed): changed})
        del ref["human_gold"]
        with self.assertRaises(ValueError):
            comparison.validate_references(inputs, {base.pair_key(ref): ref})

    def test_valid_and_invalid_abstentions_remain_in_real_denominator_and_candidates_are_pending(self):
        articles = [self.article("adopt"), self.article("held"), self.article("invalid"), self.article("synthetic", "synthetic")]
        refs = [self.reference(row) for row in articles]
        baseline = [self.result(row, "baseline") for row in articles]
        grounded = [self.result(articles[0], "grounded"), self.result(articles[1], "grounded", "보류"),
                    self.result(articles[2], "grounded", raw="{"), self.result(articles[3], "grounded")]
        args = self.fixture(articles, baseline, grounded, refs)
        before = {path: path.read_bytes() for path in [args.inputs, *args.reference, args.baseline, args.grounded]}
        summary = self.run_compare(args)
        real = summary["by_kind"]["real"]["grounded"]
        self.assertEqual(real["pairs"], 3)
        self.assertEqual(real["invalid_results"], 1)
        self.assertEqual(real["reference_adopted_but_not_selected"], 2)
        self.assertEqual(real["matrix_rows_reference_columns_prediction"]["채택"], {"채택": 1, "제외": 0, "보류": 2})
        self.assertEqual(summary["by_kind"]["synthetic"]["grounded"]["pairs"], 1)
        self.assertFalse(summary["human_gold"])
        self.assertIsNone(summary["population_accuracy"])
        self.assertIsNone(summary["gnn_performance"])
        candidates = base.read_rows(args.out / "review_candidates.jsonl")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["body"], articles[0]["body"])
        for field in ("human_reviewed", "gnn_labels_generated", "service_published"):
            self.assertIs(candidates[0][field], False)
        self.assertTrue(candidates[0]["candidate_only"])
        packet = base.read_rows(args.out / "human_review_packet.jsonl")
        self.assertEqual(len(packet), 3)
        self.assertTrue(all(row["review_status"] == "pending" and row["human_selection"] is None for row in packet))
        self.assertTrue(all("ai_reference" not in row and "fact_audit" not in row for row in packet))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(set(summary["output_sha256"]), {"pair_comparison.jsonl", "review_candidates.jsonl", "human_review_packet.jsonl"})
        for name, expected in summary["output_sha256"].items():
            self.assertEqual(base.sha(args.out / name), expected)
        with self.assertRaises(FileExistsError):
            self.run_compare(args)

    def test_fresh_requires_exact_input_reference_and_experiment_freezes(self):
        args = self.fixture()
        args.cohort = "fresh"
        with self.assertRaisesRegex(ValueError, "requires --freeze"):
            self.run_compare(args)
        self.freeze_fresh(args)
        original = json.loads(args.freeze.read_text(encoding="utf8"))
        altered = copy.deepcopy(original)
        altered["policy_freeze_sha256"] = "0" * 64
        base.write_json(args.freeze, altered)
        with self.assertRaisesRegex(ValueError, "policy freeze hash differs"):
            self.run_compare(args)
        base.write_json(args.freeze, original)
        with args.reference[0].open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "input/reference file changed"):
            self.run_compare(args)
        self.freeze_fresh(args, update_references=False)
        before = {path: path.read_bytes() for path in [args.inputs, *args.reference, args.freeze,
                  args.freeze.with_name("experiment_freeze.json"), args.baseline, args.grounded]}
        summary = self.run_compare(args)
        self.assertTrue(summary["fresh_freeze_verified"])
        self.assertEqual(summary["sources"][str(args.freeze.resolve())], base.sha(args.freeze))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_fresh_per_pair_hash_and_review_blinding_attestations_fail_closed(self):
        changes = [{"input_sha256": "0" * 64}, {"input_file_sha256": "0" * 64},
                   {"model_outputs_seen": True}, {"model_outputs_seen": None},
                   {"full_article_text_read": False}, {"full_article_text_read": 1},
                   {"body_sha256": "0" * 64}, {"reviewed_body_chars": 1}]
        for index, change in enumerate(changes):
            args = self.fixture(suffix="fresh-reference-" + str(index))
            self.freeze_fresh(args)
            refs = base.read_rows(args.reference[0])
            refs[0].update(change)
            self.write(args.reference[0], refs)
            # Re-pin file bytes so the per-row checks, not just the file hash,
            # must reject incompatible provenance and reviewer declarations.
            self.freeze_fresh(args, update_references=False)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_compare(args)
            self.assertFalse(args.out.exists())

    def test_development_cannot_borrow_fresh_freeze_or_unpinned_reference_files(self):
        fresh = self.fixture([self.article("fresh")], suffix="fresh-cohort")
        self.freeze_fresh(fresh)
        development = self.fixture([self.article("old")], suffix="old-cohort")
        development.cohort, development.freeze = "fresh", fresh.freeze
        with self.assertRaisesRegex(ValueError, "exactly the requested"):
            self.run_compare(development)
        self.assertFalse(development.out.exists())
        freeze = json.loads(fresh.freeze.read_text(encoding="utf8"))
        freeze["files"][str(development.reference[0].resolve())] = base.sha(development.reference[0])
        base.write_json(fresh.freeze, freeze)
        with self.assertRaisesRegex(ValueError, "exactly the requested"):
            self.run_compare(fresh)
        self.assertFalse(fresh.out.exists())

    def test_fresh_runtime_and_input_are_bound_to_experimental_policy(self):
        for index, field in enumerate(("code_hashes", "sources", "fresh_predictions_inspected")):
            args = self.fixture(suffix="fresh-policy-" + str(index))
            self.freeze_fresh(args)
            policy_path = args.freeze.with_name("experiment_freeze.json")
            policy = json.loads(policy_path.read_text(encoding="utf8"))
            if field == "code_hashes":
                policy[field]["policy"] = "0" * 64
            elif field == "sources":
                policy[field][str(args.inputs.resolve())] = "0" * 64
            else:
                policy[field] = True
            base.write_json(policy_path, policy)
            freeze = json.loads(args.freeze.read_text(encoding="utf8"))
            freeze["policy_freeze_sha256"] = base.sha(policy_path)
            base.write_json(args.freeze, freeze)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_compare(args)
            self.assertFalse(args.out.exists())

    def test_overlength_and_incomplete_outputs_are_held(self):
        row = self.article()
        for policy in ("baseline", "grounded"):
            protocol = {"policy": policy, "max_input_tokens": 6144, "max_new_tokens": 768}
            original = self.result(row, policy)
            overlength = {**original, "input_tokens": 6145, "output_tokens": 0, "raw_output": "", "prediction": None,
                          "fact_audit": None, "generation_complete": False, "effective_selection": "보류",
                          "validation_errors": ["input_too_long_full_article_not_processed"]}
            followup.validate_result(overlength, row, protocol)
            for change in ({"raw_output": "pretend output"}, {"output_tokens": 1}):
                with self.subTest(policy=policy, change=change), self.assertRaises(ValueError):
                    followup.validate_result({**overlength, **change}, row, protocol)
            incomplete = {**original, "generation_complete": False, "effective_selection": "보류", "validation_errors": ["generation_incomplete"]}
            followup.validate_result(incomplete, row, protocol)
            with self.assertRaises(ValueError):
                followup.validate_result({**incomplete, "effective_selection": "채택"}, row, protocol)

    def test_resume_pin_rejects_missing_or_changed_original_protocol(self):
        out = self.root / "resume"; out.mkdir()
        self.write(out / "results.jsonl", [self.result(self.article(), "grounded")])
        with self.assertRaises(RuntimeError):
            base.pin_protocol(out, {"policy": "grounded"})
        self.assertFalse((out / "protocol.json").exists())
        base.write_json(out / "protocol.json", {"policy": "grounded", "input_sha256": "original"})
        with self.assertRaises(RuntimeError):
            base.pin_protocol(out, {"policy": "grounded", "input_sha256": "changed"})

    def test_original_v4_baseline_wire_contract_remains_compatible(self):
        args = self.fixture()
        rows = base.read_rows(args.baseline)
        for row in rows:
            row.pop("fact_audit"); row.pop("policy")
            row["evidence_mode"] = "sentence_ids"
        self.write(args.baseline, rows)
        protocol_path = args.baseline.with_name("protocol.json")
        protocol = json.loads(protocol_path.read_text(encoding="utf8"))
        protocol.pop("schema"); protocol.pop("policy"); protocol.pop("code_hashes")
        protocol.update(code_sha256=base.sha(base.__file__), evidence_code_sha256=base.sha(followup.evidence.__file__),
                        attention_code_sha256=base.sha(Path(base.__file__).with_name("local_relevance_attention.py")),
                        evidence_mode="sentence_ids")
        base.write_json(protocol_path, protocol)
        summary = self.run_compare(args)
        self.assertEqual(summary["by_kind"]["real"]["baseline"]["pairs"], 1)

    def test_comparison_rejects_changed_sampling_token_budget_and_model_revision(self):
        for index, (field, value) in enumerate((("temperature", 0.1), ("top_p", 0.9), ("top_k", 4),
                ("max_input_tokens", 9999), ("max_new_tokens", 999), ("model_revision", "other"), ("prompt_sha256", "other"))):
            args = self.fixture(suffix="controlled-" + str(index))
            path = args.grounded.with_name("protocol.json")
            protocol = json.loads(path.read_text(encoding="utf8")); protocol[field] = value
            base.write_json(path, protocol)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_compare(args)

    # The following boundaries expose review findings if the implementation has
    # not yet incorporated them. They intentionally assert the safe behavior.
    def test_comparison_rejects_other_uncontrolled_execution_differences(self):
        for index, (field, value) in enumerate((("attn_implementation", "eager"), ("do_sample", False),
                ("seed_policy", "other"), ("quantization", "other"), ("model_repo", "other"),
                ("receipt_sha256", "other"), ("versions", {"torch": "other"}), ("truncation", True))):
            args = self.fixture(suffix="uncontrolled-" + str(index))
            path = args.grounded.with_name("protocol.json")
            protocol = json.loads(path.read_text(encoding="utf8")); protocol[field] = value
            base.write_json(path, protocol)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_compare(args)

    def test_other_task_reference_types_or_label_enums_cannot_be_mixed(self):
        row = self.article(); inputs = {base.pair_key(row): row}
        for changes in ({"reference_type": "stock_return_direction"}, {"reference_type": "prior_ai_review"},
                        {"expected_label": "supported"}, {"expected_time_status": "positive_return"}):
            reference = {**self.reference(row), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                comparison.validate_references(inputs, {base.pair_key(reference): reference})

    def test_runtime_metric_types_and_overlength_completion_are_consistent(self):
        row = self.article()
        protocol = {"policy": "grounded", "max_input_tokens": 6144, "max_new_tokens": 768}
        result = self.result(row, "grounded")
        for changes in ({"input_tokens": -1}, {"input_tokens": True}, {"output_tokens": 1.5},
                        {"output_tokens": float("nan")}, {"elapsed_seconds": float("nan")},
                        {"elapsed_seconds": -1}, {"peak_cuda_allocated_bytes": -1}, {"generation_complete": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                followup.validate_result({**result, **changes}, row, protocol)
        overlength = {**result, "input_tokens": 6145, "output_tokens": 0, "raw_output": "", "prediction": None,
                      "fact_audit": None, "generation_complete": True, "effective_selection": "보류",
                      "validation_errors": ["input_too_long_full_article_not_processed"]}
        with self.assertRaises(ValueError):
            followup.validate_result(overlength, row, protocol)


if __name__ == "__main__":
    unittest.main()
