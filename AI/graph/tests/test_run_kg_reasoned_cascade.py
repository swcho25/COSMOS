"""CPU-only orchestration checks; all folds/data below are synthetic fixtures.

No actual CAL/TEST inputs, model weights, GPU, or subprocesses are accessed.
"""
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
import run_kg_reasoned_cascade as runner
import test_kg_reasoned_teacher_v3 as teacher_fixtures


@contextmanager
def workspace_tmp():
    root = Path(__file__).resolve().parents[1] / "artifacts/test_tmp_reasoned_runner"
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
    job = object.__new__(runner.ReasonedRun)
    job.out, job.plan_sha = out, "synthetic-plan"
    job.plan = {"sample_sizes": dict(runner.core.SIZES),
                "alias_provenance": runner.aliases.provenance(),
                "user_scope": "news_selector_retraining_and_validation_only",
                "full_source_manifest_sha256": "synthetic-source"}
    return job


def parent_fixture(root):
    out = root / "parent"
    out.mkdir()
    source = out / "source.bin"
    source.write_bytes(b"opaque synthetic fixture; no article contents")
    plan = {"source_file_sha256": {str(source): runner.transfer.sha(source)},
            "sample_sizes": dict(runner.core.SIZES),
            "routing_config": copy.deepcopy(runner.core.routing.DEFAULT_CONFIG),
            "source_sample_root": str(out / "synthetic_samples"),
            "full_source_manifest_sha256": "synthetic-source",
            "student_parent_encoder": {"fixture": True},
            "model_receipt_sha256": runner.transfer.sha(
                runner.HERE / "local_models/relevance_model_receipt.json")}
    write(out / "experiment_plan.json", plan)
    plan_sha = runner.transfer.sha(out / "experiment_plan.json")
    write(out / "plan_receipt.json", {"plan_sha256": plan_sha})
    return SimpleNamespace(out=out, plan=plan, plan_sha=plan_sha)


class NamespaceTests(unittest.TestCase):
    def test_private_bindings_leave_original_experiment_untouched(self):
        self.assertIsNot(runner.core, runner.original)
        self.assertIs(runner.core.teacher, runner.teacher)
        self.assertIs(runner.core.student, runner.student)
        self.assertIs(runner.core.code_hashes, runner.code_hashes)
        self.assertIsNot(runner.original.teacher, runner.teacher)
        self.assertIsNot(runner.original.student, runner.student)
        self.assertEqual(runner.original.teacher.VERSION, "kg_semantic_teacher_v1")
        self.assertIs(runner.core.teacher_labels.__globals__["teacher"], runner.teacher)
        self.assertIs(runner.core.predict_once.__globals__["student"], runner.student)
        self.assertEqual(runner.student.LABEL_POLICY, runner.teacher.VERSION)
        self.assertIs(runner.ReasonedRun.replay, runner.core.Repair.replay)
        self.assertIs(runner.ReasonedRun.confirm, runner.core.Repair.confirm)
        self.assertIs(runner.ReasonedRun.report, runner.core.Repair.report)

    def test_child_cli_remaps_teacher_student_and_prediction_runner(self):
        expected = {"kg_semantic_teacher.py": Path(runner.teacher.__file__).name,
                    "kg_cascade_student_v2.py": "kg_reasoned_student.py",
                    "run_kg_cascade_repair.py": "run_kg_reasoned_cascade.py"}
        with workspace_tmp() as root:
            job = bare(root)
            for source, target in expected.items():
                with self.subTest(script=source), mock.patch.object(
                        runner.core.Repair, "child", return_value=1.5) as child:
                    args = ["--inputs", root / "synthetic.jsonl", "--out", root / "output"]
                    self.assertEqual(job.child("fixture", source, args), 1.5)
                    child.assert_called_once_with("fixture", target, args)

    def test_snapshot_hashes_exclude_parent_relative_data_paths(self):
        hashes = runner.code_hashes()
        for required in ("run_kg_reasoned_cascade.py", Path(runner.teacher.__file__).name,
                         "kg_reasoned_student.py", "kg_target_aliases.py"):
            self.assertIn(required, hashes)
        self.assertTrue(all(Path(name).name == name and name.endswith(".py") for name in hashes))


