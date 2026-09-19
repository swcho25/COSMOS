"""Continue the frozen A/B pilot after B's pair-token metadata execution error.

The parent v2 plan, teacher TRAIN/DEV labels, completed A checkpoint, A DEV
predictions and threshold result are immutable inputs. A is reused, never
retrained or selected from repeated attempts. B alone uses the versioned token
metadata repair, with the same architecture, labels, four epochs and optimizer.
The known A DEV result is disclosed before freezing this continuation; CAL/TEST
must not have started. Selection, CAL confirmation, TEST denominator and timing
gates are inherited unchanged. This is an execution repair, not a new search.

Current user scope ends at retraining/validation. A successful validation still
writes full_fast_pass_allowed=false: no full-corpus, graph, GNN, service or
notification action is launched by this program. Interrupted B attempts remain
preserved; the new run follows the same fresh-attempt training policy.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import shutil
import time

import run_kg_cascade_repair as core

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-kg-cascade-pair-repair-1"
MODULES = {"A": "kg_cascade_student_v2", "B": "kg_cascade_student_v2b"}
REUSED_STAGES = ("teacher_train", "teacher_dev", "student_train_A", "student_dev_A", "dev_thresholds_A")
read, require, preview, transfer = core.read, core.require, core.preview, core.transfer


def student_module(name):
    require(name in MODULES.values(), "Unexpected candidate implementation")
    module = importlib.import_module(name)
    require(tuple(module.CLASSES) == tuple(core.student.CLASSES), "Candidate class contract changed")
    return module


def code_hashes():
    names = set(core.code_hashes()) | {Path(__file__).name}
    for name in MODULES.values():
        names.update(student_module(name).code_hashes())
    return {name: transfer.sha(HERE / name) for name in sorted(names)}


def require_unstarted_holdouts(parent_out):
    """Check only directory/receipt existence, never read held-out row contents."""
    parent_out = Path(parent_out)
    forbidden = ("teacher_calibration", "teacher_test", "student_calibration_A", "student_calibration_B",
                 "student_test_A", "student_test_B", "fallback_replay", "routing_policy.json", "evaluation_report.json",
                 "candidate_selection.json")
    require(not any((parent_out / name).exists() for name in forbidden),
            "Continuation requires the pre-CAL/TEST failure boundary")
    forbidden_stages = ("candidate_selection", "teacher_calibration", "student_calibration", "calibration_confirm",
                        "teacher_test", "student_test", "fallback_replay", "report")
    require(not any((parent_out / "stages" / (name + ".json")).exists() for name in forbidden_stages),
            "Held-out or selection stages already exist in the parent")


def parent_stage(parent, name):
    path = parent.out / "stages" / (name + ".json")
    receipt = read(path)
    require(receipt.get("complete") is True and receipt.get("stage") == name
            and receipt.get("plan_sha256") == parent.plan_sha, "Invalid parent completed stage: " + name)
    require(all(transfer.sha(path) == sha for path, sha in receipt["files"].items()),
            "Parent stage files changed: " + name)
    return {"receipt_path": str(path), "receipt_sha256": transfer.sha(path),
            "files": receipt["files"], "value": receipt["value"]}


def freeze_plan(out, parent_out):
    out, parent_out = Path(out).resolve(), Path(parent_out).resolve()
    require(out != parent_out and not (out / "experiment_plan.json").exists(), "Use a new continuation output")
    require(not any((out / name).exists() for name in ("stages", "student_B", "teacher_calibration", "teacher_test")),
            "Freeze before any continuation execution")
    require_unstarted_holdouts(parent_out)
    parent = core.Repair(parent_out)
    reused = {name: parent_stage(parent, name) for name in REUSED_STAGES}
    require(not (parent_out / "stages/student_train_B.json").exists(), "Parent B already completed")
    configs = parent.plan["candidate_configs"]
    require(set(configs) == {"A", "B"} and all(config["epochs"] == 4 for config in configs.values()),
            "Parent must preserve the original fixed A/B epoch budget")
    for candidate, name in MODULES.items():
        require(student_module(name).configuration(configs[candidate]) == configs[candidate],
                "Token repair changed the predeclared candidate configuration")
    a_root = Path(reused["student_train_A"]["value"]["root"])
    manifest = read(a_root / "training_manifest.json")
    require(manifest.get("complete") is True and manifest["config"] == configs["A"]
            and manifest["code_hashes"] == student_module(MODULES["A"]).code_hashes(),
            "Completed original A model no longer matches its original implementation")
    require(reused["student_dev_A"]["value"]["student_manifest_sha256"] == transfer.sha(a_root / "training_manifest.json"),
            "Parent A DEV predictions bind a different model")
    threshold_path = reused["dev_thresholds_A"]["value"]["path"]
    require(core.enabled(read(threshold_path)) == reused["dev_thresholds_A"]["value"]["enabled_dev_direction"],
            "Parent A DEV threshold receipt mismatch")
    bound = dict(parent.plan["source_file_sha256"])
    for stage in reused.values():
        bound.update(stage["files"])
        bound[stage["receipt_path"]] = stage["receipt_sha256"]
    for name in ("experiment_plan.json", "plan_receipt.json", "student_config_A.json", "student_config_B.json"):
        path = parent_out / name
        bound[str(path)] = transfer.sha(path)
    plan = copy.deepcopy(parent.plan)
    plan.update(schema=SCHEMA, frozen_at=datetime.now(timezone.utc).isoformat(),
        parent_root=str(parent_out), parent_plan_sha256=parent.plan_sha,
        source_file_sha256=bound, reused_parent_stages=reused, candidate_modules=MODULES,
        code_sha256=code_hashes(), repair_scope="B_pair_sequence_metadata_only_not_architecture_or_hyperparameters",
        original_A_dev_result_observed_before_repair=True,
        original_A_dev_enabled_direction=reused["dev_thresholds_A"]["value"]["enabled_dev_direction"],
        original_A_dev_log_loss=reused["student_train_A"]["value"]["dev_log_loss"],
        original_B_completed_training=False, original_B_failure="tokenizer_exact_pair_coverage_assertion",
        parent_freeze_observation_history={key: parent.plan.get(key) for key in (
            "teacher_train_dev_full_relabeling_started", "calibration_test_labels_seen", "sequence")},
        teacher_train_dev_full_relabeling_started=True, teacher_train_dev_labels_complete_and_reused=True,
        A_training_complete_and_reused=True,
        sequence=[*["reuse_parent_" + name for name in REUSED_STAGES], "train_corrected_B", "predict_dev_B",
            "dev_thresholds_B", "candidate_selection", "teacher_calibration", "student_calibration",
            "calibration_confirm", "teacher_test", "student_test", "fallback_replay", "report"],
        calibration_test_started_before_repair=False, calibration_test_labels_seen=False,
        user_scope="news_selector_retraining_and_validation_only", full_fast_pass_authorized=False,
        full_corpus_auto_launch=False, notifications_authorized=False)
    out.mkdir(parents=True, exist_ok=True)
    frozen = out / "frozen_code"
    frozen.mkdir(exist_ok=False)
    for name in plan["code_sha256"]:
        shutil.copy2(HERE / name, frozen / name)
    for name, config in configs.items():
        transfer.atomic_json(out / ("student_config_" + name + ".json"), config)
    require(code_hashes() == plan["code_sha256"]
            and all(transfer.sha(frozen / name) == sha for name, sha in plan["code_sha256"].items())
            and all(transfer.sha(path) == sha for path, sha in bound.items()), "Inputs/code changed while freezing continuation")
    require_unstarted_holdouts(parent_out)
    transfer.atomic_json(out / "experiment_plan.json", plan)
    transfer.atomic_json(out / "plan_receipt.json", {"plan_sha256": transfer.sha(out / "experiment_plan.json")})
    return plan


def predict_once(inputs, checkpoint, out, module_name, *, device="cuda"):
    module = student_module(module_name)
    inputs, checkpoint, out = Path(inputs).resolve(), Path(checkpoint).resolve(), Path(out).resolve()
    require(not out.exists(), "Prediction output must be new")
    source_sha = transfer.sha(inputs)
    rows = preview.read_jsonl(inputs)
    require(rows and source_sha == transfer.sha(inputs), "Empty or changing prediction inputs")
    before = code_hashes()
    started = time.perf_counter()
    predictor = module.StudentPredictor(checkpoint, device=device)
    try:
        if str(device).startswith("cuda"):
            predictor.torch.cuda.synchronize()
        setup = time.perf_counter() - started
        started = time.perf_counter()
        predictions = predictor.predict(rows)
        elapsed = time.perf_counter() - started
        model_sha = predictor.manifest_sha256
    finally:
        predictor.close()
    require(len(rows) == len(predictions) and all(preview.pair_key(row) == preview.pair_key(pred)
            and pred["input_sha256"] == preview.canonical_input_sha256(row)
            and pred["student_manifest_sha256"] == model_sha for row, pred in zip(rows, predictions)),
            "Prediction source/model binding differs")
    require(before == code_hashes() and source_sha == transfer.sha(inputs), "Prediction source/code changed")
    summary = {"complete": True, "pairs": len(rows), "input_sha256": source_sha, "student_module": module_name,
        "student_manifest_sha256": model_sha, "requires_fallback": sum(p["requires_fallback"] for p in predictions),
        "inference_seconds": elapsed, "setup_seconds": setup, "production_approved": False,
        "timing_scope": "one model load and one complete predictor call; process startup/export IO excluded"}
    core.bulk_timing(summary, len(rows))
    out.mkdir(parents=True, exist_ok=False)
    with (out / "results.jsonl").open("x", encoding="utf8", newline="\n") as stream:
        for prediction in predictions:
            stream.write(preview.canonical(prediction) + "\n")
    transfer.atomic_json(out / "summary.json", summary)
    return summary


class PairRepair(core.Repair):
    def assert_frozen(self):
        p = self.plan
        require(p.get("schema") == SCHEMA and transfer.sha(self.out / "experiment_plan.json") == self.plan_sha,
                "Continuation plan changed")
        require(code_hashes() == p["code_sha256"] and p["candidate_modules"] == MODULES,
                "Continuation implementation changed")
        require(all(transfer.sha(path) == sha for path, sha in p["source_file_sha256"].items()),
                "Parent completed stages/source bytes changed")
        require(transfer.sha(HERE / "local_models/relevance_model_receipt.json") == p["model_receipt_sha256"],
                "Teacher model receipt changed")
        for name, config in p["candidate_configs"].items():
            require(read(self.out / ("student_config_" + name + ".json")) == config,
                    "Original candidate configuration changed")
        if p["challenge_receipt"] is not None:
            require(core.challenge_binding(p["challenge_receipt"]["path"]) == p["challenge_receipt"],
                    "Teacher development challenge binding changed")
        require(p["user_scope"] == "news_selector_retraining_and_validation_only"
                and p["full_fast_pass_authorized"] is False, "Latest user scope changed")

    def status(self, state, **extra):
        transfer.atomic_json(self.out / "status.json", {"schema": SCHEMA, "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "plan_sha256": self.plan_sha,
            "human_gold": False, "production_approved": False, "full_corpus_selection_complete": False,
            "full_fast_pass_allowed": False, "notifications_authorized": False, **extra})

    def reuse(self, name):
        bound = self.plan["reused_parent_stages"][name]
        require(transfer.sha(bound["receipt_path"]) == bound["receipt_sha256"]
                and all(transfer.sha(path) == sha for path, sha in bound["files"].items()), "Reused parent stage changed")
        receipt = read(bound["receipt_path"])
        require(receipt["value"] == bound["value"] and receipt["plan_sha256"] == self.plan["parent_plan_sha256"],
                "Reused stage value or parent plan differs")
        value = {**bound["value"], "reused_parent_receipt": bound["receipt_path"]}
        if name in ("student_train_A", "student_dev_A"):
            value["student_module"] = MODULES["A"]
        return value, [Path(bound["receipt_path"]), *[Path(path) for path in bound["files"]]]

    def train(self, name, train_labels, dev_labels):
        require(name == "B", "Completed A must be reused, never retrained")
        module_name = self.plan["candidate_modules"][name]
        module = student_module(module_name)
        require(module.legacy.verified_encoder(module.legacy.BUNDLE)[1] == self.plan["student_parent_encoder"],
                "Original parent encoder changed")
        folder = self.attempt("student_B")
        args = ["train", "--out", folder, "--device", "cuda", "--config", self.out / "student_config_B.json"]
        for fold, labels in (("train", train_labels), ("dev", dev_labels)):
            core.teacher_labels(preview.read_jsonl(self.inputs(fold)), self.inputs(fold), labels["root"])
            args += ["--" + fold + "-inputs", self.inputs(fold), "--" + fold + "-results",
                     Path(labels["root"]) / "results.jsonl", "--" + fold + "-protocol", Path(labels["root"]) / "protocol.json"]
        seconds = self.child("student_train_B", module_name + ".py", args)
        manifest = read(folder / "training_manifest.json")
        require(manifest.get("complete") is True and manifest["config"] == self.plan["candidate_configs"][name]
                and manifest["encoder"] == self.plan["student_parent_encoder"]
                and manifest["code_hashes"] == module.code_hashes(), "Corrected B training manifest differs")
        selected = [trial for trial in manifest["trials"] if trial["epoch"] == manifest["selected_epoch"]]
        require(len(selected) == 1, "Selected B epoch invalid")
        return {"root": str(folder), "candidate": name, "student_module": module_name,
                "wall_seconds": seconds, "dev_log_loss": selected[0]["model_dev_log_loss"]}, [folder / "training_manifest.json",
                folder / "plan.json", *[folder / "checkpoint" / path for path in manifest["checkpoint_files"]]]

    def predict(self, fold, trained):
        module_name = self.plan["candidate_modules"][trained["candidate"]]
        require(trained["student_module"] == module_name, "Selected candidate implementation mismatch")
        name = "student_" + fold + "_" + trained["candidate"]
        folder = self.attempt(name)
        seconds = self.child(name, Path(__file__).name, ["--mode", "predict_once", "--inputs", self.inputs(fold),
            "--checkpoint", trained["root"], "--out", folder, "--student-module", module_name, "--device", "cuda"])
        summary = read(folder / "summary.json")
        require(summary.get("complete") is True and summary["input_sha256"] == transfer.sha(self.inputs(fold))
                and summary["pairs"] == self.plan["sample_sizes"][fold] and summary["student_module"] == module_name,
                "Continuation prediction source/module mismatch")
        return {"root": str(folder), "candidate": trained["candidate"], "student_module": module_name,
                "wall_seconds": seconds, "student_bulk_timing": core.bulk_timing(summary, self.plan["sample_sizes"][fold]),
                "student_manifest_sha256": transfer.sha(Path(trained["root"]) / "training_manifest.json")}, [
                    folder / "results.jsonl", folder / "summary.json"]

    def aligned(self, fold, labels, predictions):
        require(predictions.get("student_module") == self.plan["candidate_modules"][predictions["candidate"]],
                "Prediction implementation is not the selected candidate")
        return super().aligned(fold, labels, predictions)

    def release(self, report, report_path, trained=None, policy=None):
        """Always deny full routing under the current retraining-only user scope."""
        module = trained.get("student_module") if trained else None
        model_sha = transfer.sha(Path(trained["root"]) / "training_manifest.json") if trained else None
        policy_value = read(policy["path"]) if policy else None
        require("rows" not in report and "labels" not in report, "Validation report must be aggregate only")
        if trained:
            require(module == self.plan["candidate_modules"][trained["candidate"]], "Release candidate module mismatch")
        if report.get("pilot_gates", {}).get("all_pass") is True:
            require(report.get("total_pairs") == core.SIZES["test"] and model_sha is not None and policy_value is not None
                    and report.get("student_manifest_sha256") == model_sha
                    and report.get("policy_sha256") == policy_value["policy_sha256"],
                    "Passing validation must bind all TEST pairs, model and policy")
            core.bulk_timing(report.get("student_bulk_timing", {}), report["total_pairs"])
            timing = report.get("timing_comparison", {})
            seconds = [timing.get(name) for name in ("student_total_seconds", "teacher_all_total_seconds", "fallback_teacher_total_seconds")]
            require(all(core._finite_seconds(value) for value in seconds) and seconds[0] > 0
                    and seconds[1] > seconds[0] + seconds[2], "Passing validation needs measured same-workload speed improvement")
        report = {**report, "student_module": module, "continuation_parent_plan_sha256": self.plan["parent_plan_sha256"],
                  "implementation_repair": self.plan["repair_scope"], "original_A_dev_result_observed_before_repair": True,
                  "full_fast_pass_allowed": False, "user_scope": self.plan["user_scope"]}
        transfer.atomic_json(report_path, report)
        value = {"schema": core.RELEASE_SCHEMA, "complete": True, "full_fast_pass_allowed": False,
            "denial_reason": "user_authorized_selector_retraining_validation_only",
            "plan_sha256": self.plan_sha, "parent_plan_sha256": self.plan["parent_plan_sha256"],
            "source_manifest_sha256": self.plan["full_source_manifest_sha256"], "student_module": module,
            "candidate_modules": self.plan["candidate_modules"], "implementation_repair": self.plan["repair_scope"],
            "student_manifest_sha256": model_sha,
            "routing_policy_file_sha256": transfer.sha(policy["path"]) if policy else None,
            "routing_policy_sha256": read(policy["path"])["policy_sha256"] if policy else None,
            "validation_report": {"path": Path(report_path).name, "sha256": transfer.sha(report_path)},
            "challenge_receipt": self.plan["challenge_receipt"], "factual_accuracy_established": False,
            "full_corpus_selection_complete": False, "full_corpus_auto_launched": False,
            "notifications_authorized": False}
        transfer.atomic_json(self.out / "release_receipt.json", value)
        return value

    def run(self):
        with transfer.CoordinatorLock(self.out / ".pair_repair.lock"):
            try:
                reused = {name: self.stage(name, lambda name=name: self.reuse(name)) for name in REUSED_STAGES}
                tr, dv = reused["teacher_train"], reused["teacher_dev"]
                b = self.stage("student_train_B", lambda: self.train("B", tr, dv))
                bp = self.stage("student_dev_B", lambda: self.predict("dev", b))
                bt = self.stage("dev_thresholds_B", lambda: self.tune("B", dv, bp))
                candidates = {"A": {"trained": reused["student_train_A"], "predictions": reused["student_dev_A"],
                                     "thresholds": reused["dev_thresholds_A"]},
                              "B": {"trained": b, "predictions": bp, "thresholds": bt}}
                selection = self.stage("candidate_selection", lambda: self.select(candidates))
                if selection["selected"] is None:
                    self.quality_failure("dev", "Neither original A nor technically repaired B enabled a DEV direction")
                    return
                chosen = candidates[selection["selected"]]
                trained = chosen["trained"]
                cal = self.stage("teacher_calibration", lambda: self.teach("calibration"))
                cp = self.stage("student_calibration", lambda: self.predict("calibration", trained))
                policy = self.stage("calibration_confirm", lambda: self.confirm(cal, cp, chosen["thresholds"]))
                if not policy["enabled_direction"]:
                    self.quality_failure("calibration", "Frozen DEV candidate failed independent CAL confirmation")
                    return
                test = self.stage("teacher_test", lambda: self.teach("test"))
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
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--student-module", choices=tuple(MODULES.values()))
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    if args.mode == "freeze":
        if args.parent_out is None:
            parser.error("freeze requires --parent-out")
        freeze_plan(args.out, args.parent_out)
    elif args.mode == "run":
        PairRepair(args.out).run()
    else:
        if args.inputs is None or args.checkpoint is None or args.student_module is None:
            parser.error("predict_once requires --inputs, --checkpoint, --student-module")
        predict_once(args.inputs, args.checkpoint, args.out, args.student_module, device=args.device)
