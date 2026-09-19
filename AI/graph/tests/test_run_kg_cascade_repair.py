"""CPU orchestration tests: holdout isolation, immutable resume, conservative release."""
from pathlib import Path
from contextlib import contextmanager
import json
import shutil
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_kg_cascade_repair as repair


@contextmanager
def workspace_tmp():
    root = Path(__file__).resolve().parents[1] / "artifacts/test_tmp_repair"
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("case_" + uuid.uuid4().hex)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Test temp directory escaped workspace root")
    target.mkdir()
    try:
        yield str(target)
    finally:
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Test cleanup escaped workspace root")
        shutil.rmtree(target)


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf8")


class Harness(repair.Repair):
    def __init__(self, out, dev_enabled=(False, False), cal_enabled=False):
        self.out = Path(out)
        self.plan = {"candidate_selection": "fixed", "challenge_receipt": None}
        self.plan_sha = "test-plan"
        self.calls = []
        self.dev_enabled = dict(zip(("A", "B"), dev_enabled))
        self.cal_enabled = cal_enabled
        self.last_status = None

    def stage(self, name, action):
        self.calls.append(name)
        return action()[0]

    def status(self, state, **extra):
        self.last_status = {"state": state, **extra}

    def teach(self, fold):
        return {"fold": fold}, []

    def train(self, name, *args):
        return {"candidate": name, "dev_log_loss": 0.5 if name == "A" else 0.6}, []

    def predict(self, fold, trained):
        return {"fold": fold, "candidate": trained["candidate"]}, []

    def tune(self, name, *args):
        return {"enabled_dev_direction": self.dev_enabled[name]}, []

    def confirm(self, *args):
        return {"enabled_direction": self.cal_enabled}, []

    def replay(self, *args):
        return {"measured": True}, []

    def report(self, *args):
        return {"gates": {"all_pass": True}, "release": {"full_fast_pass_allowed": False}}, []

    def quality_failure(self, stage, reason):
        self.status("QUALITY_GATE_FAILED", stage=stage, reason=reason)


class CandidateSelectionTests(unittest.TestCase):
    def test_viable_candidate_wins_before_loss(self):
        values = {"A": {"enabled_dev_direction": False, "dev_log_loss": 0.1},
                  "B": {"enabled_dev_direction": True, "dev_log_loss": 0.8}}
        self.assertEqual(repair.choose_candidate(values), "B")

    def test_lower_loss_then_a_tie_break(self):
        values = {"A": {"enabled_dev_direction": True, "dev_log_loss": 0.7},
                  "B": {"enabled_dev_direction": True, "dev_log_loss": 0.6}}
        self.assertEqual(repair.choose_candidate(values), "B")
        values["A"]["dev_log_loss"] = 0.6
        self.assertEqual(repair.choose_candidate(values), "A")

    def test_no_candidate_stays_failed(self):
        values = {name: {"enabled_dev_direction": False, "dev_log_loss": 0.5} for name in ("A", "B")}
        self.assertIsNone(repair.choose_candidate(values))

    def test_invalid_metrics_fail(self):
        with self.assertRaises(ValueError):
            repair.choose_candidate({"A": {"enabled_dev_direction": True, "dev_log_loss": float("nan")},
                                     "B": {"enabled_dev_direction": True, "dev_log_loss": 1.0}})

    def test_none_reference_keeps_full_denominator(self):
        self.assertEqual(repair.reference_labels([0, None, 1, 2]), ["include", "review", "exclude", "review"])
        with self.assertRaises(ValueError):
            repair.reference_labels([True])


