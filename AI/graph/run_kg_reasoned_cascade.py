"""Freeze and evaluate one revised news selector using reasoned pseudo-labels.

The old two-pass teacher, A/B models, plans and results remain immutable. This
experiment reuses only the grouped source split. Full TRAIN/DEV labels are made
again with a new teacher; previously exposed development cases are disclosed.
One B-shaped student uses lexical name hints, LR 1e-5, dropout .2 and four epochs.
Independent CAL/TEST remain sealed until the unchanged preceding gate passes.
All source pairs remain in the evaluation denominator; execution failures are
masked only in training and counted separately. At most 5% unusable teacher
executions are tolerated in any completed fold. This does not make valid labels
human truth. No whole-corpus processing, graph, service or automation is started.
Completed stages resume after integrity checks. An interrupted student starts a
new attempt from the pretrained encoder; optimizer state is not resumed.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import sys

import kg_reasoned_teacher_v3 as teacher
import kg_reasoned_student as student
import kg_target_aliases as aliases
import run_kg_cascade_repair as original

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-reasoned-selector-experiment-1"
MAX_UNUSABLE_FRACTION = .05
# Reuse proven fold alignment, timing and statistical gates in a private module.
# Changing these bindings does not mutate the original experiment's loader.
_spec = importlib.util.spec_from_file_location("_cosmos_reasoned_runner_core", HERE / "run_kg_cascade_repair.py")
core = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = core
_spec.loader.exec_module(core)
core.teacher, core.student = teacher, student
read, require, transfer, preview = core.read, core.require, core.transfer, core.preview


def code_hashes():
    names = set(original.code_hashes()) | set(teacher.code_hashes()) | set(student.code_hashes())
    names.update((Path(__file__).name, "kg_target_aliases.py"))
    # Data dependencies are bound separately; never copy ../ paths into snapshots.
    return {name: transfer.sha(HERE / name) for name in sorted(names)
            if Path(name).name == name and name.endswith(".py")}


core.code_hashes = code_hashes


def challenge_binding(path):
    path = Path(path).resolve()
    value = read(path)
    require(value.get("accepted") is True and value.get("teacher_version") == teacher.VERSION
            and value.get("teacher_code_hashes") == teacher.code_hashes()
            and value.get("teacher_prompt_sha256") == teacher.digest(teacher.CRITERIA)
            and value.get("alias_provenance") == aliases.provenance(), "Development challenge is not current/accepted")
    require(value.get("human_gold") is False and value.get("factual_accuracy_established") is False
            and value.get("reference_kind") == original.CHALLENGE_REFERENCE,
            "Development observations cannot establish independent factual accuracy")
    require(value.get("bound_files") and all(transfer.sha(p) == h for p, h in value["bound_files"].items()),
            "Development inputs/results/reference changed")
    return {"path": str(path), "sha256": transfer.sha(path), "accepted": True,
            "reference_kind": original.CHALLENGE_REFERENCE, "factual_accuracy_established": False}


def freeze_plan(out, parent_out, challenge):
    out, parent_out = Path(out).resolve(), Path(parent_out).resolve()
    require(out != parent_out and not (out / "experiment_plan.json").exists(), "Use a new experiment directory")
    require(not any((out / name).exists() for name in ("stages", "teacher_train", "teacher_dev", "student_B")),
            "Freeze must precede experiment labeling/training")
    parent = original.Repair(parent_out)
    # Existence only: no held-out rows or labels are opened for development.
    for name in ("teacher_calibration", "teacher_test", "candidate_selection.json", "evaluation_report.json"):
        require(not (parent_out / name).exists(), "Use the preserved pre-CAL original v2 parent")
    bound = dict(parent.plan["source_file_sha256"])
    for name in ("experiment_plan.json", "plan_receipt.json"):
        path = parent_out / name
        bound[str(path)] = transfer.sha(path)
    bound[str(aliases.ALIAS_FILE)] = transfer.sha(aliases.ALIAS_FILE)
    config = student.configuration()
    require(config["candidate"] == "B" and config["epochs"] == 4
            and config["learning_rate"] == 1e-5 and config["head_learning_rate"] == 3e-4
            and config["head_dropout"] == .2 and config["label_policy"] == teacher.VERSION,
            "Unexpected preregistered student configuration")
    plan = copy.deepcopy(parent.plan)
    plan.update(schema=SCHEMA, frozen_at=datetime.now(timezone.utc).isoformat(),
        parent_root=str(parent_out), parent_plan_sha256=parent.plan_sha, source_file_sha256=bound,
        candidate_configs={"B": config}, candidate_modules={"B": "kg_reasoned_student"},
        candidate_selection="single_preregistered_B_shape_then_unchanged_DEV_gate",
        teacher_version=teacher.VERSION, teacher_prompt_sha256=teacher.digest(teacher.CRITERIA),
        alias_provenance=aliases.provenance(), code_sha256=code_hashes(),
        challenge_receipt=challenge_binding(challenge),
        teacher_train_dev_full_relabeling_started=False, calibration_test_labels_seen=False,
        maximum_unusable_teacher_fraction=MAX_UNUSABLE_FRACTION,
        old_train_dev_labels_and_predictions_seen_for_diagnosis=True,
        old_teacher_labels_reused=False, old_student_weights_reused=False,
        intervention="reasoned_target_event_teacher_lexical_hints_and_regularized_student",
        attribution="combined_development_intervention_not_isolated_causal_ablation",
        quality_target="agreement_with_reasoned_pseudo_labels_not_human_accuracy",
        sequence=["teacher_train", "teacher_dev", "student_train_B", "student_dev_B", "dev_thresholds_B",
                  "teacher_calibration", "student_calibration", "calibration_confirm", "teacher_test",
                  "student_test", "fallback_replay", "report"],
        user_scope="news_selector_retraining_and_validation_only", full_fast_pass_authorized=False,
        full_corpus_auto_launch=False, notifications_authorized=False)
    out.mkdir(parents=True, exist_ok=True)
    frozen = out / "frozen_code"
    frozen.mkdir(exist_ok=False)
    for name in plan["code_sha256"]:
        shutil.copy2(HERE / name, frozen / name)
    transfer.atomic_json(out / "student_config_B.json", config)
    require(code_hashes() == plan["code_sha256"]
            and all(transfer.sha(frozen / name) == h for name, h in plan["code_sha256"].items())
            and all(transfer.sha(p) == h for p, h in bound.items()), "Sources/code changed during freezing")
    require(challenge_binding(challenge) == plan["challenge_receipt"], "Challenge changed during freezing")
    transfer.atomic_json(out / "experiment_plan.json", plan)
    transfer.atomic_json(out / "plan_receipt.json", {"plan_sha256": transfer.sha(out / "experiment_plan.json")})
    return plan


class ReasonedRun(core.Repair):
    def assert_frozen(self):
        p = self.plan
        require(p.get("schema") == SCHEMA and transfer.sha(self.out / "experiment_plan.json") == self.plan_sha,
                "Reasoned experiment plan changed")
        require(code_hashes() == p["code_sha256"]
                and all(transfer.sha(path) == h for path, h in p["source_file_sha256"].items()),
                "Bound source/code changed")
        require(aliases.provenance() == p["alias_provenance"]
                and transfer.sha(HERE / "local_models/relevance_model_receipt.json") == p["model_receipt_sha256"],
                "Teacher model or alias provenance changed")
        require(read(self.out / "student_config_B.json") == p["candidate_configs"]["B"]
                and student.configuration() == p["candidate_configs"]["B"], "Student configuration changed")
        require(challenge_binding(p["challenge_receipt"]["path"]) == p["challenge_receipt"], "Challenge changed")
        require(p["routing_config"] == core.routing.DEFAULT_CONFIG
                and p["sample_sizes"] == core.SIZES and p["maximum_unusable_teacher_fraction"] == MAX_UNUSABLE_FRACTION,
                "Frozen quality gates or denominator changed")
        require(p["full_fast_pass_authorized"] is False and p["notifications_authorized"] is False,
                "User scope changed")

    def status(self, state, **extra):
        transfer.atomic_json(self.out / "status.json", {"schema": SCHEMA, "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "plan_sha256": self.plan_sha,
            "human_gold": False, "production_approved": False, "full_corpus_selection_complete": False,
            "full_fast_pass_allowed": False, "notifications_authorized": False, **extra})

    def child(self, name, script, args):
        remap = {"kg_semantic_teacher.py": "kg_reasoned_teacher_v3.py",
                 "kg_cascade_student_v2.py": "kg_reasoned_student.py",
                 "run_kg_cascade_repair.py": Path(__file__).name}
        return super().child(name, remap.get(script, script), args)

    def train(self, name, train_labels, dev_labels):
        require(name == "B", "Only the preregistered reasoned student is allowed")
        value, files = super().train(name, train_labels, dev_labels)
        manifest = read(Path(value["root"]) / "training_manifest.json")
        require(manifest["code_hashes"] == student.code_hashes(), "Student code provenance differs")
        value["student_module"] = "kg_reasoned_student"
        return value, files

    def labels_usable(self, label_stage, fold):
        return label_stage["unusable_teacher_pairs"] / self.plan["sample_sizes"][fold] <= MAX_UNUSABLE_FRACTION

    def release(self, report, report_path, trained=None, policy=None):
        module = "kg_reasoned_student" if trained else None
        model_sha = transfer.sha(Path(trained["root"]) / "training_manifest.json") if trained else None
        require("rows" not in report and "labels" not in report, "Report must contain aggregates only")
        report = {**report, "student_module": module, "teacher_version": teacher.VERSION,
                  "alias_provenance": self.plan["alias_provenance"], "full_fast_pass_allowed": False,
                  "user_scope": self.plan["user_scope"], "factual_accuracy_established": False}
        if "timing_scope" in report:
            report["timing_scope"] = ("same reasoned full-article teacher CLI for full TEST baseline and fallback replay; "
                "sequential wall times include model loading and IO; graph construction excluded")
        transfer.atomic_json(report_path, report)
        value = {"schema": core.RELEASE_SCHEMA, "complete": True, "full_fast_pass_allowed": False,
            "denial_reason": "user_authorized_selector_retraining_validation_only",
            "plan_sha256": self.plan_sha, "source_manifest_sha256": self.plan["full_source_manifest_sha256"],
            "student_module": module, "student_manifest_sha256": model_sha,
            "routing_policy_file_sha256": transfer.sha(policy["path"]) if policy else None,
            "routing_policy_sha256": read(policy["path"])["policy_sha256"] if policy else None,
            "validation_report": {"path": Path(report_path).name, "sha256": transfer.sha(report_path)},
            "factual_accuracy_established": False, "full_corpus_selection_complete": False,
            "full_corpus_auto_launched": False, "notifications_authorized": False}
        transfer.atomic_json(self.out / "release_receipt.json", value)
        return value

    def quality_failure(self, stage, reason):
        test_started = stage == "teacher_test" or (self.out / "teacher_test").exists()
        report = {"state": "QUALITY_GATE_FAILED", "stage": stage, "reason": reason,
            "plan_sha256": self.plan_sha, "teacher_version": teacher.VERSION,
            "pilot_gates": {"all_pass": False}, "heldout_test_evaluated": False,
            "heldout_test_inputs_opened": test_started, "heldout_test_teacher_started": test_started,
            "human_gold": False, "factual_accuracy_established": False,
            "full_corpus_selection_complete": False, "full_fast_pass_allowed": False}
        path = self.out / "evaluation_report.json"
        self.release(report, path)
        self.status("QUALITY_GATE_FAILED", stage=stage, reason=reason, report=str(path))

    def run(self):
        with transfer.CoordinatorLock(self.out / ".reasoned_run.lock"):
            try:
                labels = {}
                for fold in ("train", "dev"):
                    labels[fold] = self.stage("teacher_" + fold, lambda fold=fold: self.teach(fold))
                    if not self.labels_usable(labels[fold], fold):
                        self.quality_failure("teacher_" + fold, "More than 5% unusable teacher executions")
                        return
                trained = self.stage("student_train_B", lambda: self.train("B", labels["train"], labels["dev"]))
                pred = self.stage("student_dev_B", lambda: self.predict("dev", trained))
                thresholds = self.stage("dev_thresholds_B", lambda: self.tune("B", labels["dev"], pred))
                if not thresholds["enabled_dev_direction"]:
                    self.quality_failure("dev", "Reasoned student did not satisfy the unchanged DEV routing criteria")
                    return
                cal = self.stage("teacher_calibration", lambda: self.teach("calibration"))
                if not self.labels_usable(cal, "calibration"):
                    self.quality_failure("teacher_calibration", "More than 5% unusable CAL teacher executions")
                    return
                cp = self.stage("student_calibration", lambda: self.predict("calibration", trained))
                policy = self.stage("calibration_confirm", lambda: self.confirm(cal, cp, thresholds))
                if not policy["enabled_direction"]:
                    self.quality_failure("calibration", "Frozen DEV policy failed independent CAL confirmation")
                    return
                test = self.stage("teacher_test", lambda: self.teach("test"))
                if not self.labels_usable(test, "test"):
                    self.quality_failure("teacher_test", "More than 5% unusable TEST teacher executions")
                    return
                tp = self.stage("student_test", lambda: self.predict("test", trained))
                replay = self.stage("fallback_replay", lambda: self.replay(test, tp, policy))
                report = self.stage("report", lambda: self.report(test, tp, policy, replay, trained))
                self.status("PILOT_COMPLETE" if report["gates"]["all_pass"] else "QUALITY_GATE_FAILED", report=report)
            except BaseException as exc:
                self.status("INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
                            error={"type": type(exc).__name__, "message": str(exc)})
                raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("freeze", "run", "predict_once"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parent-out", type=Path)
    parser.add_argument("--challenge-receipt", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    if args.mode == "freeze":
        if args.parent_out is None or args.challenge_receipt is None:
            parser.error("freeze requires parent-out and challenge-receipt")
        freeze_plan(args.out, args.parent_out, args.challenge_receipt)
    elif args.mode == "run":
        ReasonedRun(args.out).run()
    else:
        if args.inputs is None or args.checkpoint is None:
            parser.error("predict_once requires inputs/checkpoint")
        core.predict_once(args.inputs, args.checkpoint, args.out, device=args.device)
