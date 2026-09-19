"""Run the separately frozen v2 semantic-teacher/student decision pilot.

Reuses only the immutable v1 grouped source sample, never its labels or models.
The fixed sequence is new teacher TRAIN/DEV, two predeclared student candidates,
DEV choice, fresh semantic CAL confirmation, then sealed TEST and measured
fallback replay. No viable DEV route stops before CAL; failed CAL stops before
TEST. Every evaluation source pair remains in the denominator. A failed teacher
execution is an explicit reference abstention (review), not a negative label.

This pilot measures consistency with a NEW pseudo-label protocol, not human
accuracy or parity with the old evidence-generating teacher. A bound, accepted
development challenge receipt plus all unchanged quality/speed gates can issue
a release for the separate provisional full-source fast pass. Nothing here
launches that pass, publishes a graph, runs GNN training, or modifies a service.

GPU subprocesses run sequentially. Teacher SQLite checkpoints resume; completed
stage receipts are verified and reused. Interrupted training/prediction attempts
are preserved and restarted in a new directory (optimizer resume is not claimed).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import time

import kg_cascade_routing as routing
import kg_cascade_student_v2 as student
import kg_semantic_teacher as teacher
import knowledge_graph_news_preview as preview
import run_kg_cascade_pilot as original
import transfer_kg_reselection as transfer

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-kg-cascade-repair-2"
RELEASE_SCHEMA = "cosmos-kg-cascade-release-1"
SIZES = {"train": 1536, "dev": 256, "calibration": 768, "test": 512}
CHALLENGE_REFERENCE = "synthetic_and_ai_development_challenges_not_independent_gold"


def read(path):
    return original.read(path)


def require(value, message):
    if not value:
        raise ValueError(message)


def code_hashes():
    names = set(original.code_hashes()) | set(student.code_hashes()) | set(teacher.code_hashes()) | {
        Path(__file__).name, "kg_cascade_routing.py"}
    return {name: transfer.sha(HERE / name) for name in sorted(names)}


def enabled(candidate, *, calibrated=False):
    key = "enabled" if calibrated else "enabled_candidate"
    return any(rule.get(key) is True for rule in candidate.get("directions", {}).values())


def reference_labels(values):
    """Keep failed teacher rows as reference abstentions in the full denominator."""
    require(all(value is None or (type(value) is int and 0 <= value < len(student.CLASSES))
                for value in values), "Unexpected semantic teacher class index")
    return ["review" if value is None else student.CLASSES[value] for value in values]


def choose_candidate(candidates):
    """Preregistered ranking: viable DEV direction, then selected log loss, then A."""
    require(set(candidates) == {"A", "B"}, "Exactly the predeclared A/B candidates are required")
    for name, value in candidates.items():
        require(type(value["dev_log_loss"]) in (float, int) and math.isfinite(value["dev_log_loss"])
                and value["dev_log_loss"] >= 0 and type(value["enabled_dev_direction"]) is bool,
                "Invalid candidate metrics: " + name)
    ranked = sorted(candidates, key=lambda name: (not candidates[name]["enabled_dev_direction"],
                    candidates[name]["dev_log_loss"], name))
    return ranked[0] if candidates[ranked[0]]["enabled_dev_direction"] else None


def challenge_binding(path):
    if path is None:
        return None
    path = Path(path).resolve()
    value = read(path)
    require(type(value.get("accepted")) is bool, "Challenge receipt needs an explicit acceptance decision")
    require(value.get("teacher_version") == teacher.VERSION
            and value.get("teacher_code_hashes") == teacher.code_hashes()
            and value.get("teacher_prompt_sha256") == teacher.digest({"criteria": teacher.CRITERIA, "steps": teacher.STEPS}),
            "Challenge receipt is not bound to the frozen semantic teacher")
    require(value.get("human_gold") is False and value.get("factual_accuracy_established") is False
            and value.get("reference_kind") == CHALLENGE_REFERENCE,
            "Development challenges cannot establish independent factual accuracy")
    return {"path": str(path), "sha256": transfer.sha(path), "accepted": value["accepted"],
            "reference_kind": CHALLENGE_REFERENCE, "factual_accuracy_established": False}


def _verified_parent_stage(parent, name):
    path = parent.out / "stages" / (name + ".json")
    receipt = read(path)
    require(receipt.get("complete") is True and receipt.get("stage") == name
            and receipt.get("plan_sha256") == parent.plan_sha,
            "Missing or unbound parent source stage: " + name)
    require(all(transfer.sha(path) == sha for path, sha in receipt["files"].items()),
            "Parent source stage bytes changed: " + name)
    return receipt, {str(path): transfer.sha(path), **receipt["files"]}


def freeze_plan(out, parent_out, challenge_receipt=None):
    out, parent_out = Path(out).resolve(), Path(parent_out).resolve()
    require(out != parent_out and not (out / "experiment_plan.json").exists(), "Use a new repair experiment")
    require(not any((out / name).exists() for name in
        ("teacher_train", "teacher_dev", "teacher_calibration", "teacher_test", "student_A", "student_B", "stages")),
        "Freeze must precede teacher labels and candidate training")
    parent = original.Pilot(parent_out)
    sample, sample_files = _verified_parent_stage(parent, "sample")
    source, source_files = _verified_parent_stage(parent, "download_only")
    require(parent.plan["sample_sizes"] == SIZES, "Unexpected parent grouped sample sizes")
    data = Path(sample["value"]["root"]).resolve()
    require(read(data / "manifest.json").get("complete") is True
            and read(data / "manifest.json").get("sizes") == SIZES, "Parent sample incomplete")
    candidates = {name: student.configuration(config) for name, config in student.PREDECLARED_CANDIDATES.items()}
    require(set(candidates) == {"A", "B"} and all(config["epochs"] == 4
            and config["label_policy"] == teacher.VERSION for config in candidates.values()),
            "Only the predeclared four-epoch semantic-teacher candidates are allowed")
    bound_files = {**sample_files, **source_files,
        str(parent_out / "experiment_plan.json"): transfer.sha(parent_out / "experiment_plan.json"),
        str(parent_out / "plan_receipt.json"): transfer.sha(parent_out / "plan_receipt.json")}
    challenge = challenge_binding(challenge_receipt)
    plan = {"schema": SCHEMA, "frozen_at": datetime.now(timezone.utc).isoformat(),
        "parent_root": str(parent_out), "parent_plan_sha256": parent.plan_sha,
        "source_sample_root": str(data), "sample_sizes": SIZES, "source_file_sha256": bound_files,
        "full_source_manifest": source["value"]["manifest"],
        "full_source_manifest_sha256": transfer.sha(source["value"]["manifest"]),
        "candidate_configs": candidates, "candidate_selection": "enabled_DEV_direction_then_min_selected_DEV_logloss_then_A",
        "routing_config": routing.DEFAULT_CONFIG, "teacher_version": teacher.VERSION,
        "teacher_prompt_sha256": teacher.digest({"criteria": teacher.CRITERIA, "steps": teacher.STEPS}),
        "model_receipt_sha256": transfer.sha(HERE / "local_models/relevance_model_receipt.json"),
        "student_parent_encoder": student.legacy.verified_encoder(student.legacy.BUNDLE)[1],
        "challenge_receipt": challenge, "code_sha256": code_hashes(),
        "teacher_failure_reference": "review_abstention_keep_all_source_pairs",
        "teacher_failure_training": "mask_unusable_label_and_record_count",
        "sequence": ["teacher_train", "teacher_dev", "student_A", "student_B", "candidate_selection",
                     "teacher_calibration", "student_calibration", "calibration_confirm",
                     "teacher_test", "student_test", "fallback_replay", "report"],
        "quality_target": "agreement_with_new_semantic_pseudo_label_teacher_not_old_grounded_policy",
        "train_dev_development_cases_seen": True,
        "teacher_train_dev_full_relabeling_started": False,
        "calibration_test_labels_seen": False,
        "pre_freeze_development_challenges": challenge,
        "v1_train_dev_labels_used_for_failure_diagnosis": True,
        "human_gold": False, "factual_accuracy_established": False,
        "grounded_old_protocol_parity": False, "graph_materialization_ready": False,
        "full_corpus_auto_launch": False, "service_published": False, "gnn_training_performed": False}
    out.mkdir(parents=True, exist_ok=True)
    frozen = out / "frozen_code"
    frozen.mkdir(exist_ok=False)
    for name in plan["code_sha256"]:
        shutil.copy2(HERE / name, frozen / name)
    for name, config in candidates.items():
        transfer.atomic_json(out / ("student_config_" + name + ".json"), config)
    require(code_hashes() == plan["code_sha256"]
            and all(transfer.sha(frozen / name) == sha for name, sha in plan["code_sha256"].items())
            and all(transfer.sha(path) == sha for path, sha in bound_files.items()),
            "Code or parent source changed while freezing repair plan")
    require(challenge_binding(challenge_receipt) == challenge, "Development challenge changed while freezing")
    transfer.atomic_json(out / "experiment_plan.json", plan)
    transfer.atomic_json(out / "plan_receipt.json", {"plan_sha256": transfer.sha(out / "experiment_plan.json")})
    return plan


def teacher_labels(rows, inputs, folder):
    folder = Path(folder)
    protocol, receipt = read(folder / "protocol.json"), read(folder / "completion_receipt.json")
    require(protocol.get("input_sha256") == transfer.sha(inputs) and protocol.get("pairs") == len(rows),
            "Semantic teacher input file or denominator differs")
    require(receipt.get("complete") is True and receipt.get("pairs") == len(rows)
            and receipt.get("protocol_sha256") == teacher.digest(protocol)
            and receipt.get("results_sha256") == transfer.sha(folder / "results.jsonl"),
            "Semantic teacher completion receipt mismatch")
    raw = preview.read_jsonl(folder / "results.jsonl")
    return teacher.validate_labels(rows, raw, protocol)


def _finite_seconds(value, *, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def bulk_timing(summary, expected_pairs, *, route_seconds=0.):
    """Validate one-load measurements; never extrapolate per-pair startup costs."""
    require(type(summary.get("pairs")) is int and summary["pairs"] == expected_pairs and expected_pairs > 0,
            "Bulk timing denominator differs from the full evaluated source")
    require(_finite_seconds(summary.get("inference_seconds"), positive=True)
            and _finite_seconds(summary.get("setup_seconds")) and _finite_seconds(route_seconds),
            "Bulk timing needs measured positive inference and nonnegative setup/route seconds")
    return {"pairs": expected_pairs, "inference_seconds": summary["inference_seconds"] + route_seconds,
            "setup_seconds": summary["setup_seconds"]}


def predict_once(inputs, checkpoint, out, *, device="cuda"):
    """A sequential child mode with separately measured one-load setup/prediction.

    Setup is the StudentPredictor constructor plus CUDA synchronization, not a
    per-pair share of process startup. Prediction is the complete once-loaded
    predictor call. Process startup, source reading, cleanup and export IO remain
    in the parent's separate end-to-end wall-time measurement. No production or
    holdout data is read unless this mode is explicitly launched by the pipeline.
    """
    inputs, checkpoint, out = Path(inputs).resolve(), Path(checkpoint).resolve(), Path(out).resolve()
    require(not out.exists(), "Prediction output must be a new attempt")
    source_sha = transfer.sha(inputs)
    rows = preview.read_jsonl(inputs)
    require(rows and source_sha == transfer.sha(inputs), "Empty or changing prediction source")
    before_code = code_hashes()
    started = time.perf_counter()
    predictor = student.StudentPredictor(checkpoint, device=device)
    try:
        if str(device).startswith("cuda"):
            predictor.torch.cuda.synchronize()
        setup_seconds = time.perf_counter() - started
        prediction_started = time.perf_counter()
        predictions = predictor.predict(rows)
        inference_seconds = time.perf_counter() - prediction_started
        model_sha = predictor.manifest_sha256
    finally:
        predictor.close()
    require(len(predictions) == len(rows) and all(preview.pair_key(row) == preview.pair_key(prediction)
            and prediction["input_sha256"] == preview.canonical_input_sha256(row)
            and prediction["student_manifest_sha256"] == model_sha for row, prediction in zip(rows, predictions)),
            "Prediction source/model coverage mismatch")
    require(source_sha == transfer.sha(inputs) and before_code == code_hashes(),
            "Prediction source or orchestration code changed")
    summary = {"complete": True, "pairs": len(rows), "input_sha256": source_sha,
        "student_manifest_sha256": model_sha, "requires_fallback": sum(p["requires_fallback"] for p in predictions),
        "inference_seconds": inference_seconds, "setup_seconds": setup_seconds,
        "timing_scope": "one StudentPredictor load then one complete predict call; export IO and process startup excluded",
        "production_approved": False}
    bulk_timing(summary, len(rows))
    out.mkdir(parents=True, exist_ok=False)
    with (out / "results.jsonl").open("x", encoding="utf8", newline="\n") as stream:
        for row in predictions:
            stream.write(preview.canonical(row) + "\n")
    transfer.atomic_json(out / "summary.json", summary)
    return summary


class Repair(original.Pilot):
    def __init__(self, out):
        self.out = Path(out).resolve()
        self.plan = read(self.out / "experiment_plan.json")
        self.plan_sha = read(self.out / "plan_receipt.json")["plan_sha256"]
        self.assert_frozen()
        (self.out / "stages").mkdir(exist_ok=True)
        (self.out / "logs").mkdir(exist_ok=True)

    def assert_frozen(self):
        p = self.plan
        require(p.get("schema") == SCHEMA and transfer.sha(self.out / "experiment_plan.json") == self.plan_sha,
                "Repair plan changed")
        require(code_hashes() == p["code_sha256"]
                and transfer.sha(HERE / "local_models/relevance_model_receipt.json") == p["model_receipt_sha256"],
                "Repair code or teacher model receipt changed")
        require(all(transfer.sha(path) == sha for path, sha in p["source_file_sha256"].items()),
                "Frozen parent source changed")
        for name, config in p["candidate_configs"].items():
            require(read(self.out / ("student_config_" + name + ".json")) == config,
                    "Candidate training configuration changed")
        challenge = p["challenge_receipt"]
        if challenge is not None:
            require(challenge_binding(challenge["path"]) == challenge, "Bound development challenge changed")

    def status(self, state, **extra):
        import os
        transfer.atomic_json(self.out / "status.json", {"schema": SCHEMA, "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
            "plan_sha256": self.plan_sha, "human_gold": False, "production_approved": False,
            "full_corpus_selection_complete": False, **extra})

    def stage(self, name, action):
        self.assert_frozen()
        path = self.out / "stages" / (name + ".json")
        if path.exists():
            receipt = read(path)
            require(receipt.get("complete") is True and receipt.get("stage") == name
                    and receipt.get("plan_sha256") == self.plan_sha,
                    "Invalid completed repair stage: " + name)
            require(all(transfer.sha(p) == h for p, h in receipt["files"].items()),
                    "Completed repair stage artifacts changed: " + name)
            return receipt["value"]
        self.status("RUNNING", stage=name)
        started = time.perf_counter()
        value, files = action()
        self.assert_frozen()
        transfer.atomic_json(path, {"complete": True, "stage": name, "plan_sha256": self.plan_sha,
            "finished_at": datetime.now(timezone.utc).isoformat(), "wall_seconds": time.perf_counter() - started,
            "value": value, "files": {str(Path(p).resolve()): transfer.sha(p) for p in files}})
        return value

    def inputs(self, fold):
        return Path(self.plan["source_sample_root"]) / fold / "inputs.jsonl"

    def teach(self, fold):
        folder = self.out / ("teacher_" + fold)
        existed = folder.exists()
        seconds = self.child("teacher_" + fold, "kg_semantic_teacher.py",
                             ["--inputs", self.inputs(fold), "--out", folder])
        rows = preview.read_jsonl(self.inputs(fold))
        require(len(rows) == self.plan["sample_sizes"][fold], "Frozen fold denominator differs")
        values = teacher_labels(rows, self.inputs(fold), folder)
        receipt = read(folder / "completion_receipt.json")
        return {"root": str(folder), "wall_seconds": seconds,
                "uninterrupted_measurement": not existed and receipt["resumed_pairs"] == 0,
                "unusable_teacher_pairs": sum(value is None for value in values)}, [folder / name for name in
                ("protocol.json", "results.jsonl", "completion_receipt.json")]

    def train(self, name, train_labels, dev_labels):
        require(student.legacy.verified_encoder(student.legacy.BUNDLE)[1] == self.plan["student_parent_encoder"],
                "Parent encoder changed since plan freeze")
        folder = self.attempt("student_" + name)
        args = ["train", "--out", folder, "--device", "cuda", "--config", self.out / ("student_config_" + name + ".json")]
        for fold, labels in (("train", train_labels), ("dev", dev_labels)):
            teacher_labels(preview.read_jsonl(self.inputs(fold)), self.inputs(fold), labels["root"])
            args += ["--" + fold + "-inputs", self.inputs(fold), "--" + fold + "-results",
                     Path(labels["root"]) / "results.jsonl", "--" + fold + "-protocol", Path(labels["root"]) / "protocol.json"]
        seconds = self.child("student_train_" + name, "kg_cascade_student_v2.py", args)
        manifest = read(folder / "training_manifest.json")
        require(manifest.get("complete") is True and manifest["config"] == self.plan["candidate_configs"][name]
                and manifest["encoder"] == self.plan["student_parent_encoder"], "Student training manifest differs")
        selected = [trial for trial in manifest["trials"] if trial["epoch"] == manifest["selected_epoch"]]
        require(len(selected) == 1, "Selected training epoch missing or duplicated")
        return {"root": str(folder), "wall_seconds": seconds, "candidate": name,
                "dev_log_loss": selected[0]["model_dev_log_loss"]}, [folder / "training_manifest.json", folder / "plan.json",
                    *[folder / "checkpoint" / path for path in manifest["checkpoint_files"]]]

    def predict(self, fold, trained):
        name = "student_" + fold + "_" + trained["candidate"]
        folder = self.attempt(name)
        seconds = self.child(name, Path(__file__).name, ["--mode", "predict_once", "--inputs", self.inputs(fold),
            "--checkpoint", trained["root"], "--out", folder, "--device", "cuda"])
        summary = read(folder / "summary.json")
        require(summary.get("complete") is True and summary["input_sha256"] == transfer.sha(self.inputs(fold))
                and summary["pairs"] == self.plan["sample_sizes"][fold], "Student prediction incomplete")
        return {"root": str(folder), "wall_seconds": seconds, "candidate": trained["candidate"],
                "student_bulk_timing": bulk_timing(summary, self.plan["sample_sizes"][fold]),
                "student_manifest_sha256": transfer.sha(Path(trained["root"]) / "training_manifest.json")}, [
                folder / "results.jsonl", folder / "summary.json"]

    def aligned(self, fold, labels, predictions):
        rows = preview.read_jsonl(self.inputs(fold))
        values = teacher_labels(rows, self.inputs(fold), labels["root"])
        pred = preview.read_jsonl(Path(predictions["root"]) / "results.jsonl")
        require(len(rows) == self.plan["sample_sizes"][fold] and len(pred) == len(rows), "Evaluation denominator differs")
        for row, prediction in zip(rows, pred):
            require(preview.pair_key(row) == preview.pair_key(prediction)
                    and preview.canonical_input_sha256(row) == prediction["input_sha256"]
                    and prediction.get("student_manifest_sha256") == predictions["student_manifest_sha256"],
                    "Student prediction source or model mismatch")
            coverage = prediction.get("coverage", {})
            if prediction["probabilities"] is not None:
                require(prediction.get("requires_fallback") is False and not prediction.get("fallback_reasons")
                        and coverage.get("full_coverage") is True and coverage.get("encoding_completed") is True
                        and coverage.get("truncated") is False
                        and coverage.get("total_content_tokens", 0) > 0
                        and coverage.get("covered_content_tokens") == coverage.get("total_content_tokens"),
                        "Student assigned probabilities to an unsupported input")
        trace = [x for x in preview.read_jsonl(Path(self.plan["source_sample_root"]) / "selection_trace.jsonl") if x["fold"] == fold]
        indexed = {preview.pair_key(x): x for x in trace}
        require(len(indexed) == len(trace) == len(rows) and set(indexed) == {preview.pair_key(x) for x in rows},
                "Group isolation trace differs")
        return {"rows": rows, "labels": reference_labels(values), "teacher_values": values,
                "probs": [p["probabilities"] for p in pred], "groups": [indexed[preview.pair_key(x)]["group_id"] for x in rows],
                "ids": [preview.canonical(preview.pair_key(x)) for x in rows],
                "unusable_teacher_pairs": sum(value is None for value in values)}

    def tune(self, name, labels, predictions):
        data = self.aligned("dev", labels, predictions)
        value = routing.tune_policy(data["labels"], data["probs"], self.plan["routing_config"],
                                   dev_ids=data["ids"], dev_group_ids=data["groups"])
        path = self.out / ("dev_threshold_candidate_" + name + ".json")
        transfer.atomic_json(path, value)
        return {"path": str(path), "enabled_dev_direction": enabled(value)}, [path]

    def select(self, candidates):
        metrics = {name: {"dev_log_loss": value["trained"]["dev_log_loss"],
                         "enabled_dev_direction": value["thresholds"]["enabled_dev_direction"]}
                   for name, value in candidates.items()}
        selected = choose_candidate(metrics)
        value = {"selected": selected, "metrics": metrics, "candidates": candidates,
                 "selection_rule": self.plan["candidate_selection"], "plan_sha256": self.plan_sha,
                 "calibration_or_test_used": False, "selected_before_calibration": True}
        path = self.out / "candidate_selection.json"
        transfer.atomic_json(path, value)
        return value, [path]

    def confirm(self, labels, predictions, candidate):
        data = self.aligned("calibration", labels, predictions)
        value = routing.confirm_policy(data["labels"], data["probs"], read(candidate["path"]),
                                       cal_ids=data["ids"], cal_group_ids=data["groups"])
        path = self.out / "routing_policy.json"
        transfer.atomic_json(path, value)
        return {"path": str(path), "enabled_direction": enabled(value, calibrated=True)}, [path]

    def replay(self, labels, predictions, policy):
        data = self.aligned("test", labels, predictions)
        frozen = read(policy["path"])
        start = time.perf_counter()
        routes = [routing.route(p, frozen) for p in data["probs"]]
        route_seconds = time.perf_counter() - start
        selected = [row for row, route in zip(data["rows"], routes) if route["needs_teacher"]]
        baseline = [value for value, route in zip(data["teacher_values"], routes) if route["needs_teacher"]]
        value = {"automatic": len(routes) - len(selected), "fallback_pairs": len(selected), "seconds": 0.,
                 "route_seconds": route_seconds, "measured": False, "replay_decision_changes": None}
        files = []
        if value["automatic"] and selected:
            folder = self.attempt("fallback_replay")
            folder.mkdir()
            inputs = folder / "inputs.jsonl"
            with inputs.open("x", encoding="utf8", newline="\n") as stream:
                for row in selected:
                    stream.write(preview.canonical(row) + "\n")
            result_folder = folder / "teacher"
            seconds = self.child("fallback_replay", "kg_semantic_teacher.py", ["--inputs", inputs, "--out", result_folder])
            repeated = teacher_labels(selected, inputs, result_folder)
            require(read(result_folder / "completion_receipt.json")["resumed_pairs"] == 0,
                    "Fallback timing must be an uninterrupted replay")
            value.update(seconds=seconds, measured=True,
                         replay_decision_changes=sum(a != b for a, b in zip(baseline, repeated)))
            files += [inputs, *[result_folder / name for name in ("protocol.json", "results.jsonl", "completion_receipt.json")]]
        elif value["automatic"]:
            value.update(measured=True, replay_decision_changes=0)
        path = self.out / "fallback_replay_receipt.json"
        transfer.atomic_json(path, value)
        return value, [*files, path]

    def report(self, labels, predictions, policy, replay, trained):
        data = self.aligned("test", labels, predictions)
        timings = None
        if replay["measured"] and labels["uninterrupted_measurement"]:
            timings = {"student_total_seconds": predictions["wall_seconds"] + replay["route_seconds"],
                       "teacher_all_total_seconds": labels["wall_seconds"], "fallback_teacher_total_seconds": replay["seconds"]}
        report = routing.evaluate_cascade(data["labels"], data["probs"], read(policy["path"]), timings=timings,
                                           test_ids=data["ids"], test_group_ids=data["groups"])
        # The bulk worker consumes only this aggregate report, never sealed row labels.
        report.pop("rows")
        report.update(plan_sha256=self.plan_sha, student_manifest_sha256=predictions["student_manifest_sha256"],
            student_bulk_timing=bulk_timing(predictions["student_bulk_timing"], len(data["rows"]), route_seconds=replay["route_seconds"]),
            semantic_teacher_version=teacher.VERSION, old_grounded_protocol_parity=False,
            human_gold=False, factual_accuracy_established=False, population_accuracy_claim=False,
            unusable_teacher_pairs=data["unusable_teacher_pairs"],
            teacher_failure_reference="review_abstention_all_source_pairs_retained",
            independent_truth_audit="not_established_by_development_challenges",
            fallback_replay_decision_changes=replay["replay_decision_changes"],
            timing_scope="same compact teacher CLI for full TEST baseline and fallback replay; sequential wall times include model loads and IO; graph evidence extraction excluded",
            full_corpus_selection_complete=False, production_approved=False)
        report["pilot_gates"]["fallback_replay_stable"] = replay["replay_decision_changes"] == 0
        report["pilot_gates"]["all_pass"] = report["pilot_gates"]["all_pass"] and report["pilot_gates"]["fallback_replay_stable"]
        path = self.out / "evaluation_report.json"
        transfer.atomic_json(path, report)
        release = self.release(report, path, trained, policy)
        return {"path": str(path), "gates": report["pilot_gates"], "release": release}, [path, self.out / "release_receipt.json"]

    def release(self, report, report_path, trained=None, policy=None):
        challenge = self.plan["challenge_receipt"]
        model_sha = transfer.sha(Path(trained["root"]) / "training_manifest.json") if trained else None
        policy_value = read(policy["path"]) if policy else None
        gates = report.get("pilot_gates", {})
        allowed = all(gates.get(name) is True for name in ("all_pass", "decision_parity_pass", "coverage_pass",
                      "measured_speed_pass", "fallback_replay_stable")) and bool(challenge and challenge["accepted"])
        if allowed:
            require(report.get("total_pairs") == SIZES["test"] and model_sha is not None and policy_value is not None
                    and report.get("student_manifest_sha256") == model_sha
                    and report.get("policy_sha256") == policy_value["policy_sha256"],
                    "Passing release must bind all TEST pairs to the exact student and policy")
            require("rows" not in report and "labels" not in report, "Release report must be aggregate only")
            bulk_timing(report.get("student_bulk_timing", {}), report["total_pairs"])
            timing = report.get("timing_comparison", {})
            seconds = [timing.get(name) for name in ("student_total_seconds", "teacher_all_total_seconds", "fallback_teacher_total_seconds")]
            require(all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in seconds)
                    and seconds[0] > 0 and seconds[1] > seconds[0] + seconds[2],
                    "Passing release requires a measured positive same-workload speed improvement")
        value = {"schema": RELEASE_SCHEMA, "complete": True, "full_fast_pass_allowed": allowed,
            "plan_sha256": self.plan_sha, "source_manifest_sha256": self.plan["full_source_manifest_sha256"],
            "student_module": "kg_cascade_student_v2",
            "student_manifest_sha256": model_sha,
            "routing_policy_file_sha256": transfer.sha(policy["path"]) if policy else None,
            "routing_policy_sha256": policy_value["policy_sha256"] if policy_value else None,
            "validation_report": {"path": Path(report_path).name, "sha256": transfer.sha(report_path)},
            "challenge_receipt": challenge, "factual_accuracy_established": False,
            "full_fast_pass_output": "provisional_auto_decisions_and_unresolved_queue_not_grounded_graph",
            "full_corpus_selection_complete": False, "full_corpus_auto_launched": False}
        transfer.atomic_json(self.out / "release_receipt.json", value)
        return value

    def quality_failure(self, stage, reason):
        report = {"state": "QUALITY_GATE_FAILED", "stage": stage, "reason": reason,
            "plan_sha256": self.plan_sha, "teacher_version": teacher.VERSION,
            "pilot_gates": {"all_pass": False}, "heldout_test_evaluated": False,
            "human_gold": False, "factual_accuracy_established": False,
            "full_corpus_selection_complete": False, "full_fast_pass_allowed": False}
        path = self.out / "evaluation_report.json"
        transfer.atomic_json(path, report)
        self.release(report, path)
        self.status("QUALITY_GATE_FAILED", stage=stage, reason=reason, report=str(path))

    def run(self):
        with transfer.CoordinatorLock(self.out / ".repair.lock"):
            try:
                tr = self.stage("teacher_train", lambda: self.teach("train"))
                dv = self.stage("teacher_dev", lambda: self.teach("dev"))
                candidates = {}
                for name in ("A", "B"):
                    trained = self.stage("student_train_" + name, lambda name=name: self.train(name, tr, dv))
                    pred = self.stage("student_dev_" + name, lambda trained=trained: self.predict("dev", trained))
                    thresholds = self.stage("dev_thresholds_" + name, lambda name=name, pred=pred: self.tune(name, dv, pred))
                    candidates[name] = {"trained": trained, "predictions": pred, "thresholds": thresholds}
                selection = self.stage("candidate_selection", lambda: self.select(candidates))
                if selection["selected"] is None:
                    self.quality_failure("dev", "Neither predeclared candidate enabled any DEV automatic direction")
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
                state = "PILOT_COMPLETE" if report["gates"]["all_pass"] else "QUALITY_GATE_FAILED"
                self.status(state, report=report, full_fast_pass_allowed=report["release"]["full_fast_pass_allowed"])
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
        if args.parent_out is None:
            parser.error("freeze requires --parent-out")
        freeze_plan(args.out, args.parent_out, args.challenge_receipt)
    elif args.mode == "run":
        Repair(args.out).run()
    else:
        if args.inputs is None or args.checkpoint is None:
            parser.error("predict_once requires --inputs and --checkpoint")
        predict_once(args.inputs, args.checkpoint, args.out, device=args.device)
