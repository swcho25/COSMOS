"""Run the preregistered KG decision-distillation pilot, never a production job.

The frozen plan precedes all newly sampled labels. Sequence: verified download
only, group-separated sample, train/dev teacher labels, fine-tuned student,
DEV thresholds, independent CAL confirmation, sealed TEST teacher comparison.
GPU children run sequentially and release memory on exit. Teacher chunks resume;
interrupted sampling/training/prediction outputs remain in numbered attempts.
No failed calibration is retuned. No result is automatically deployed or used
to replace the full corpus, GNN, HDFS analyses, or service database.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import kg_cascade_routing as routing
import kg_cascade_student as student
import knowledge_graph_batch_inference as batch
import knowledge_graph_news_preview as preview
import knowledge_graph_reselection_worker as worker
import prepare_kg_cascade_data as sampler
import transfer_kg_reselection as transfer

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-kg-cascade-pilot-1"


def read(path):
    return transfer.strict_json(Path(path).read_text(encoding="utf8"))


def code_hashes():
    names = set(batch.code_hashes()) | set(student.code_hashes()) | {
        Path(__file__).name, "kg_cascade_routing.py", "prepare_kg_cascade_data.py",
        "knowledge_graph_reselection_worker.py", "transfer_kg_reselection.py",
        "prepare_local_relevance_pilot.py", "lake_news_normalize.py", "news_content.py"}
    return {name: transfer.sha(HERE / name) for name in sorted(names)}


def freeze_plan(out, remote_run, exclusions):
    out, exclusions = Path(out).resolve(), Path(exclusions).resolve()
    exclusion_manifest = exclusions.with_name("manifest.json")
    excluded = read(exclusion_manifest)
    if (excluded.get("complete") is not True or excluded.get("known_articles") != 750
            or excluded.get("price_holdout_articles") != 11969
            or excluded["file"]["sha256"] != transfer.sha(exclusions)):
        raise ValueError("Frozen 750/11969 exclusion inventory required")
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "experiment_plan.json"
    if plan_path.exists():
        raise FileExistsError("Experiment already frozen; use --mode run")
    plan = {"schema": SCHEMA, "frozen_at": datetime.now(timezone.utc).isoformat(),
        "remote_run": str(transfer.remote_path(remote_run)),
        "exclusions": str(exclusions), "exclusions_sha256": transfer.sha(exclusions),
        "exclusion_manifest_sha256": transfer.sha(exclusion_manifest),
        "sample_sizes": sampler.SIZES, "sample_seed": sampler.SEED,
        "sample_scope": sampler.SCOPE, "student_config": student.DEFAULT_CONFIG,
        "routing_config": routing.DEFAULT_CONFIG, "teacher_batch_size": 1,
        "teacher_max_batch_tokens": 8192, "teacher_chunk_size": 32,
        "model_receipt_sha256": transfer.sha(HERE / "local_models/relevance_model_receipt.json"),
        "student_parent_encoder": student.legacy.verified_encoder(student.legacy.BUNDLE)[1],
        "code_sha256": code_hashes(), "new_labels_seen": False,
        "sequence": ["download_only", "sample", "teacher_train", "teacher_dev", "student_train",
                     "student_dev", "dev_thresholds", "teacher_calibration", "student_calibration",
                     "calibration_confirm", "teacher_test", "student_test", "fallback_replay", "report"],
        "quality_target": "replicate frozen teacher decisions, not certify teacher factual correctness",
        "success_requirements": ["CAL each enabled direction: 97.5% exact lower agreement bound >=.98, support>=183",
            "TEST one-sided95% added-disagreement upper bound<=.02", "TEST automatic fraction>=.30",
            "actual measured decision-stage wall time improves, including model loads",
            "fallback replay does not change teacher decisions"],
        "grounded_graph_parity_measured": False, "independent_truth_audit": "pending_separate_blind_review",
        "out_of_distribution_scope": "closed fixed source snapshot pilot only; no future distribution guarantee",
        "human_gold": False, "production_approved": False, "full_corpus_inference_authorized_by_this_plan": False,
        "gnn_retraining": False, "service_published": False}
    (out / "frozen_code").mkdir(exist_ok=False)
    for name in plan["code_sha256"]:
        shutil.copy2(HERE / name, out / "frozen_code" / name)
    transfer.atomic_json(plan_path, plan)
    transfer.atomic_json(out / "plan_receipt.json", {"plan_sha256": transfer.sha(plan_path)})
    transfer.atomic_json(out / "student_config.json", plan["student_config"])
    return plan


def teacher_labels(rows, folder, *, expected_worker_manifest=None):
    """Validate actual run binding as well as replaying the teacher's raw stages."""
    folder = Path(folder)
    protocol_path = folder / "protocol.json"
    protocol = read(protocol_path)
    if (protocol.get("prompt_sha256") != batch.prompt_sha256()
            or protocol.get("model_receipt_sha256") != transfer.sha(HERE / "local_models/relevance_model_receipt.json")
            or any(protocol.get("code_hashes", {}).get(name) != digest for name, digest in batch.code_hashes().items())):
        raise ValueError("Teacher prompt/code/model pin mismatch")
    if expected_worker_manifest is not None:
        expected = worker.frozen_protocol(worker.inventory(Path(expected_worker_manifest)), 32, 1, 8192)
        if protocol != expected:
            raise ValueError("Teacher worker input protocol differs")
        receipt = read(folder / "completion_receipt.json")
        if (receipt.get("complete") is not True or receipt.get("protocol_sha256") != transfer.sha(protocol_path)
                or receipt.get("files", {}).get("results.jsonl") != transfer.sha(folder / "results.jsonl")):
            raise ValueError("Teacher completion receipt mismatch")
    raw = preview.read_jsonl(folder / "results.jsonl")
    required = {"type": "local_llm_inference", "model_repo": protocol["model_repo"],
                "model_revision": protocol["model_revision"], "protocol_sha256": transfer.sha(protocol_path)}
    if any(result.get("selection", {}).get("model_provenance") != required for result in raw):
        raise ValueError("Teacher result provenance is not bound to this run protocol")
    return student.validate_teacher_labels(rows, raw, protocol)