class FreezeTests(unittest.TestCase):
    def freeze(self, root, parent):
        with mock.patch.object(runner.original, "Repair", return_value=parent), \
             mock.patch.object(runner, "challenge_binding", return_value={"accepted": True, "path": "fixture"}), \
             mock.patch.object(runner, "code_hashes", return_value={}):
            return runner.freeze_plan(root / "new", parent.out, root / "challenge.json")

    def test_freeze_binds_sources_aliases_and_unchanged_gates_without_reusing_labels(self):
        with workspace_tmp() as root:
            parent = parent_fixture(root)
            before = {str(p): p.read_bytes() for p in parent.out.rglob("*") if p.is_file()}
            plan = self.freeze(root, parent)
            self.assertEqual(plan["candidate_configs"], {"B": runner.student.configuration()})
            self.assertEqual(plan["routing_config"], parent.plan["routing_config"])
            self.assertEqual(plan["sample_sizes"], runner.core.SIZES)
            self.assertEqual(plan["maximum_unusable_teacher_fraction"], .05)
            self.assertEqual(plan["source_file_sha256"][str(runner.aliases.ALIAS_FILE)],
                             runner.transfer.sha(runner.aliases.ALIAS_FILE))
            self.assertFalse(plan["old_teacher_labels_reused"])
            self.assertFalse(plan["old_student_weights_reused"])
            self.assertTrue(plan["old_train_dev_labels_and_predictions_seen_for_diagnosis"])
            self.assertFalse(plan["calibration_test_labels_seen"])
            for field in ("full_fast_pass_authorized", "full_corpus_auto_launch", "notifications_authorized"):
                self.assertFalse(plan[field])
            self.assertEqual(before, {str(p): p.read_bytes() for p in parent.out.rglob("*") if p.is_file()})

    def test_previously_started_parent_holdout_rejected_without_content_read(self):
        for name in ("teacher_calibration", "teacher_test", "candidate_selection.json", "evaluation_report.json"):
            with self.subTest(name=name), workspace_tmp() as root:
                parent = parent_fixture(root)
                (parent.out / name).write_bytes(b"sealed; test must not read this")
                with self.assertRaisesRegex(ValueError, "pre-CAL"):
                    self.freeze(root, parent)
                self.assertFalse((root / "new/experiment_plan.json").exists())

    def test_existing_plan_rejected_before_parent_or_challenge_access(self):
        with workspace_tmp() as root:
            write(root / "experiment_plan.json", {"preserve": True})
            with mock.patch.object(runner.original, "Repair") as parent:
                with self.assertRaises(ValueError):
                    runner.freeze_plan(root, root / "parent", root / "challenge.json")
                parent.assert_not_called()

    def test_challenge_requires_current_teacher_source_and_non_gold_declaration(self):
        with workspace_tmp() as root:
            bound = root / "inputs.jsonl"
            bound.write_bytes(b"synthetic development input")
            value = {"accepted": True, "teacher_version": runner.teacher.VERSION,
                     "teacher_code_hashes": runner.teacher.code_hashes(),
                     "teacher_prompt_sha256": runner.teacher.digest(runner.teacher.CRITERIA),
                     "alias_provenance": runner.aliases.provenance(), "human_gold": False,
                     "factual_accuracy_established": False, "reference_kind": runner.original.CHALLENGE_REFERENCE,
                     "bound_files": {str(bound): runner.transfer.sha(bound)}}
            path = root / "challenge.json"
            write(path, value)
            self.assertTrue(runner.challenge_binding(path)["accepted"])
            for key, bad in (("accepted", False), ("human_gold", True),
                             ("factual_accuracy_established", True), ("teacher_version", "old")):
                write(path, {**value, key: bad})
                with self.subTest(field=key), self.assertRaises(ValueError):
                    runner.challenge_binding(path)
            write(path, value)
            bound.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                runner.challenge_binding(path)


