"""No-GPU/no-network checks of the pilot's stage order, resume and CLI wiring."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_kg_cascade_pilot as run


SEQUENCE = ["download_only", "sample", "teacher_train", "teacher_dev", "student_train",
            "student_dev", "dev_thresholds", "teacher_calibration", "student_calibration",
            "calibration_confirm", "teacher_test", "student_test", "fallback_replay", "report"]


def row(identity):
    return {"article_id": identity, "ticker": "A", "company_name": "Company A",
            "published_at": None, "title": identity + " title", "body": identity + " body", "source_url": None}


class PilotTests(unittest.TestCase):
    def setUp(self):
        artifact_root = run.HERE / "artifacts"
        self.root = artifact_root / ("cascade_orchestrator_test_" + uuid.uuid4().hex)
        self.assertTrue(self.root.resolve().is_relative_to(artifact_root.resolve()))
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        # Guard every test against accidentally launching a real GPU/network child.
        guard = mock.patch.object(run.subprocess, "run", side_effect=AssertionError("Real subprocess forbidden in CPU test"))
        self.subprocess = guard.start()
        self.addCleanup(guard.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        run.transfer.atomic_json(path, value)
        return path

    def jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(run.preview.canonical(x) + "\n" for x in rows), encoding="utf8")
        return path

    def bare(self):
        # Real stage receipts/status/locks are exercised, frozen-input verification
        # has separate tests below and is not bypassed in production.
        pilot = run.Pilot.__new__(run.Pilot)
        pilot.out = self.root / "pilot"
        pilot.out.mkdir()
        (pilot.out / "stages").mkdir()
        (pilot.out / "logs").mkdir()
        pilot.plan_sha = "a" * 64
        pilot.plan = {"student_config": dict(run.student.DEFAULT_CONFIG),
            "student_parent_encoder": {"fixture": True},
            "sample_sizes": dict.fromkeys(run.sampler.SIZES, 1), "sequence": SEQUENCE,
            "routing_config": dict(run.routing.DEFAULT_CONFIG),
            "remote_run": "/home/ubuntu/ner-work/kg_reselection_fixture", "exclusions": "fixture"}
        pilot.assert_frozen = mock.Mock()
        self.write(pilot.out / "student_config.json", pilot.plan["student_config"])
        return pilot

    def fake_pipeline(self, pilot, events, *, interrupt_first_sample=False):
        sample_calls = 0
        def emit(name, value=None):
            events.append(name)
            artifact = self.write(pilot.out / "fake_artifacts" / (name + ".json"), {"stage": name})
            return value or {"root": name}, [artifact]
        def sample(source):
            nonlocal sample_calls
            sample_calls += 1
            self.assertEqual(source["root"], "download_only")
            if interrupt_first_sample:
                folder = pilot.attempt("samples")
                folder.mkdir()
                (folder / "partial.txt").write_text("preserve this attempt", encoding="utf8")
                if sample_calls == 1:
                    raise KeyboardInterrupt("simulated sample interruption")
            return emit("sample")
        def teach(fold, data):
            self.assertEqual(data["root"], "sample")
            if fold == "calibration":
                self.assertTrue((pilot.out / "stages/dev_thresholds.json").exists())
                self.assertFalse((pilot.out / "stages/teacher_test.json").exists())
            if fold == "test":
                self.assertTrue((pilot.out / "stages/calibration_confirm.json").exists())
            return emit("teacher_" + fold)
        pilot.download = lambda: emit("download_only")
        pilot.sample = sample
        pilot.teach = teach
        pilot.train = lambda *args: emit("student_train")
        pilot.predict = lambda fold, *args: emit("student_" + fold)
        pilot.tune = lambda *args: emit("dev_thresholds")
        pilot.confirm = lambda *args: emit("calibration_confirm")
        pilot.replay = lambda *args: emit("fallback_replay")
        pilot.report = lambda *args: emit("report", {"path": "fake_report", "gates": {"all_pass": True}})

    def test_stage_order_freezes_dev_before_cal_and_routes_before_test(self):
        pilot, events = self.bare(), []
        self.fake_pipeline(pilot, events)
        pilot.run()
        self.assertEqual(events, SEQUENCE)
        status = run.read(pilot.out / "status.json")
        self.assertEqual(status["state"], "PILOT_COMPLETE")
        self.assertFalse(status["production_approved"])
        self.assertEqual(status["independent_truth_audit"], "pending_separate_blind_review")
        self.assertFalse(self.subprocess.called)

    def test_resume_skips_completed_stages_and_rejects_artifact_tamper(self):
        pilot, events = self.bare(), []
        self.fake_pipeline(pilot, events)
        pilot.run()
        pilot.run()
        self.assertEqual(events, SEQUENCE)
        self.write(pilot.out / "fake_artifacts/student_dev.json", {"changed": True})
        with self.assertRaisesRegex(ValueError, "artifacts changed: student_dev"):
            pilot.run()
        self.assertEqual(events, SEQUENCE)
        self.assertEqual(run.read(pilot.out / "status.json")["state"], "FAILED")

    def test_interrupted_attempt_is_preserved_and_next_attempt_is_new(self):
        pilot, events = self.bare(), []
        self.fake_pipeline(pilot, events, interrupt_first_sample=True)
        with self.assertRaises(KeyboardInterrupt):
            pilot.run()
        self.assertTrue((pilot.out / "stages/download_only.json").exists())
        self.assertFalse((pilot.out / "stages/sample.json").exists())
        self.assertEqual(run.read(pilot.out / "status.json")["state"], "INTERRUPTED")
        first = pilot.out / "samples/attempt_001/partial.txt"
        self.assertEqual(first.read_text(encoding="utf8"), "preserve this attempt")
        pilot.run()
        self.assertEqual(events, SEQUENCE)
        self.assertEqual(first.read_text(encoding="utf8"), "preserve this attempt")
        self.assertTrue((pilot.out / "samples/attempt_002/partial.txt").exists())

    def test_child_uses_structured_args_and_appends_failed_attempt_logs(self):
        pilot = self.bare()
        self.subprocess.side_effect = [subprocess.CalledProcessError(1, "fake"), None]
        with self.assertRaises(subprocess.CalledProcessError):
            pilot.child("fake_child", "kg_cascade_student.py", ["predict", "--out", self.root / "has space"])
        pilot.child("fake_child", "kg_cascade_student.py", ["predict", "--out", self.root / "has space"])
        command = self.subprocess.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "-X", "utf8"])
        self.assertEqual(command[-1], str(self.root / "has space"))
        self.assertTrue(self.subprocess.call_args.kwargs["check"])
        self.assertNotIn("shell", self.subprocess.call_args.kwargs)
        self.assertEqual(len((pilot.out / "logs/fake_child.log").read_text(encoding="utf8").splitlines()), 2)

    def test_training_cli_uses_only_train_dev_and_checkpoint_files_exist(self):
        pilot = self.bare()
        source, teacher = self.root / "sample", self.root / "teacher"
        for fold in ("train", "dev"):
            self.jsonl(source / fold / "inputs.jsonl", [row(fold)])
            self.jsonl(teacher / fold / "results.jsonl", [{"fold": fold, "raw": "fixture"}])
            self.write(teacher / fold / "protocol.json", {"fold": fold})
        captured = {}
        def train_model(train_rows, train_results, dev_rows, dev_results, out, **kwargs):
            captured.update(kwargs)
            self.assertEqual([r["article_id"] for r in train_rows], ["train"])
            self.assertEqual([r["article_id"] for r in dev_rows], ["dev"])
            self.assertEqual(train_results[0]["fold"], "train")
            self.assertEqual(dev_results[0]["fold"], "dev")
            self.assertEqual(kwargs["config"], pilot.plan["student_config"])
            self.assertEqual(kwargs["train_protocol"], {"fold": "train"})
            self.assertEqual(kwargs["dev_protocol"], {"fold": "dev"})
            self.write(out / "plan.json", {"fixture": True})
            checkpoint = self.write(out / "checkpoint/encoder/config.json", {"fixture": True})
            self.write(out / "training_manifest.json", {"complete": True,
                "config": kwargs["config"], "encoder": pilot.plan["student_parent_encoder"],
                "checkpoint_files": {"encoder/config.json": run.transfer.sha(checkpoint)}})
        def child(name, script, args):
            self.assertEqual((name, script), ("student_train", "kg_cascade_student.py"))
            string_args = list(map(str, args))
            self.assertNotIn("--calibration-inputs", string_args)
            self.assertNotIn("--test-inputs", string_args)
            with mock.patch.object(sys, "argv", [script, *string_args]), mock.patch.object(run.student, "train_model", side_effect=train_model):
                run.student.main()
            return 1.25
        pilot.child = child
        with mock.patch.object(run, "teacher_labels", return_value=[0]) as teacher_check, mock.patch.object(
                run.student.legacy, "verified_encoder", return_value=(Path("unused"), pilot.plan["student_parent_encoder"])):
            value, files = pilot.train({"root": str(source)}, {"root": str(teacher / "train")}, {"root": str(teacher / "dev")})
        self.assertEqual(teacher_check.call_count, 2)
        self.assertTrue(all(path.is_file() for path in files))
        self.assertEqual(Path(value["root"]).name, "attempt_001")
        self.assertEqual(value["wall_seconds"], 1.25)
        self.assertEqual(captured["device"], "cuda")
        self.assertEqual(len(captured["source_files"]), 7)

    def test_prediction_cli_receives_training_root_not_checkpoint_subdirectory(self):
        pilot = self.bare()
        source, trained = self.root / "sample", self.root / "trained"
        self.write(trained / "training_manifest.json", {"fixture": True})
        self.jsonl(source / "calibration/inputs.jsonl", [row("calibration")])
        def predict(rows, checkpoint, *, device):
            self.assertEqual(Path(checkpoint), trained)
            self.assertEqual(device, "cuda")
            return [{**rows[0], "requires_fallback": True, "elapsed_seconds": .1}]
        def child(name, script, args):
            self.assertEqual(name, "student_calibration")
            with mock.patch.object(sys, "argv", [script, *map(str, args)]), mock.patch.object(run.student, "predict", side_effect=predict):
                run.student.main()
            return 2.
        pilot.child = child
        value, files = pilot.predict("calibration", {"root": str(source)}, {"root": str(trained)})
        self.assertTrue(all(path.is_file() for path in files))
        self.assertEqual(run.read(Path(value["root"]) / "summary.json")["requires_fallback"], 1)
        self.assertEqual(value["student_manifest_sha256"], run.transfer.sha(trained / "training_manifest.json"))

    def test_alignment_refuses_wrong_checkpoint_and_partial_content_scores(self):
        pilot = self.bare()
        source, predictions = self.root / "sample", self.root / "predictions"
        sample = row("calibration")
        self.jsonl(source / "calibration/inputs.jsonl", [sample])
        self.jsonl(source / "selection_trace.jsonl", [{"fold": "calibration", "article_id": sample["article_id"],
            "ticker": sample["ticker"], "group_id": "one-group"}])
        prediction = {"article_id": sample["article_id"], "ticker": sample["ticker"],
            "input_sha256": run.preview.canonical_input_sha256(sample), "student_manifest_sha256": "student-sha",
            "probabilities": {"include": .9, "exclude": .05, "review": .05},
            "requires_fallback": False, "fallback_reasons": [],
            "coverage": {"full_coverage": True, "encoding_completed": True}}
        def aligned():
            return pilot.aligned("calibration", {"root": str(source)}, {"root": "mock-teacher"},
                {"root": str(predictions), "student_manifest_sha256": "student-sha"})
        self.jsonl(predictions / "results.jsonl", [prediction])
        with mock.patch.object(run, "teacher_labels", return_value=[0]):
            self.assertEqual(aligned()[1], ["include"])
            self.jsonl(predictions / "results.jsonl", [{**prediction, "student_manifest_sha256": "other-sha"}])
            with self.assertRaisesRegex(ValueError, "different student checkpoint"):
                aligned()
            self.jsonl(predictions / "results.jsonl", [{**prediction, "coverage": {"full_coverage": False, "encoding_completed": True}}])
            with self.assertRaisesRegex(ValueError, "incompletely encoded"):
                aligned()
            self.jsonl(predictions / "results.jsonl", [{**prediction, "requires_fallback": True}])
            with self.assertRaisesRegex(ValueError, "incompletely encoded"):
                aligned()
            self.jsonl(predictions / "results.jsonl", [{**prediction, "probabilities": None, "requires_fallback": True,
                "fallback_reasons": ["unsupported_document_window_count"], "coverage": {"full_coverage": False, "encoding_completed": False}}])
            self.assertEqual(aligned()[2], [None])

    def test_teacher_results_must_bind_exact_run_protocol_hash(self):
        fake_here, folder = self.root / "teacher-code", self.root / "teacher"
        receipt = self.write(fake_here / "local_models/relevance_model_receipt.json", {"fixture": True})
        protocol = {"prompt_sha256": "prompt-fixture", "model_receipt_sha256": run.transfer.sha(receipt),
            "code_hashes": {"fixture.py": "code-fixture"}, "model_repo": "fixture", "model_revision": "revision"}
        protocol_path = self.write(folder / "protocol.json", protocol)
        bound = {"type": "local_llm_inference", "model_repo": "fixture", "model_revision": "revision",
                 "protocol_sha256": run.transfer.sha(protocol_path)}
        self.jsonl(folder / "results.jsonl", [{"selection": {"model_provenance": bound}}])
        with mock.patch.object(run, "HERE", fake_here), mock.patch.object(run.batch, "prompt_sha256", return_value="prompt-fixture"), mock.patch.object(
                run.batch, "code_hashes", return_value={"fixture.py": "code-fixture"}), mock.patch.object(run.student, "validate_teacher_labels", return_value=[0]) as replay:
            self.assertEqual(run.teacher_labels([row("fixture")], folder), [0])
            self.assertEqual(replay.call_count, 1)
            self.jsonl(folder / "results.jsonl", [{"selection": {"model_provenance": {**bound, "protocol_sha256": "0" * 64}}}])
            with self.assertRaisesRegex(ValueError, "not bound"):
                run.teacher_labels([row("fixture")], folder)
            self.assertEqual(replay.call_count, 1)

    def test_report_never_approves_production_even_when_gates_pass(self):
        pilot = self.bare()
        inputs = [row("test")]
        pilot.aligned = mock.Mock(return_value=(inputs, ["include"], [{"include": .99, "exclude": .005, "review": .005}], ["group"]))
        policy = self.write(self.root / "policy.json", {"fixture": True})
        replay = {"measured": True, "seconds": 2., "route_seconds": .1, "replay_decision_changes": 0}
        with mock.patch.object(run.routing, "evaluate_cascade", return_value={"pilot_gates": {"all_pass": True}}) as evaluate:
            value, files = pilot.report({}, {"uninterrupted_measurement": True, "wall_seconds": 10.},
                                       {"wall_seconds": 1.}, {"path": str(policy)}, replay)
        report = run.read(value["path"])
        self.assertTrue(report["pilot_gates"]["all_pass"])
        self.assertFalse(report["production_approved"])
        self.assertFalse(report["gnn_training_performed"])
        self.assertFalse(report["population_accuracy_claim"])
        self.assertEqual(evaluate.call_args.kwargs["timings"], {"student_total_seconds": 1.1,
            "teacher_all_total_seconds": 10., "fallback_teacher_total_seconds": 2.})
        self.assertEqual(files, [Path(value["path"])])

    def test_replayed_decision_change_fails_gate_and_resumed_teacher_hides_speed(self):
        pilot = self.bare()
        pilot.aligned = mock.Mock(return_value=([row("test")], ["include"], [{"include": 1., "exclude": 0., "review": 0.}], ["g"]))
        policy = self.write(self.root / "policy.json", {"fixture": True})
        with mock.patch.object(run.routing, "evaluate_cascade", return_value={"pilot_gates": {"all_pass": True}}) as evaluate:
            value, _ = pilot.report({}, {"uninterrupted_measurement": False, "wall_seconds": 10.},
                {"wall_seconds": 1.}, {"path": str(policy)},
                {"measured": True, "seconds": 2., "route_seconds": .1, "replay_decision_changes": 1})
        self.assertIsNone(evaluate.call_args.kwargs["timings"])
        self.assertFalse(run.read(value["path"])["pilot_gates"]["all_pass"])

    @contextmanager
    def frozen_fixture(self):
        fake_here = self.root / "fake_graph"
        self.write(fake_here / "local_models/relevance_model_receipt.json", {"fixture": True})
        fake_code = fake_here / "fixture.py"
        fake_code.write_text("# frozen code fixture\n", encoding="utf8")
        exclusions = self.jsonl(self.root / "exclusions/keys.jsonl", [{"kind": "id", "value": "fixture"}])
        self.write(exclusions.with_name("manifest.json"), {"complete": True, "known_articles": 750,
            "price_holdout_articles": 11969, "file": {"sha256": run.transfer.sha(exclusions)}})
        out = self.root / "frozen"
        with mock.patch.object(run, "HERE", fake_here), mock.patch.object(run, "code_hashes", side_effect=lambda: {
                "fixture.py": run.transfer.sha(fake_code)}), mock.patch.object(run.student.legacy, "verified_encoder", return_value=(Path("unused"), {"fixture": True})):
            plan = run.freeze_plan(out, "/home/ubuntu/ner-work/kg_reselection_fixture", exclusions)
            yield out, plan

    def test_frozen_plan_binds_inputs_and_disallows_auto_deployment(self):
        with self.frozen_fixture() as (out, plan):
            self.assertEqual(plan["sequence"], SEQUENCE)
            self.assertFalse(plan["production_approved"])
            self.assertFalse(plan["full_corpus_inference_authorized_by_this_plan"])
            self.assertFalse(plan["new_labels_seen"])
            pilot = run.Pilot(out)
            pilot.assert_frozen()
            self.write(out / "student_config.json", {**plan["student_config"], "epochs": 99})
            with self.assertRaisesRegex(ValueError, "Frozen experiment"):
                pilot.assert_frozen()

    def test_changed_exclusion_inventory_and_double_freeze_are_rejected(self):
        with self.frozen_fixture() as (out, plan):
            with self.assertRaises(FileExistsError):
                run.freeze_plan(out, plan["remote_run"], plan["exclusions"])
            pilot = run.Pilot(out)
            Path(plan["exclusions"]).write_text("changed\n", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "Frozen experiment"):
                pilot.assert_frozen()


if __name__ == "__main__":
    unittest.main()