class SequenceTests(unittest.TestCase):
    def test_dev_failure_never_opens_calibration_or_test(self):
        with workspace_tmp() as tmp:
            job = Harness(tmp)
            job.run()
            self.assertEqual(job.last_status["state"], "QUALITY_GATE_FAILED")
            self.assertEqual(job.last_status["stage"], "dev")
            self.assertNotIn("teacher_calibration", job.calls)
            self.assertNotIn("teacher_test", job.calls)
            self.assertEqual(job.calls[-1], "candidate_selection")

    def test_calibration_failure_never_opens_test(self):
        with workspace_tmp() as tmp:
            job = Harness(tmp, (False, True), False)
            job.run()
            self.assertEqual(job.last_status["stage"], "calibration")
            self.assertNotIn("teacher_test", job.calls)
            self.assertLess(job.calls.index("candidate_selection"), job.calls.index("teacher_calibration"))

    def test_success_sequences_replay_and_report_but_does_not_launch_bulk(self):
        with workspace_tmp() as tmp:
            job = Harness(tmp, (True, True), True)
            job.run()
            self.assertEqual(job.calls[-2:], ["fallback_replay", "report"])
            self.assertEqual(job.last_status["state"], "PILOT_COMPLETE")
            self.assertFalse(job.last_status["full_fast_pass_allowed"])
            self.assertFalse(any("bulk" in item for item in job.calls))

    def test_run_failure_records_failed_and_does_not_advance(self):
        with workspace_tmp() as tmp:
            job = Harness(tmp)
            with mock.patch.object(job, "teach", side_effect=ValueError("teacher checkpoint changed")):
                with self.assertRaisesRegex(ValueError, "checkpoint changed"):
                    job.run()
            self.assertEqual(job.last_status["state"], "FAILED")
            self.assertEqual(job.calls, ["teacher_train"])

    def test_run_interruption_records_interrupted_without_false_completion(self):
        with workspace_tmp() as tmp:
            job = Harness(tmp)
            with mock.patch.object(job, "teach", side_effect=KeyboardInterrupt("interrupted")):
                with self.assertRaises(KeyboardInterrupt):
                    job.run()
            self.assertEqual(job.last_status["state"], "INTERRUPTED")
            self.assertNotIn("report", job.calls)


class FreezeBoundaryTests(unittest.TestCase):
    def test_existing_labels_rejected_before_parent_or_holdout_access(self):
        with workspace_tmp() as tmp:
            out = Path(tmp) / "repair"
            (out / "teacher_train").mkdir(parents=True)
            with mock.patch.object(repair.original, "Pilot") as parent:
                with self.assertRaisesRegex(ValueError, "Freeze must precede"):
                    repair.freeze_plan(out, Path(tmp) / "parent")
                parent.assert_not_called()

    def test_existing_plan_is_immutable(self):
        with workspace_tmp() as tmp:
            out = Path(tmp) / "repair"
            out.mkdir()
            write(out / "experiment_plan.json", {"frozen": True})
            with mock.patch.object(repair.original, "Pilot") as parent:
                with self.assertRaises(ValueError):
                    repair.freeze_plan(out, Path(tmp) / "parent")
                parent.assert_not_called()
            self.assertEqual(repair.read(out / "experiment_plan.json"), {"frozen": True})

    def test_tampered_parent_stage_rejected_without_loading_article_contents(self):
        with workspace_tmp() as tmp:
            out = Path(tmp)
            (out / "stages").mkdir()
            source = out / "opaque_source.dat"
            source.write_bytes(b"opaque bytes for hashing only")
            write(out / "stages/sample.json", {"complete": True, "stage": "sample", "plan_sha256": "parent",
                    "files": {str(source): "wrong"}, "value": {"root": "unused"}})
            parent = mock.Mock(out=out, plan_sha="parent")
            with self.assertRaisesRegex(ValueError, "Parent source stage bytes changed"):
                repair._verified_parent_stage(parent, "sample")

    def test_challenge_cannot_assert_accuracy_or_use_changed_teacher(self):
        with workspace_tmp() as tmp:
            path = Path(tmp) / "challenge.json"
            value = {"accepted": True, "teacher_version": repair.teacher.VERSION,
                "teacher_code_hashes": repair.teacher.code_hashes(),
                "teacher_prompt_sha256": repair.teacher.digest({"criteria": repair.teacher.CRITERIA, "steps": repair.teacher.STEPS}),
                "human_gold": False, "factual_accuracy_established": False, "reference_kind": repair.CHALLENGE_REFERENCE}
            write(path, value)
            self.assertTrue(repair.challenge_binding(path)["accepted"])
            value["factual_accuracy_established"] = True
            write(path, value)
            with self.assertRaises(ValueError):
                repair.challenge_binding(path)
            value["factual_accuracy_established"] = False
            value["teacher_prompt_sha256"] = "changed"
            write(path, value)
            with self.assertRaises(ValueError):
                repair.challenge_binding(path)