class ResumeAndTeacherTests(unittest.TestCase):
    def test_completed_stage_reused_with_hash_checks(self):
        with workspace_tmp() as root:
            job = bare(root)
            (root / "stages").mkdir()
            job.assert_frozen = lambda: None
            job.status = lambda *args, **kwargs: None
            output = root / "complete.json"
            write(output, {"complete": True})
            action = mock.Mock(return_value=({"candidate": "B"}, [output]))
            self.assertEqual(job.stage("student_train_B", action), {"candidate": "B"})
            self.assertEqual(job.stage("student_train_B", action), {"candidate": "B"})
            self.assertEqual(action.call_count, 1)
            write(output, {"tampered": True})
            with self.assertRaises(ValueError):
                job.stage("student_train_B", action)

    def test_incomplete_training_attempt_preserved_and_next_attempt_chosen(self):
        with workspace_tmp() as root:
            job = bare(root)
            first = job.attempt("student_B")
            write(first / "partial.json", {"epochs_finished": 2})
            second = job.attempt("student_B")
            self.assertNotEqual(first, second)
            self.assertEqual(runner.read(first / "partial.json"), {"epochs_finished": 2})
            self.assertFalse(second.exists())

    def test_new_teacher_receipt_and_labels_compatible_with_inherited_loader(self):
        with workspace_tmp() as root:
            row = teacher_fixtures.source()
            source = root / "inputs.jsonl"
            source.write_text(runner.preview.canonical(row) + "\n", encoding="utf8")
            folder = root / "teacher"
            folder.mkdir()
            with mock.patch.object(runner.teacher._core, "resolver", return_value=teacher_fixtures.Aliases):
                protocol = runner.teacher.make_protocol(runner.transfer.sha(source), 1)
                result = teacher_fixtures.teacher().classify(row, protocol)
                write(folder / "protocol.json", protocol)
                (folder / "results.jsonl").write_text(runner.preview.canonical(result) + "\n", encoding="utf8")
                write(folder / "completion_receipt.json", {"complete": True, "pairs": 1,
                      "protocol_sha256": runner.teacher.digest(protocol),
                      "results_sha256": runner.transfer.sha(folder / "results.jsonl"), "resumed_pairs": 0})
                values = runner.core.teacher_labels([row], source, folder)
                self.assertEqual(values, [runner.student.CLASSES.index("include")])
                changed = dict(result, decision="exclude")
                (folder / "results.jsonl").write_text(runner.preview.canonical(changed) + "\n", encoding="utf8")
                with self.assertRaisesRegex(ValueError, "receipt"):
                    runner.core.teacher_labels([row], source, folder)

    def test_prediction_once_loads_reasoned_model_once_and_uses_cpu_fixture(self):
        with workspace_tmp() as root:
            row = teacher_fixtures.source()
            inputs = root / "inputs.jsonl"
            inputs.write_text(runner.preview.canonical(row) + "\n", encoding="utf8")
            predictor = mock.Mock(manifest_sha256="fixture-model")
            predictor.predict.return_value = [{"article_id": row["article_id"], "ticker": row["ticker"],
                "input_sha256": runner.preview.canonical_input_sha256(row),
                "student_manifest_sha256": "fixture-model", "requires_fallback": False}]
            with mock.patch.object(runner.student, "StudentPredictor", return_value=predictor) as constructor, \
                 mock.patch.object(runner.core, "code_hashes", return_value={}), \
                 mock.patch.object(runner.core.time, "perf_counter", side_effect=[0., 2., 2., 5.]):
                summary = runner.core.predict_once(inputs, root / "model", root / "prediction", device="cpu")
            constructor.assert_called_once_with((root / "model").resolve(), device="cpu")
            predictor.predict.assert_called_once()
            predictor.close.assert_called_once()
            self.assertEqual(summary["setup_seconds"], 2.)
            self.assertEqual(summary["inference_seconds"], 3.)


class Harness(runner.ReasonedRun):
    def __init__(self, out, dev_enabled=False, cal_enabled=False, unusable=None):
        self.out, self.plan_sha = out, "synthetic-plan"
        self.plan = {"sample_sizes": dict(runner.core.SIZES)}
        self.dev_enabled, self.cal_enabled = dev_enabled, cal_enabled
        self.unusable, self.calls, self.teacher_calls, self.train_calls = unusable or {}, [], [], []
        self.last_status = None

    def stage(self, name, action):
        self.calls.append(name)
        return action()[0]

    def status(self, state, **extra):
        self.last_status = {"state": state, **extra}

    def teach(self, fold):
        self.teacher_calls.append(fold)
        return {"unusable_teacher_pairs": self.unusable.get(fold, 0)}, []

    def train(self, name, *args):
        self.train_calls.append(name)
        return {"candidate": name}, []

    def predict(self, *args):
        return {}, []

    def tune(self, *args):
        return {"enabled_dev_direction": self.dev_enabled}, []

    def confirm(self, *args):
        return {"enabled_direction": self.cal_enabled}, []

    def replay(self, *args):
        return {}, []

    def report(self, *args):
        return {"gates": {"all_pass": True}}, []

    def quality_failure(self, stage, reason):
        self.status("QUALITY_GATE_FAILED", stage=stage, reason=reason)


