"""Build a portable, checksum-bound B inference worker for one remaining queue.

The existing B model and inference source are copied byte-for-byte. This does
not download a model, run predictions, change quality gates, or copy secrets.
The parent paused results stay on the first PC; only one assigned queue and
its provenance travel to the second PC. Run after split_remaining completes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

HERE = Path(__file__).resolve().parent
CODE = (
    "kg_cascade_bulk.py", "kg_cascade_pair_encoding_fix.py",
    "kg_cascade_routing.py", "kg_cascade_semantic_labels.py",
    "kg_cascade_student.py", "kg_cascade_student_v2.py", "kg_cascade_student_v2b.py",
    "kg_provisional_bulk.py", "kg_semantic_teacher.py", "kg_tape_only_guard.py",
    "knowledge_graph_batch_inference.py", "knowledge_graph_news_preview.py",
    "knowledge_graph_relevance_policy.py", "knowledge_graph_reselection_worker.py",
    "local_news_relevance.py", "local_relevance_attention.py", "relevance_classifier.py",
    "relevance_sentence_evidence.py", "run_knowledge_graph_relevance.py",
    "transfer_kg_reselection.py",
)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def build(split_root, checkpoint, partition, *, archive=False):
    split_root, checkpoint = Path(split_root).resolve(), Path(checkpoint).resolve()
    if partition not in ("pc1", "pc2"):
        raise ValueError("Partition must be pc1 or pc2")
    plan_path = split_root / "split_plan.json"
    plan = read(plan_path)
    if plan.get("schema") != "cosmos-kg-provisional-remaining-split-1" or plan.get("complete") is not True:
        raise ValueError("Split plan must be complete")
    receipt = read(split_root / "split_plan_receipt.json")
    if receipt.get("plan_sha256") != sha(plan_path):
        raise ValueError("Split plan receipt differs")
    target = split_root / partition
    manifest_path = target / "input/manifest.json"
    source_manifest = read(manifest_path)
    if source_manifest.get("complete") is not True:
        raise ValueError("Input manifest incomplete")
    assignment = next(item for item in plan["partitions"] if item["id"] == partition)
    if (assignment["manifest_sha256"] != sha(manifest_path) or
            assignment["expected_unique_pairs"] != source_manifest["expected_unique_pairs"]):
        raise ValueError("Input differs from assigned split")
    if (target / "package_manifest.json").exists():
        raise ValueError("Package already frozen; use existing package")
    if (target / "output").exists():
        raise ValueError("Cannot package a worker that already ran")

    # Refuse stale preexisting payload; input itself is created by the splitter.
    for name in ("app", "model", "run.sh", "bootstrap.py", "split_plan.json"):
        if (target / name).exists():
            raise ValueError("Existing payload: " + name)
    templates = HERE / "parallel_worker"
    for required in ("run.sh", "run.ps1", "bootstrap.py", "requirements.txt", "README.md"):
        if not (templates / required).is_file():
            raise FileNotFoundError(templates / required)

    import kg_provisional_bulk as bulk
    model = bulk.model_contract(checkpoint)
    binding = split_root / "base/model_binding.json"
    if (sha(binding) != plan["base"]["model_binding_sha256"] or
            model["student_manifest_sha256"] != read(binding)["student_manifest_sha256"]):
        raise ValueError("Model differs from paused run")
    bulk.inventory(manifest_path)
    (target / "app/local_models").mkdir(parents=True)
    for name in CODE:
        shutil.copy2(HERE / name, target / "app" / name)
    shutil.copy2(HERE / "local_models/relevance_model_receipt.json",
                 target / "app/local_models/relevance_model_receipt.json")
    (target / "model").mkdir()
    shutil.copy2(checkpoint / "training_manifest.json", target / "model/training_manifest.json")
    shutil.copytree(checkpoint / "checkpoint", target / "model/checkpoint")
    for name in ("run.sh", "run.ps1", "bootstrap.py", "requirements.txt", "README.md"):
        shutil.copy2(templates / name, target / name)
    shutil.copy2(plan_path, target / "split_plan.json")
    files = [{"path": path.relative_to(target).as_posix(), "sha256": sha(path),
              "bytes": path.stat().st_size}
             for path in sorted(target.rglob("*")) if path.is_file()]
    package = {
        "schema": "cosmos-portable-worker-package-1", "complete": True,
        "python_version": "3.12", "partition": partition,
        "split_plan_sha256": sha(plan_path),
        "input_manifest_sha256": sha(manifest_path),
        "expected_unique_pairs": source_manifest["expected_unique_pairs"],
        "student_manifest_sha256": model["student_manifest_sha256"],
        "worker": {"chunk_size": 32}, "files": files,
        "classification_scope": "provisional_candidate_only",
        "production_approved": False, "graph_ready": False,
    }
    frozen = target / "package_manifest.json"
    frozen.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    result = {"package": str(target), "partition": partition,
              "expected_unique_pairs": package["expected_unique_pairs"],
              "package_manifest_sha256": sha(frozen),
              "files": len(files), "payload_bytes": sum(item["bytes"] for item in files)}
    if archive:
        archive_path = split_root / (partition + "_worker.zip")
        # Exclusive creation avoids silently replacing a delivered worker.
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=1, allowZip64=True) as zip_out:
            for item in files + [{"path": "package_manifest.json"}]:
                path = target / item["path"]
                zip_out.write(path, partition + "/" + item["path"])
        result.update(archive=str(archive_path), archive_bytes=archive_path.stat().st_size,
                      archive_sha256=sha(archive_path))
    (split_root / (partition + "_package_receipt.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split-root", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--partition", required=True, choices=("pc1", "pc2"))
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.split_root, args.checkpoint, args.partition,
                           archive=args.archive), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
