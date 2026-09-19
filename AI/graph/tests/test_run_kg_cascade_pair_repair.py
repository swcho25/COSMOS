"""CPU-only continuation checks; all folds below are synthetic fixture names."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import copy
import json
import shutil
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_kg_cascade_pair_repair as repair


@contextmanager
def workspace_tmp():
    root = Path(__file__).resolve().parents[1] / "artifacts/test_tmp_pair_repair"
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("case_" + uuid.uuid4().hex)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Test output escaped workspace")
    target.mkdir()
    try:
        yield target
    finally:
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Test cleanup escaped workspace")
        shutil.rmtree(target)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf8")


def bare(out):
    job = object.__new__(repair.PairRepair)
    job.out = out
    job.plan_sha = "continuation-plan"
    job.plan = {"candidate_modules": repair.MODULES, "parent_plan_sha256": "parent-plan",
        "user_scope": "news_selector_retraining_and_validation_only", "repair_scope": "metadata_only",
        "full_source_manifest_sha256": "source", "challenge_receipt": None,
        "full_fast_pass_authorized": False}
    return job


def parent_fixture(root):
    parent_out = root / "parent"
    parent_out.mkdir()
    configs = {name: {"candidate": name, "epochs": 4} for name in ("A", "B")}
    source = parent_out / "opaque_source.bin"
    source.write_bytes(b"fixture bytes only; no article or label contents")
    plan = {"source_file_sha256": {str(source): repair.transfer.sha(source)}, "candidate_configs": configs,
        "sample_sizes": repair.core.SIZES, "candidate_selection": "enabled_DEV_direction_then_min_selected_DEV_logloss_then_A",
        "routing_config": repair.core.routing.DEFAULT_CONFIG, "teacher_version": "fixture-teacher",
        "teacher_prompt_sha256": "prompt", "teacher_train_dev_full_relabeling_started": False,
        "calibration_test_labels_seen": False, "sequence": ["teacher_train", "teacher_dev", "student_A", "student_B"],
        "challenge_receipt": None, "model_receipt_sha256": "teacher-model", "student_parent_encoder": "encoder",
        "full_source_manifest_sha256": "full-source", "full_corpus_auto_launch": False}
    write(parent_out / "experiment_plan.json", plan)
    plan_sha = repair.transfer.sha(parent_out / "experiment_plan.json")
    write(parent_out / "plan_receipt.json", {"plan_sha256": plan_sha})
    for name, config in configs.items():
        write(parent_out / ("student_config_" + name + ".json"), config)
    model = parent_out / "student_A/attempt_002"
    write(model / "training_manifest.json", {"complete": True, "config": configs["A"], "code_hashes": {}})
    weights = model / "checkpoint/weights.bin"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"complete-original-A-checkpoint")
    values = {}
    for name in repair.REUSED_STAGES:
        output = parent_out / (name + "_output.json")
        if name == "student_train_A":
            value = {"root": str(model), "candidate": "A", "dev_log_loss": .92}
            files = [model / "training_manifest.json", weights]
        elif name == "student_dev_A":
            write(output, {"pairs": 256})
            value = {"root": str(output.parent), "candidate": "A",
                "student_manifest_sha256": repair.transfer.sha(model / "training_manifest.json")}
            files = [output]
        elif name == "dev_thresholds_A":
            write(output, {"directions": {"include": {"enabled_candidate": False}, "exclude": {"enabled_candidate": False}}})
            value = {"path": str(output), "enabled_dev_direction": False}
            files = [output]
        else:
            write(output, {"complete": True})
            value, files = {"root": str(output.parent)}, [output]
        write(parent_out / "stages" / (name + ".json"), {"complete": True, "stage": name, "plan_sha256": plan_sha,
            "files": {str(path): repair.transfer.sha(path) for path in files}, "value": value})
        values[name] = value
    parent = SimpleNamespace(out=parent_out, plan=plan, plan_sha=plan_sha)
    module = SimpleNamespace(configuration=lambda config: dict(config), code_hashes=lambda: {})
    return parent, module, weights


class FreezeTests(unittest.TestCase):
    def test_freeze_reuses_original_A_and_discloses_development_history(self):
        with workspace_tmp() as root:
            parent, module, weights = parent_fixture(root)
            previous = {str(p): p.read_bytes() for p in parent.out.rglob("*") if p.is_file()}
            with mock.patch.object(repair.core, "Repair", return_value=parent), \
                 mock.patch.object(repair, "student_module", return_value=module), \
                 mock.patch.object(repair, "code_hashes", return_value={}):
                plan = repair.freeze_plan(root / "continuation", parent.out)
            self.assertEqual(plan["candidate_configs"], parent.plan["candidate_configs"])
            self.assertEqual(plan["routing_config"], parent.plan["routing_config"])
            self.assertEqual(plan["sample_sizes"], parent.plan["sample_sizes"])
            self.assertTrue(plan["original_A_dev_result_observed_before_repair"])
            self.assertFalse(plan["original_A_dev_enabled_direction"])
            self.assertTrue(plan["teacher_train_dev_labels_complete_and_reused"])
            self.assertTrue(plan["teacher_train_dev_full_relabeling_started"])
            self.assertTrue(plan["A_training_complete_and_reused"])
            self.assertFalse(plan["parent_freeze_observation_history"]["teacher_train_dev_full_relabeling_started"])
            self.assertFalse(plan["calibration_test_labels_seen"])
            self.assertFalse(plan["full_fast_pass_authorized"])
            self.assertEqual(plan["source_file_sha256"][str(weights)], repair.transfer.sha(weights))
            self.assertEqual(plan["sequence"][:5], ["reuse_parent_" + name for name in repair.REUSED_STAGES])
            self.assertEqual(previous, {str(p): p.read_bytes() for p in parent.out.rglob("*") if p.is_file()})

    def test_started_parent_holdout_or_selection_rejected_without_read(self):
        for name in ("teacher_calibration", "teacher_test", "student_calibration_B", "candidate_selection.json", "stages/teacher_test.json"):
            with self.subTest(name=name), workspace_tmp() as root:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"do not read")
                with mock.patch.object(repair, "read", side_effect=AssertionError("Content was read")):
                    with self.assertRaisesRegex(ValueError, "boundary|already exist"):
                        repair.require_unstarted_holdouts(root)

    def test_existing_output_plan_rejected_before_parent_access(self):
        with workspace_tmp() as root:
            write(root / "experiment_plan.json", {"untouched": True})
            with mock.patch.object(repair.core, "Repair") as parent:
                with self.assertRaises(ValueError):
                    repair.freeze_plan(root, root / "parent")
                parent.assert_not_called()

    def test_tampered_A_checkpoint_rejected_before_freeze(self):
        with workspace_tmp() as root:
            parent, module, weights = parent_fixture(root)
            weights.write_bytes(b"changed")
            with mock.patch.object(repair.core, "Repair", return_value=parent):
                with self.assertRaisesRegex(ValueError, "Parent stage files changed"):
                    repair.freeze_plan(root / "new", parent.out)
            self.assertFalse((root / "new/experiment_plan.json").exists())

    def test_repaired_module_cannot_change_fixed_configuration(self):
        with workspace_tmp() as root:
            parent, module, _ = parent_fixture(root)
            module.configuration = lambda config: {**config, "epochs": 5}
            with mock.patch.object(repair.core, "Repair", return_value=parent), \
                 mock.patch.object(repair, "student_module", return_value=module):
                with self.assertRaisesRegex(ValueError, "predeclared candidate configuration"):
                    repair.freeze_plan(root / "new", parent.out)

    def test_completed_parent_B_not_silently_retrained(self):
        with workspace_tmp() as root:
            parent, _, _ = parent_fixture(root)
            write(parent.out / "stages/student_train_B.json", {})
            with mock.patch.object(repair.core, "Repair", return_value=parent):
                with self.assertRaisesRegex(ValueError, "Parent B already completed"):
                    repair.freeze_plan(root / "new", parent.out)


class ResumeTests(unittest.TestCase):
    def test_reuse_does_not_mutate_parent_and_rejects_changed_receipt(self):
        with workspace_tmp() as root:
            parent, _, _ = parent_fixture(root)
            job = bare(root)
            job.plan["parent_plan_sha256"] = parent.plan_sha
            bound = repair.parent_stage(parent, "student_train_A")
            job.plan["reused_parent_stages"] = {"student_train_A": bound}
            original = copy.deepcopy(bound)
            value, paths = job.reuse("student_train_A")
            self.assertEqual(value["student_module"], repair.MODULES["A"])
            self.assertEqual(bound, original)
            self.assertIn(Path(bound["receipt_path"]), paths)
            write(Path(bound["receipt_path"]), {"changed": True})
            with self.assertRaisesRegex(ValueError, "Reused parent stage changed"):
                job.reuse("student_train_A")

    def test_completed_continuation_stage_reused_once_with_integrity_checks(self):
        with workspace_tmp() as root:
            job = bare(root)
            (root / "stages").mkdir()
            job.assert_frozen = lambda: None
            job.status = lambda *args, **kwargs: None
            artifact = root / "artifact.json"
            write(artifact, {"frozen": True})
            action = mock.Mock(return_value=({"candidate": "B"}, [artifact]))
            self.assertEqual(job.stage("student_train_B", action), {"candidate": "B"})
            self.assertEqual(job.stage("student_train_B", action), {"candidate": "B"})
            self.assertEqual(action.call_count, 1)
            write(artifact, {"changed": True})
            with self.assertRaises(ValueError):
                job.stage("student_train_B", action)

    def test_changed_bound_source_prevents_resume(self):
        with workspace_tmp() as root:
            source = root / "source.bin"
            source.write_bytes(b"original")
            job = bare(root)
            job.plan.update(schema=repair.SCHEMA, code_sha256={}, source_file_sha256={str(source): repair.transfer.sha(source)})
            write(root / "experiment_plan.json", job.plan)
            job.plan_sha = repair.transfer.sha(root / "experiment_plan.json")
            source.write_bytes(b"changed")
            with mock.patch.object(repair, "code_hashes", return_value={}):
                with self.assertRaisesRegex(ValueError, "Parent completed stages/source bytes changed"):
                    job.assert_frozen()

    def test_A_training_always_rejected_before_encoder_or_source_access(self):
        with workspace_tmp() as root:
            with mock.patch.object(repair, "student_module") as module:
                with self.assertRaisesRegex(ValueError, "never retrained"):
                    bare(root).train("A", {}, {})
                module.assert_not_called()

    def test_incorrect_prediction_module_rejected_before_child(self):
        with workspace_tmp() as root:
            job = bare(root)
            job.child = mock.Mock()
            with self.assertRaisesRegex(ValueError, "implementation mismatch"):
                job.predict("dev", {"candidate": "B", "student_module": repair.MODULES["A"]})
            job.child.assert_not_called()


class Harness(repair.PairRepair):
    def __init__(self, out, dev_enabled=(False, False), cal_enabled=False):
        self.out, self.plan_sha = out, "synthetic-plan"
        self.plan = {"candidate_selection": "unchanged-original-rule", "candidate_modules": repair.MODULES}
        self.dev_enabled, self.cal_enabled = dict(zip(("A", "B"), dev_enabled)), cal_enabled
        self.calls, self.train_calls, self.teach_calls = [], [], []
        self.last_status = None

    def status(self, state, **extra):
        self.last_status = {"state": state, **extra}

    def stage(self, name, action):
        self.calls.append(name)
        return action()[0]

    def reuse(self, name):
        if name == "student_train_A":
            return {"candidate": "A", "student_module": repair.MODULES["A"], "dev_log_loss": .3}, []
        if name == "dev_thresholds_A":
            return {"enabled_dev_direction": self.dev_enabled["A"]}, []
        return {}, []

    def teach(self, fold):
        self.teach_calls.append(fold)
        return {"fold": fold}, []

    def train(self, name, *args):
        self.train_calls.append(name)
        return {"candidate": name, "student_module": repair.MODULES[name], "dev_log_loss": .6}, []

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


class SequenceTests(unittest.TestCase):
    def test_only_B_trained_and_no_holdouts_if_both_dev_routes_disabled(self):
        with workspace_tmp() as root:
            job = Harness(root)
            job.run()
            self.assertEqual(job.train_calls, ["B"])
            self.assertEqual(job.teach_calls, [])
            self.assertEqual(job.calls[:5], list(repair.REUSED_STAGES))
            self.assertEqual(job.last_status["stage"], "dev")
            self.assertEqual(job.calls[-1], "candidate_selection")

    def test_calibration_failure_does_not_open_test(self):
        with workspace_tmp() as root:
            job = Harness(root, (False, True), False)
            job.run()
            self.assertEqual(job.teach_calls, ["calibration"])
            self.assertEqual(job.last_status["stage"], "calibration")
            self.assertLess(job.calls.index("candidate_selection"), job.calls.index("teacher_calibration"))

    def test_success_keeps_test_replay_report_sequence_without_bulk(self):
        with workspace_tmp() as root:
            job = Harness(root, (False, True), True)
            job.run()
            self.assertEqual(job.teach_calls, ["calibration", "test"])
            self.assertEqual(job.calls[-4:], ["teacher_test", "student_test", "fallback_replay", "report"])
            self.assertEqual(job.last_status["state"], "PILOT_COMPLETE")
            self.assertFalse(any("bulk" in name for name in job.calls))
            self.assertEqual(repair.read(root / "candidate_selection.json")["selected"], "B")

    def test_selection_rule_lower_loss_then_A_is_inherited_unchanged(self):
        self.assertIs(repair.PairRepair.select, repair.core.Repair.select)
        self.assertIs(repair.PairRepair.confirm, repair.core.Repair.confirm)
        self.assertIs(repair.PairRepair.report, repair.core.Repair.report)
        values = {name: {"enabled_dev_direction": True, "dev_log_loss": .6} for name in ("A", "B")}
        self.assertEqual(repair.core.choose_candidate(values), "A")
        values["B"]["dev_log_loss"] = .5
        self.assertEqual(repair.core.choose_candidate(values), "B")

    def test_failure_and_interruption_record_status_without_report(self):
        for error, status in ((ValueError("failure"), "FAILED"), (KeyboardInterrupt(), "INTERRUPTED")):
            with self.subTest(status=status), workspace_tmp() as root:
                job = Harness(root)
                with mock.patch.object(job, "train", side_effect=error):
                    with self.assertRaises(type(error)):
                        job.run()
                self.assertEqual(job.last_status["state"], status)
                self.assertNotIn("teacher_calibration", job.calls)
                self.assertNotIn("report", job.calls)


class ReleaseTests(unittest.TestCase):
    def fixture(self, root, candidate="B"):
        job = bare(root)
        job.plan["challenge_receipt"] = {"accepted": True}
        model = root / "model"
        write(model / "training_manifest.json", {"complete": True})
        policy = root / "policy.json"
        write(policy, {"policy_sha256": "policy"})
        report = {"total_pairs": 512, "student_manifest_sha256": repair.transfer.sha(model / "training_manifest.json"),
            "policy_sha256": "policy", "pilot_gates": {name: True for name in
                ("all_pass", "decision_parity_pass", "coverage_pass", "measured_speed_pass", "fallback_replay_stable")},
            "student_bulk_timing": {"pairs": 512, "inference_seconds": 8., "setup_seconds": 2.},
            "timing_comparison": {"student_total_seconds": 10., "teacher_all_total_seconds": 100., "fallback_teacher_total_seconds": 20.}}
        path = root / "evaluation_report.json"
        write(path, report)
        return job, report, path, {"root": str(model), "candidate": candidate, "student_module": repair.MODULES[candidate]}, {"path": str(policy)}

    def test_even_passed_gates_and_challenge_never_authorize_full_pass(self):
        for candidate in ("A", "B"):
            with self.subTest(candidate=candidate), workspace_tmp() as root:
                job, report, path, trained, policy = self.fixture(root, candidate)
                value = job.release(report, path, trained, policy)
                self.assertFalse(value["full_fast_pass_allowed"])
                self.assertFalse(value["full_corpus_auto_launched"])
                self.assertFalse(value["notifications_authorized"])
                self.assertEqual(value["student_module"], repair.MODULES[candidate])
                self.assertEqual(value["validation_report"]["sha256"], repair.transfer.sha(path))
                self.assertEqual(value["student_manifest_sha256"], report["student_manifest_sha256"])
                self.assertEqual(repair.read(path)["student_bulk_timing"]["pairs"], 512)
                self.assertTrue(repair.read(path)["original_A_dev_result_observed_before_repair"])

    def test_mismatched_denominator_module_and_timing_rejected(self):
        for field in ("denominator", "module", "timing"):
            with self.subTest(field=field), workspace_tmp() as root:
                job, report, path, trained, policy = self.fixture(root)
                if field == "denominator":
                    report["total_pairs"] = 511
                elif field == "module":
                    trained["student_module"] = repair.MODULES["A"]
                else:
                    report["student_bulk_timing"]["pairs"] = 511
                with self.assertRaises(ValueError):
                    job.release(report, path, trained, policy)

    def test_report_never_copies_article_or_label_rows(self):
        with workspace_tmp() as root:
            job, report, path, trained, policy = self.fixture(root)
            report["rows"] = [{}]
            with self.assertRaisesRegex(ValueError, "aggregate only"):
                job.release(report, path, trained, policy)

    def test_early_quality_failure_receipt_denied_with_implementation_binding(self):
        with workspace_tmp() as root:
            job = bare(root)
            report = {"pilot_gates": {"all_pass": False}}
            path = root / "report.json"
            write(path, report)
            value = job.release(report, path)
            self.assertFalse(value["full_fast_pass_allowed"])
            self.assertIsNone(value["student_manifest_sha256"])
            self.assertEqual(value["candidate_modules"], repair.MODULES)


class PredictionTimingTests(unittest.TestCase):
    def row(self):
        return {"article_id": "synthetic", "ticker": "A", "company_name": "A", "title": "A result",
            "body": "A reports higher operating profit.", "published_at": None, "source_url": None}

    def test_selected_module_once_loaded_and_measured_separately(self):
        with workspace_tmp() as root:
            row = self.row()
            inputs = root / "inputs.jsonl"
            inputs.write_text(repair.preview.canonical(row) + "\n", encoding="utf8")
            predictor = mock.Mock(manifest_sha256="model")
            predictor.predict.return_value = [{"article_id": row["article_id"], "ticker": row["ticker"],
                "input_sha256": repair.preview.canonical_input_sha256(row), "student_manifest_sha256": "model", "requires_fallback": False}]
            module = mock.Mock(StudentPredictor=mock.Mock(return_value=predictor))
            with mock.patch.object(repair, "student_module", return_value=module) as selected, \
                 mock.patch.object(repair, "code_hashes", return_value={}), \
                 mock.patch.object(repair.time, "perf_counter", side_effect=[0., 3., 3., 10.]):
                summary = repair.predict_once(inputs, root / "checkpoint", root / "predictions", repair.MODULES["B"], device="cpu")
            selected.assert_called_once_with(repair.MODULES["B"])
            module.StudentPredictor.assert_called_once()
            predictor.predict.assert_called_once()
            predictor.close.assert_called_once()
            self.assertEqual(summary["setup_seconds"], 3.)
            self.assertEqual(summary["inference_seconds"], 7.)
            self.assertEqual(summary["student_module"], repair.MODULES["B"])
            self.assertEqual(repair.core.bulk_timing(summary, 1), {"pairs": 1, "setup_seconds": 3., "inference_seconds": 7.})

    def test_unapproved_module_rejected_without_import(self):
        with mock.patch.object(repair.importlib, "import_module") as importer:
            with self.assertRaises(ValueError):
                repair.student_module("different_model")
            importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