class ReceiptTests(unittest.TestCase):
    def bare(self, tmp):
        job = object.__new__(repair.Repair)
        job.out = Path(tmp)
        (job.out / "stages").mkdir(exist_ok=True)
        job.plan_sha = "plan"
        job.plan = {"challenge_receipt": None, "full_source_manifest_sha256": "source"}
        job.assert_frozen = lambda: None
        job.status = lambda *args, **kwargs: None
        return job

    def test_completed_stage_reused_then_tampering_fails(self):
        with workspace_tmp() as tmp:
            job = self.bare(tmp)
            artifact = Path(tmp) / "artifact.json"
            write(artifact, {"complete": True})
            action = mock.Mock(return_value=({"value": 1}, [artifact]))
            self.assertEqual(job.stage("first", action), {"value": 1})
            self.assertEqual(job.stage("first", action), {"value": 1})
            self.assertEqual(action.call_count, 1)
            write(artifact, {"complete": False})
            with self.assertRaises(ValueError):
                job.stage("first", action)

    def test_incomplete_receipt_never_reused(self):
        with workspace_tmp() as tmp:
            job = self.bare(tmp)
            write(job.out / "stages/x.json", {"complete": False, "stage": "x", "plan_sha256": "plan", "files": {}, "value": {}})
            with self.assertRaises(ValueError):
                job.stage("x", lambda: ({}, []))

    def test_changed_code_after_action_does_not_commit_success(self):
        with workspace_tmp() as tmp:
            job = self.bare(tmp)
            job.assert_frozen = mock.Mock(side_effect=[None, ValueError("changed")])
            with self.assertRaises(ValueError):
                job.stage("x", lambda: ({}, []))
            self.assertFalse((job.out / "stages/x.json").exists())

    def test_no_challenge_never_releases_even_passed_gates(self):
        with workspace_tmp() as tmp:
            job = self.bare(tmp)
            report = {"pilot_gates": {name: True for name in ("all_pass", "decision_parity_pass", "coverage_pass", "measured_speed_pass", "fallback_replay_stable")}}
            path = Path(tmp) / "report.json"
            write(path, report)
            release = job.release(report, path)
            self.assertFalse(release["full_fast_pass_allowed"])

    def test_failed_gate_blocks_release_with_accepted_challenge(self):
        with workspace_tmp() as tmp:
            job = self.bare(tmp)
            job.plan["challenge_receipt"] = {"accepted": True}
            report = {"pilot_gates": {"all_pass": True, "measured_speed_pass": False}}
            path = Path(tmp) / "report.json"
            write(path, report)
            self.assertFalse(job.release(report, path)["full_fast_pass_allowed"])

    def test_claimed_pass_must_bind_complete_denominator_model_and_policy(self):
        with workspace_tmp() as tmp:
            job = self.bare(tmp)
            job.plan["challenge_receipt"] = {"accepted": True}
            report = {"pilot_gates": {name: True for name in ("all_pass", "decision_parity_pass", "coverage_pass", "measured_speed_pass", "fallback_replay_stable")}, "total_pairs": 12}
            path = Path(tmp) / "report.json"
            write(path, report)
            with self.assertRaises(ValueError):
                job.release(report, path)

    def test_semantic_receipt_must_bind_input_and_results(self):
        with workspace_tmp() as tmp:
            root = Path(tmp)
            inp = root / "inputs.jsonl"
            inp.write_text('{}\n', encoding="utf8")
            write(root / "protocol.json", {"input_sha256": "wrong", "pairs": 1})
            write(root / "completion_receipt.json", {"complete": True, "pairs": 1})
            with mock.patch.object(repair.teacher, "validate_labels") as validate:
                with self.assertRaises(ValueError):
                    repair.teacher_labels([{}], inp, root)
                validate.assert_not_called()

    def release_fixture(self, tmp):
        job = self.bare(tmp)
        job.plan["challenge_receipt"] = {"accepted": True, "sha256": "challenge"}
        model = job.out / "model"
        model.mkdir()
        write(model / "training_manifest.json", {"complete": True})
        policy = job.out / "routing_policy.json"
        write(policy, {"policy_sha256": "policy"})
        report = {"total_pairs": 512, "student_manifest_sha256": repair.transfer.sha(model / "training_manifest.json"),
                  "policy_sha256": "policy", "timing_comparison": {"student_total_seconds": 10.,
                  "teacher_all_total_seconds": 100., "fallback_teacher_total_seconds": 30.},
                  "student_bulk_timing": {"pairs": 512, "inference_seconds": 5., "setup_seconds": 2.},
                  "pilot_gates": {name: True for name in ("all_pass", "decision_parity_pass", "coverage_pass", "measured_speed_pass", "fallback_replay_stable")}}
        report_path = job.out / "evaluation_report.json"
        write(report_path, report)
        return job, report, report_path, {"root": str(model)}, {"path": str(policy)}

    def test_positive_release_binds_bulk_contract_without_launch(self):
        with workspace_tmp() as tmp:
            job, report, path, trained, policy = self.release_fixture(tmp)
            release = job.release(report, path, trained, policy)
            self.assertTrue(release["full_fast_pass_allowed"])
            self.assertEqual(release["schema"], "cosmos-kg-cascade-release-1")
            self.assertEqual(release["student_manifest_sha256"], report["student_manifest_sha256"])
            self.assertEqual(release["routing_policy_file_sha256"], repair.transfer.sha(policy["path"]))
            self.assertEqual(release["validation_report"]["sha256"], repair.transfer.sha(path))
            self.assertFalse(release["full_corpus_auto_launched"])
            self.assertFalse(release["full_corpus_selection_complete"])

    def test_report_rows_or_false_speed_cannot_be_released(self):
        with workspace_tmp() as tmp:
            job, report, path, trained, policy = self.release_fixture(tmp)
            report["rows"] = [{"teacher_decision": "include"}]
            with self.assertRaises(ValueError):
                job.release(report, path, trained, policy)
            del report["rows"]
            report["timing_comparison"]["fallback_teacher_total_seconds"] = 1000.
            with self.assertRaises(ValueError):
                job.release(report, path, trained, policy)

    def test_teacher_none_is_not_used_as_an_oracle_for_forced_student_fallback(self):
        with workspace_tmp() as tmp:
            job = self.bare(tmp)
            source = job.out / "source"
            fold = source / "dev"
            fold.mkdir(parents=True)
            row = {"article_id": "a", "ticker": "A", "company_name": "A", "title": "A reports", "body": "A profit rose", "published_at": None, "source_url": None}
            (fold / "inputs.jsonl").write_text(repair.preview.canonical(row) + "\n", encoding="utf8")
            (source / "selection_trace.jsonl").write_text(repair.preview.canonical({"article_id": "a", "ticker": "A", "fold": "dev", "group_id": "g"}) + "\n", encoding="utf8")
            predroot = job.out / "predictions"
            predroot.mkdir()
            prediction = {"article_id": "a", "ticker": "A", "input_sha256": repair.preview.canonical_input_sha256(row),
                          "student_manifest_sha256": "model", "probabilities": {"include": .99, "exclude": .005, "review": .005},
                          "requires_fallback": False, "fallback_reasons": [], "coverage": {"full_coverage": True,
                          "encoding_completed": True, "truncated": False, "total_content_tokens": 3, "covered_content_tokens": 3}}
            (predroot / "results.jsonl").write_text(repair.preview.canonical(prediction) + "\n", encoding="utf8")
            job.plan.update(source_sample_root=str(source), sample_sizes={"dev": 1})
            with mock.patch.object(repair, "teacher_labels", return_value=[None]):
                result = job.aligned("dev", {"root": "unused"}, {"root": str(predroot), "student_manifest_sha256": "model"})
            self.assertEqual(result["labels"], ["review"])
            self.assertEqual(result["unusable_teacher_pairs"], 1)
            self.assertEqual(result["probs"][0]["include"], .99)
            self.assertEqual(len(result["rows"]), 1)

    def test_positive_release_requires_bulk_timing_with_same_denominator(self):
        with workspace_tmp() as tmp:
            job, report, path, trained, policy = self.release_fixture(tmp)
            report["student_bulk_timing"]["pairs"] = 511
            with self.assertRaises(ValueError):
                job.release(report, path, trained, policy)
            del report["student_bulk_timing"]
            with self.assertRaises(ValueError):
                job.release(report, path, trained, policy)