class SequenceTests(unittest.TestCase):
    def test_dev_failure_stops_before_calibration(self):
        with workspace_tmp() as root:
            job = Harness(root)
            job.run()
            self.assertEqual(job.teacher_calls, ["train", "dev"])
            self.assertEqual(job.train_calls, ["B"])
            self.assertEqual(job.last_status["stage"], "dev")
            self.assertNotIn("teacher_calibration", job.calls)

    def test_calibration_failure_stops_before_test(self):
        with workspace_tmp() as root:
            job = Harness(root, dev_enabled=True)
            job.run()
            self.assertEqual(job.teacher_calls, ["train", "dev", "calibration"])
            self.assertEqual(job.last_status["stage"], "calibration")
            self.assertNotIn("teacher_test", job.calls)

    def test_unusable_limit_stops_each_fold_before_dependent_work(self):
        cases = [("train", 77, "teacher_dev"), ("dev", 13, "student_train_B"),
                 ("calibration", 39, "student_calibration"), ("test", 26, "student_test")]
        for fold, failures, forbidden in cases:
            with self.subTest(fold=fold), workspace_tmp() as root:
                job = Harness(root, True, True, {fold: failures})
                job.run()
                self.assertEqual(job.last_status["stage"], "teacher_" + fold)
                self.assertNotIn(forbidden, job.calls)

    def test_exact_five_percent_accepted_and_excess_rejected(self):
        with workspace_tmp() as root:
            job = bare(root)
            job.plan["sample_sizes"]["train"] = 100  # synthetic count only
            self.assertTrue(job.labels_usable({"unusable_teacher_pairs": 5}, "train"))
            self.assertFalse(job.labels_usable({"unusable_teacher_pairs": 6}, "train"))

    def test_success_preserves_single_model_and_full_gate_sequence(self):
        with workspace_tmp() as root:
            job = Harness(root, True, True)
            job.run()
            self.assertEqual(job.calls, ["teacher_train", "teacher_dev", "student_train_B", "student_dev_B",
                "dev_thresholds_B", "teacher_calibration", "student_calibration", "calibration_confirm",
                "teacher_test", "student_test", "fallback_replay", "report"])
            self.assertEqual(job.last_status["state"], "PILOT_COMPLETE")
            self.assertEqual(job.train_calls, ["B"])

    def test_failure_and_keyboard_interrupt_record_status(self):
        for error, expected in ((ValueError("fixture"), "FAILED"), (KeyboardInterrupt(), "INTERRUPTED")):
            with self.subTest(expected=expected), workspace_tmp() as root:
                job = Harness(root)
                with mock.patch.object(job, "teach", side_effect=error):
                    with self.assertRaises(type(error)):
                        job.run()
                self.assertEqual(job.last_status["state"], expected)
                self.assertNotIn("student_train_B", job.calls)


class ReleaseTests(unittest.TestCase):
    def test_passed_model_still_never_authorizes_full_selection_or_notifications(self):
        with workspace_tmp() as root:
            job = bare(root)
            model = root / "model"
            write(model / "training_manifest.json", {"complete": True})
            policy = root / "policy.json"
            write(policy, {"policy_sha256": "frozen-policy"})
            report = {"pilot_gates": {"all_pass": True}, "total_pairs": 512,
                      "timing_scope": "old compact teacher placeholder"}
            path = root / "evaluation_report.json"
            result = job.release(report, path, {"root": str(model)}, {"path": str(policy)})
            for field in ("full_fast_pass_allowed", "full_corpus_auto_launched", "notifications_authorized"):
                self.assertFalse(result[field])
            self.assertFalse(runner.read(path)["factual_accuracy_established"])
            self.assertIn("reasoned full-article teacher", runner.read(path)["timing_scope"])
            self.assertEqual(result["student_module"], "kg_reasoned_student")
            self.assertEqual(result["validation_report"]["sha256"], runner.transfer.sha(path))

    def test_release_rejects_row_level_report(self):
        with workspace_tmp() as root:
            for field in ("rows", "labels"):
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "aggregates"):
                    bare(root).release({field: []}, root / "report.json")

    def test_status_always_marks_full_pass_false(self):
        with workspace_tmp() as root:
            bare(root).status("RUNNING", stage="synthetic-stage")
            state = runner.read(root / "status.json")
            self.assertFalse(state["full_fast_pass_allowed"])
            self.assertFalse(state["full_corpus_selection_complete"])
            self.assertFalse(state["notifications_authorized"])

    def test_quality_failure_distinguishes_opened_test_from_completed_model_evaluation(self):
        cases = [("dev", False, False), ("calibration", False, False),
                 ("teacher_test", False, True), ("teacher_test", True, True)]
        for stage, existing_teacher_dir, opened in cases:
            with self.subTest(stage=stage, existing=existing_teacher_dir), workspace_tmp() as root:
                if existing_teacher_dir:
                    (root / "teacher_test").mkdir()  # Empty synthetic marker only.
                job = bare(root)
                job.quality_failure(stage, "synthetic quality failure")
                report = runner.read(root / "evaluation_report.json")
                self.assertEqual(report["heldout_test_inputs_opened"], opened)
                self.assertEqual(report["heldout_test_teacher_started"], opened)
                self.assertFalse(report["heldout_test_evaluated"])
                self.assertFalse(report["full_fast_pass_allowed"])
                self.assertEqual(report["teacher_version"], runner.teacher.VERSION)


if __name__ == "__main__":
    unittest.main()