class Pilot:
    def __init__(self, out):
        self.out = Path(out).resolve()
        self.plan = read(self.out / "experiment_plan.json")
        self.plan_sha = read(self.out / "plan_receipt.json")["plan_sha256"]
        self.assert_frozen()
        (self.out / "stages").mkdir(exist_ok=True)
        (self.out / "logs").mkdir(exist_ok=True)

    def assert_frozen(self):
        p = self.plan
        if (p["schema"] != SCHEMA or transfer.sha(self.out / "experiment_plan.json") != self.plan_sha
                or code_hashes() != p["code_sha256"]
                or transfer.sha(p["exclusions"]) != p["exclusions_sha256"]
                or transfer.sha(Path(p["exclusions"]).with_name("manifest.json")) != p["exclusion_manifest_sha256"]
                or transfer.sha(HERE / "local_models/relevance_model_receipt.json") != p["model_receipt_sha256"]
                or read(self.out / "student_config.json") != p["student_config"]):
            raise ValueError("Frozen experiment inputs/code/config changed")

    def status(self, state, **extra):
        transfer.atomic_json(self.out / "status.json", {"schema": SCHEMA, "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
            "plan_sha256": self.plan_sha, "production_approved": False, **extra})

    def stage(self, name, action):
        self.assert_frozen()
        path = self.out / "stages" / (name + ".json")
        if path.exists():
            receipt = read(path)
            if receipt["plan_sha256"] != self.plan_sha or any(transfer.sha(p) != h for p, h in receipt["files"].items()):
                raise ValueError("Completed stage artifacts changed: " + name)
            return receipt["value"]
        self.status("RUNNING", stage=name)
        start = time.perf_counter()
        value, files = action()
        self.assert_frozen()
        receipt = {"complete": True, "stage": name, "plan_sha256": self.plan_sha,
            "finished_at": datetime.now(timezone.utc).isoformat(), "wall_seconds": time.perf_counter() - start,
            "value": value, "files": {str(Path(p).resolve()): transfer.sha(p) for p in files}}
        transfer.atomic_json(path, receipt)
        return value

    def child(self, name, script, args):
        start = time.perf_counter()
        command = [sys.executable, "-X", "utf8", str(HERE / script), *map(str, args)]
        with (self.out / "logs" / (name + ".log")).open("ab") as log:
            log.write((preview.canonical({"command": command, "started_at": datetime.now(timezone.utc).isoformat()}) + "\n").encode("utf8"))
            log.flush()
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        return time.perf_counter() - start

    def attempt(self, name):
        root = self.out / name
        root.mkdir(exist_ok=True)
        index = 1
        while (root / f"attempt_{index:03d}").exists():
            index += 1
        return root / f"attempt_{index:03d}"

    def download(self):
        folder = self.out / "transfer"
        self.child("download_only", "transfer_kg_reselection.py", ["--remote-run", self.plan["remote_run"],
            "--out", folder, "--batch-size", 1, "--download-only"])
        path = folder / "source/manifest.json"
        if read(folder / "transfer_status.json")["state"] != "DOWNLOAD_VERIFIED":
            raise ValueError("Download-only stage did not finish verification")
        return {"manifest": str(path)}, [path, path.with_name("_VERIFIED"), path.with_name("protocol.json")]

    def sample(self, source):
        folder = self.attempt("samples")
        args = ["--manifest", source["manifest"], "--exclusions", self.plan["exclusions"], "--out", folder]
        for fold, count in self.plan["sample_sizes"].items():
            args += ["--" + fold, count]
        self.child("sample", "prepare_kg_cascade_data.py", args)
        receipt = read(folder / "manifest.json")
        if receipt["complete"] is not True or receipt["sizes"] != self.plan["sample_sizes"]:
            raise ValueError("Sample sizes/completion differ from plan")
        files = [folder / name for name in ("manifest.json", "sampling_plan.json", "selection_trace.jsonl", "_VERIFIED")]
        for fold in self.plan["sample_sizes"]:
            files += [folder / fold / name for name in ("inputs.jsonl", "manifest.json", "protocol.json", "_VERIFIED")]
        return {"root": str(folder)}, files

    def teach(self, fold, data):
        folder = self.out / ("teacher_" + fold)
        resumed = folder.exists()
        args = ["--manifest", Path(data["root"]) / fold / "manifest.json", "--out", folder,
                "--batch-size", 1, "--chunk-size", 32, "--max-batch-tokens", 8192]
        if resumed:
            args.append("--resume")
        seconds = self.child("teacher_" + fold, "knowledge_graph_reselection_worker.py", args)
        receipt = read(folder / "completion_receipt.json")
        if receipt["complete"] is not True or receipt["processed_unique_pairs"] != self.plan["sample_sizes"][fold]:
            raise ValueError("Teacher fold incomplete")
        files = [folder / name for name in ("protocol.json", "completion_receipt.json", "results.jsonl", "selections.jsonl")]
        return {"root": str(folder), "wall_seconds": seconds, "uninterrupted_measurement": not resumed}, files

    def train(self, data, train_labels, dev_labels):
        if student.legacy.verified_encoder(student.legacy.BUNDLE)[1] != self.plan["student_parent_encoder"]:
            raise ValueError("Parent encoder changed since experiment freeze")
        folder = self.attempt("student_training")
        args = ["train", "--out", folder, "--device", "cuda", "--config", self.out / "student_config.json"]
        for fold, labels in (("train", train_labels), ("dev", dev_labels)):
            teacher_labels(preview.read_jsonl(Path(data["root"]) / fold / "inputs.jsonl"), labels["root"],
                           expected_worker_manifest=Path(data["root"]) / fold / "manifest.json")
            args += ["--" + fold + "-inputs", Path(data["root"]) / fold / "inputs.jsonl",
                "--" + fold + "-results", Path(labels["root"]) / "results.jsonl",
                "--" + fold + "-protocol", Path(labels["root"]) / "protocol.json"]
        seconds = self.child("student_train", "kg_cascade_student.py", args)
        m = read(folder / "training_manifest.json")
        if (m["complete"] is not True or m["config"] != self.plan["student_config"]
                or m["encoder"] != self.plan["student_parent_encoder"]):
            raise ValueError("Training completion/config differs")
        return {"root": str(folder), "wall_seconds": seconds}, [folder / "training_manifest.json", folder / "plan.json",
            *[folder / "checkpoint" / name for name in m["checkpoint_files"]]]

    def predict(self, fold, data, trained):
        folder = self.attempt("student_" + fold)
        seconds = self.child("student_" + fold, "kg_cascade_student.py", ["predict", "--inputs",
            Path(data["root"]) / fold / "inputs.jsonl", "--checkpoint", trained["root"], "--out", folder, "--device", "cuda"])
        return {"root": str(folder), "wall_seconds": seconds,
                "student_manifest_sha256": transfer.sha(Path(trained["root"]) / "training_manifest.json")}, [folder / "results.jsonl", folder / "summary.json"]

    def aligned(self, fold, data, labels, predictions):
        rows = preview.read_jsonl(Path(data["root"]) / fold / "inputs.jsonl")
        values = teacher_labels(rows, labels["root"],
                                expected_worker_manifest=Path(data["root"]) / fold / "manifest.json")
        pred = preview.read_jsonl(Path(predictions["root"]) / "results.jsonl")
        if len(pred) != len(rows) or any(preview.pair_key(r) != preview.pair_key(p)
                or preview.canonical_input_sha256(r) != p["input_sha256"] for r, p in zip(rows, pred)):
            raise ValueError("Student input/prediction alignment mismatch")
        for prediction in pred:
            if prediction.get("student_manifest_sha256") != predictions["student_manifest_sha256"]:
                raise ValueError("Prediction refers to a different student checkpoint")
            coverage = prediction.get("coverage", {})
            if prediction["probabilities"] is not None and (
                    prediction.get("requires_fallback") is not False or prediction.get("fallback_reasons")
                    or coverage.get("full_coverage") is not True or coverage.get("encoding_completed") is not True):
                raise ValueError("Unsupported or incompletely encoded input has student probabilities")
        trace = {preview.pair_key(x): x for x in preview.read_jsonl(Path(data["root"]) / "selection_trace.jsonl") if x["fold"] == fold}
        groups = [trace[preview.pair_key(r)]["group_id"] for r in rows]
        return rows, [student.CLASSES[x] for x in values], [p["probabilities"] for p in pred], groups

    def tune(self, data, labels, predictions):
        rows, y, probs, groups = self.aligned("dev", data, labels, predictions)
        value = routing.tune_policy(y, probs, self.plan["routing_config"],
            dev_ids=[preview.canonical(preview.pair_key(r)) for r in rows], dev_group_ids=groups)
        path = self.out / "dev_threshold_candidate.json"
        transfer.atomic_json(path, value)
        return {"path": str(path)}, [path]

    def confirm(self, data, labels, predictions, candidate):
        rows, y, probs, groups = self.aligned("calibration", data, labels, predictions)
        value = routing.confirm_policy(y, probs, read(candidate["path"]),
            cal_ids=[preview.canonical(preview.pair_key(r)) for r in rows], cal_group_ids=groups)
        path = self.out / "routing_policy.json"
        transfer.atomic_json(path, value)
        return {"path": str(path)}, [path]

    def replay(self, data, labels, predictions, policy):
        rows, y, probs, groups = self.aligned("test", data, labels, predictions)
        frozen_policy = read(policy["path"])
        route_start = time.perf_counter()
        routed = [routing.route(p, frozen_policy) for p in probs]
        route_seconds = time.perf_counter() - route_start
        selected = [r for r, result in zip(rows, routed) if result["needs_teacher"]]
        automatic = len(rows) - len(selected)
        value = {"automatic": automatic, "fallback_pairs": len(selected), "seconds": 0., "route_seconds": route_seconds,
                 "measured": False, "replay_decision_changes": None}
        files = []
        # No speed claim when everyone routes to the teacher; avoid a redundant full run.
        if automatic and selected:
            folder = self.attempt("fallback_replay")
            folder.mkdir()
            inputs = folder / "inputs.jsonl"
            inputs.write_text("".join(preview.canonical(r) + "\n" for r in selected), encoding="utf8")
            result_folder = folder / "teacher"
            value["seconds"] = self.child("fallback_replay", "knowledge_graph_batch_inference.py",
                ["--inputs", inputs, "--out", result_folder, "--batch-size", 1, "--max-batch-tokens", 8192])
            replay_labels = teacher_labels(selected, result_folder)
            before = [v for v, result in zip(y, routed) if result["needs_teacher"]]
            value.update(measured=True, replay_decision_changes=sum(a != student.CLASSES[b] for a, b in zip(before, replay_labels)))
            files += [inputs, result_folder / "results.jsonl", result_folder / "protocol.json", result_folder / "summary.json"]
        elif automatic:
            value.update(measured=True, replay_decision_changes=0)
        path = self.out / "fallback_replay_receipt.json"
        transfer.atomic_json(path, value)
        return value, [*files, path]

    def report(self, data, labels, predictions, policy, replay):
        rows, y, probs, groups = self.aligned("test", data, labels, predictions)
        timings = None
        if replay["measured"] and labels["uninterrupted_measurement"]:
            timings = {"student_total_seconds": predictions["wall_seconds"] + replay["route_seconds"],
                "teacher_all_total_seconds": labels["wall_seconds"], "fallback_teacher_total_seconds": replay["seconds"]}
        report = routing.evaluate_cascade(y, probs, read(policy["path"]), timings=timings,
            test_ids=[preview.canonical(preview.pair_key(r)) for r in rows], test_group_ids=groups)
        report.update(plan_sha256=self.plan_sha, independent_truth_audit="pending_separate_blind_review",
            production_approved=False, gnn_training_performed=False,
            fallback_replay_decision_changes=replay["replay_decision_changes"],
            timing_scope="actual sequential CLI subprocess wall times including model loads and IO; baseline uses resumable SQLite worker and replay uses batch CLI; downstream graph evidence excluded",
            population_accuracy_claim=False)
        report["pilot_gates"]["fallback_replay_stable"] = replay["replay_decision_changes"] == 0
        report["pilot_gates"]["all_pass"] = report["pilot_gates"]["all_pass"] and replay["replay_decision_changes"] == 0
        path = self.out / "evaluation_report.json"
        transfer.atomic_json(path, report)
        return {"path": str(path), "gates": report["pilot_gates"]}, [path]

    def run(self):
        with transfer.CoordinatorLock(self.out / ".pilot.lock"):
            try:
                source = self.stage("download_only", self.download)
                data = self.stage("sample", lambda: self.sample(source))
                tr = self.stage("teacher_train", lambda: self.teach("train", data))
                dv = self.stage("teacher_dev", lambda: self.teach("dev", data))
                trained = self.stage("student_train", lambda: self.train(data, tr, dv))
                dp = self.stage("student_dev", lambda: self.predict("dev", data, trained))
                candidate = self.stage("dev_thresholds", lambda: self.tune(data, dv, dp))
                cal = self.stage("teacher_calibration", lambda: self.teach("calibration", data))
                cp = self.stage("student_calibration", lambda: self.predict("calibration", data, trained))
                policy = self.stage("calibration_confirm", lambda: self.confirm(data, cal, cp, candidate))
                test = self.stage("teacher_test", lambda: self.teach("test", data))
                tp = self.stage("student_test", lambda: self.predict("test", data, trained))
                replay = self.stage("fallback_replay", lambda: self.replay(data, test, tp, policy))
                report = self.stage("report", lambda: self.report(data, test, tp, policy, replay))
                self.status("PILOT_COMPLETE", report=report, independent_truth_audit="pending_separate_blind_review")
            except BaseException as exc:
                self.status("INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
                            error={"type": type(exc).__name__, "message": str(exc)})
                raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("freeze", "run"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--remote-run")
    parser.add_argument("--exclusions", type=Path)
    args = parser.parse_args()
    if args.mode == "freeze":
        if not args.remote_run or not args.exclusions:
            parser.error("freeze needs --remote-run and --exclusions")
        freeze_plan(args.out, args.remote_run, args.exclusions)
    else:
        Pilot(args.out).run()