class TimingTests(unittest.TestCase):
    def row(self):
        return {"article_id": "timing-1", "ticker": "A", "company_name": "A",
                "title": "A reports", "body": "A profit rose", "published_at": None, "source_url": None}

    def test_setup_is_not_amortized_into_per_pair_runtime(self):
        summary = {"pairs": 512, "inference_seconds": 5., "setup_seconds": 20.}
        measured = repair.bulk_timing(summary, 512, route_seconds=.25)
        self.assertEqual(measured, {"pairs": 512, "inference_seconds": 5.25, "setup_seconds": 20.})

    def test_nonfinite_bool_negative_and_wrong_count_rejected(self):
        for key, value in (("pairs", True), ("pairs", 511), ("inference_seconds", 0.),
                           ("inference_seconds", float("nan")), ("inference_seconds", True),
                           ("setup_seconds", -1.), ("setup_seconds", float("inf")), ("setup_seconds", False)):
            summary = {"pairs": 512, "inference_seconds": 5., "setup_seconds": 2.}
            summary[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                repair.bulk_timing(summary, 512)

    def test_predict_once_loads_once_and_records_two_actual_durations(self):
        with workspace_tmp() as tmp:
            root = Path(tmp)
            row = self.row()
            inp = root / "inputs.jsonl"
            inp.write_text(repair.preview.canonical(row) + "\n", encoding="utf8")
            prediction = {"article_id": row["article_id"], "ticker": row["ticker"],
                          "input_sha256": repair.preview.canonical_input_sha256(row),
                          "student_manifest_sha256": "model", "requires_fallback": False}
            instance = mock.Mock(manifest_sha256="model")
            instance.predict.return_value = [prediction]
            with mock.patch.object(repair.student, "StudentPredictor", return_value=instance) as factory, \
                 mock.patch.object(repair.time, "perf_counter", side_effect=[10., 12., 12., 17.]), \
                 mock.patch.object(repair, "code_hashes", return_value={"fixture": "frozen"}):
                summary = repair.predict_once(inp, root / "model", root / "pred", device="cpu")
            factory.assert_called_once_with(root / "model", device="cpu")
            instance.predict.assert_called_once_with([row])
            instance.close.assert_called_once()
            self.assertEqual(summary["setup_seconds"], 2.)
            self.assertEqual(summary["inference_seconds"], 5.)
            self.assertEqual(repair.read(root / "pred/summary.json"), summary)

    def test_prediction_error_releases_model_and_never_writes_complete_summary(self):
        with workspace_tmp() as tmp:
            root = Path(tmp)
            inp = root / "inputs.jsonl"
            inp.write_text(repair.preview.canonical(self.row()) + "\n", encoding="utf8")
            instance = mock.Mock(manifest_sha256="model")
            instance.predict.side_effect = ValueError("inference failed")
            with mock.patch.object(repair.student, "StudentPredictor", return_value=instance), \
                 mock.patch.object(repair, "code_hashes", return_value={}):
                with self.assertRaisesRegex(ValueError, "inference failed"):
                    repair.predict_once(inp, root / "model", root / "pred", device="cpu")
            instance.close.assert_called_once()
            self.assertFalse((root / "pred/summary.json").exists())

    def test_aggregate_report_exports_bound_inference_plus_route_not_startup(self):
        with workspace_tmp() as tmp:
            job = ReceiptTests().bare(tmp)
            path = job.out / "policy.json"
            write(path, {"policy_sha256": "policy"})
            data = {"rows": [{}, {}], "labels": ["include", "review"], "probs": [None, None],
                    "ids": ["a", "b"], "groups": ["a", "b"], "unusable_teacher_pairs": 1}
            pred = {"wall_seconds": 30., "student_manifest_sha256": "model",
                    "student_bulk_timing": {"pairs": 2, "inference_seconds": 5., "setup_seconds": 20.}}
            replay = {"measured": True, "seconds": 30., "route_seconds": .25, "replay_decision_changes": 0}
            report = {"rows": [{}, {}], "total_pairs": 2, "policy_sha256": "policy", "pilot_gates": {"all_pass": False}}
            with mock.patch.object(job, "aligned", return_value=data), \
                 mock.patch.object(repair.routing, "evaluate_cascade", return_value=report), \
                 mock.patch.object(job, "release", return_value={"full_fast_pass_allowed": False}):
                job.report({"uninterrupted_measurement": True, "wall_seconds": 100.}, pred,
                           {"path": str(path)}, replay, {})
            actual = repair.read(job.out / "evaluation_report.json")
            self.assertNotIn("rows", actual)
            self.assertEqual(actual["student_bulk_timing"], {"pairs": 2, "inference_seconds": 5.25, "setup_seconds": 20.})


if __name__ == "__main__":
    unittest.main()
