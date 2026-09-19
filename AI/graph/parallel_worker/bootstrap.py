"""Install an isolated runtime, verify a portable package, and run existing B.

This launcher never edits the frozen app/model/input, starts an LLM, falls back
to CPU, or treats provisional classifications as validated production labels.
Python 3.12 and a functioning NVIDIA driver must already be available. Standard
venv and pip are required unless --skip-install uses a prepared interpreter.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

PACKAGE_SCHEMA = "cosmos-portable-worker-package-1"
EXPECTED_VERSIONS = {"torch": "2.6.0+cu124", "transformers": "5.16.1", "tokenizers": "0.23.2",
                     "huggingface-hub": "1.30.0", "safetensors": "0.8.0", "numpy": "2.5.3"}
TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
REQUIRED_FILES = {"bootstrap.py", "requirements.txt", "run.sh", "app/kg_provisional_bulk.py",
                  "app/kg_cascade_student_v2b.py", "app/local_models/relevance_model_receipt.json",
                  "model/training_manifest.json", "input/manifest.json", "input/protocol.json", "input/_VERIFIED"}


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, "Duplicate JSON key: " + key)
            out[key] = value
        return out
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid JSON constant: " + value)))


def package_contract(root):
    manifest_path = root / "package_manifest.json"
    manifest = read_json(manifest_path)
    require(manifest.get("schema") == PACKAGE_SCHEMA, "Unsupported package manifest schema")
    require(manifest.get("complete") is True and manifest.get("python_version") == "3.12",
            "Package is incomplete or targets a different Python runtime")
    worker = manifest.get("worker")
    require(isinstance(worker, dict) and type(worker.get("chunk_size")) is int and worker["chunk_size"] == 32,
            "Package worker chunk size must be the fixed value 32")
    require(type(manifest.get("expected_unique_pairs")) is int and manifest["expected_unique_pairs"] > 0,
            "Package assignment needs a positive unique-pair denominator")
    for key in ("student_manifest_sha256", "input_manifest_sha256"):
        value = manifest.get(key)
        require(isinstance(value, str) and len(value) == 64 and all(x in "0123456789abcdef" for x in value),
                "Invalid assignment hash: " + key)
    require(isinstance(manifest.get("files"), list) and manifest["files"], "Package file inventory is empty")
    files = {}
    for entry in manifest["files"]:
        require(isinstance(entry, dict), "Invalid package file entry")
        name = entry.get("path")
        require(isinstance(name, str) and name and "\\" not in name, "Package paths must use relative forward slashes")
        relative = Path(name)
        path = (root / relative).resolve()
        require(not relative.is_absolute() and path.is_relative_to(root) and path != root,
                "Package path escapes its directory: " + name)
        require(name not in files and path.is_file() and not (root / relative).is_symlink(), "Missing/duplicate/symlink package file: " + name)
        require(relative.parts[0] not in ("output", ".venv", "logs") and name != "package_manifest.json",
                "Mutable files or the manifest itself cannot be in the immutable inventory")
        value = entry.get("sha256")
        require(isinstance(value, str) and len(value) == 64 and all(x in "0123456789abcdef" for x in value),
                "Invalid SHA-256: " + name)
        require(type(entry.get("bytes")) is int and entry["bytes"] >= 0, "Invalid byte count: " + name)
        files[name] = {**entry, "resolved": path}
    require(REQUIRED_FILES <= set(files), "Package is missing required inventoried files: " + str(sorted(REQUIRED_FILES - set(files))))
    for directory in ("app", "model", "input"):
        for path in (root / directory).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            require(path.relative_to(root).as_posix() in files, "Unlisted immutable file: " + str(path))
    return manifest, files


def verify_assignment(manifest, model, inventory):
    require(model["student_manifest_sha256"] == manifest["student_manifest_sha256"],
            "Package assignment names a different B checkpoint")
    require(inventory["manifest_sha256"] == manifest["input_manifest_sha256"],
            "Package assignment names a different input manifest")
    require(inventory["expected_unique_pairs"] == manifest["expected_unique_pairs"],
            "Package assignment denominator differs from verified source inventory")


def verify_files(files, names=None):
    selected = sorted(files if names is None else names)
    last = time.monotonic()
    for index, name in enumerate(selected, 1):
        entry = files[name]
        path = entry["resolved"]
        require(path.stat().st_size == entry["bytes"] and digest(path) == entry["sha256"],
                "Package file bytes/hash changed: " + name)
        if time.monotonic() - last >= 10:
            print(f"[verify] {index:,}/{len(selected):,} files", flush=True)
            last = time.monotonic()
    print(f"[verify] {len(selected):,} file hashes OK", flush=True)


def installed_versions(executable=None):
    if executable is not None:
        code = ("import importlib.metadata as m,json; names=" + repr(list(EXPECTED_VERSIONS)) +
                "; out={};\nfor n in names:\n try: out[n]=m.version(n)\n except m.PackageNotFoundError: out[n]=None\nprint(json.dumps(out))")
        result = subprocess.run([str(executable), "-c", code], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    values = {}
    for name in EXPECTED_VERSIONS:
        try:
            values[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            values[name] = None
    return values


def prepare_runtime(root, skip_install, original_args):
    require(sys.version_info[:2] == (3, 12), "Full Python 3.12 is required; this script does not install Python or NVIDIA drivers")
    if skip_install:
        require(installed_versions() == EXPECTED_VERSIONS, "--skip-install requires the exact pinned runtime versions")
        return None
    venv_root = root / ".venv"
    executable = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        print("[setup] Creating isolated .venv (Python/driver must already exist)", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv_root)], check=True)
    versions = installed_versions(executable)
    if versions.get("torch") != EXPECTED_VERSIONS["torch"]:
        print("[setup] Installing CUDA PyTorch; first setup needs Internet and several GB of disk", flush=True)
        subprocess.run([str(executable), "-m", "pip", "install", "--require-virtualenv", "--only-binary=:all:",
                        "--index-url", TORCH_INDEX, "torch==" + EXPECTED_VERSIONS["torch"]], check=True)
    if any(versions.get(name) != expected for name, expected in EXPECTED_VERSIONS.items() if name != "torch"):
        subprocess.run([str(executable), "-m", "pip", "install", "--require-virtualenv", "--only-binary=:all:",
                        "--index-url", "https://pypi.org/simple", "-r", str(root / "requirements.txt")], check=True)
    subprocess.run([str(executable), "-m", "pip", "check"], check=True)
    require(installed_versions(executable) == EXPECTED_VERSIONS, "Installed versions differ from the pinned runtime")
    if Path(sys.prefix).resolve() != venv_root.resolve():
        return subprocess.call([str(executable), "-u", str(root / "bootstrap.py"), "--skip-install", *original_args])
    return None


def check_gpu():
    import torch
    require(torch.__version__ == EXPECTED_VERSIONS["torch"], "CUDA PyTorch version differs")
    require(torch.cuda.is_available(), "NVIDIA CUDA GPU is unavailable. Check the driver/WSL GPU support. CPU fallback is disabled.")
    value = torch.ones(1, device="cuda")
    require(value.item() == 1, "CUDA execution check failed")
    torch.cuda.synchronize()
    del value
    gpu = torch.cuda.get_device_properties(0)
    return {"name": gpu.name, "total_memory_bytes": gpu.total_memory,
            "torch_cuda_runtime": torch.version.cuda, "device": "cuda"}


def configure_offline(root):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["PYTHONUTF8"] = "1"
    sys.path.insert(0, str(root / "app"))


def batch_command(root, max_pairs):
    output = root / "output"
    command = [sys.executable, "-X", "utf8", "-u", str(root / "app/kg_provisional_bulk.py"),
               "--manifest", str(root / "input/manifest.json"), "--checkpoint", str(root / "model"),
               "--out", str(output), "--device", "cuda", "--chunk-size", "32", "--shard-size", "1000",
               "--stop-file", str(output / "STOP")]
    if (output / "protocol.json").is_file():
        command.append("--resume")
    if max_pairs is not None:
        command.extend(("--max-pairs", str(max_pairs)))
    return command


def run_batch(root, max_pairs, log_path):
    command = batch_command(root, max_pairs)
    print("[run] GPU provisional selection. Output: " + str(root / "output"), flush=True)
    print("[run] Log: " + str(log_path), flush=True)
    print("[run] Ctrl+C stops this run; rerunning resumes committed chunks. A STOP file needs --resume-after-stop.", flush=True)
    with log_path.open("a", encoding="utf8", newline="\n") as log:
        log.write(json.dumps({"command": command, "started_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
        log.flush()
        child = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
        last_report, last_count = 0., None
        try:
            while child.poll() is None:
                if time.monotonic() - last_report >= 10:
                    path = root / "output/status.json"
                    try:
                        status = read_json(path)
                    except (FileNotFoundError, PermissionError, ValueError):
                        status = {}
                    count, expected = status.get("processed_unique_pairs"), status.get("expected_unique_pairs")
                    if count is not None and count != last_count:
                        print(f"[progress] {count:,}/{expected:,} pairs; {status.get('state')}; {status.get('decisions')}", flush=True)
                        last_count = count
                    last_report = time.monotonic()
                time.sleep(1)
        except KeyboardInterrupt:
            output = root / "output"
            if output.exists():
                (output / "STOP").write_text("User interrupted the portable worker.\n", encoding="utf8")
            print("\n[stop] Preserving committed results and waiting for the worker to exit.", flush=True)
            try:
                if os.name != "nt" and child.poll() is None:
                    child.send_signal(signal.SIGINT)
                child.wait(timeout=15)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                child.terminate()
                child.wait(timeout=15)
            return 130
    if child.returncode:
        print("[error] Worker stopped; the log ends with:", flush=True)
        print("\n".join(log_path.read_text(encoding="utf8", errors="replace").splitlines()[-25:]), flush=True)
    else:
        print("[done] " + str(root / "output/status.json"), flush=True)
    return child.returncode


def verify_child_result(root, bulk, model, inventory):
    """Validate completion/paused metadata without promoting provisional labels."""
    output = root / "output"
    protocol = read_json(output / "protocol.json")
    require(protocol["model"]["student_manifest_sha256"] == model["student_manifest_sha256"]
            and protocol["inventory"]["manifest_sha256"] == inventory["manifest_sha256"]
            and protocol["inventory"]["expected_unique_pairs"] == inventory["expected_unique_pairs"],
            "Child output belongs to a different assignment")
    state = read_json(output / "status.json")
    values = [state]
    if state.get("complete") is True:
        receipt = read_json(output / "provisional_receipt.json")
        require(state.get("state") == "provisional_complete" and receipt.get("state") == "provisional_complete"
                and receipt.get("complete") is True and receipt.get("protocol_sha256") == digest(output / "protocol.json"),
                "Child completion receipt is missing or mismatched")
        require(receipt.get("processed_unique_pairs") == inventory["expected_unique_pairs"],
                "Child marked an incomplete assignment as complete")
        values.append(receipt)
    else:
        require(state.get("complete") is False and str(state.get("state", "")).startswith("paused"),
                "Child did not complete or pause cleanly")
    for value in values:
        require(all(type(value.get(key)) is type(expected) and value.get(key) == expected
                    for key, expected in bulk.FLAGS.items()), "Child result lost provisional-only flags")
        require(type(value.get("teacher_calls")) is int and value["teacher_calls"] == 0,
                "Child unexpectedly reported teacher execution")
        require(value.get("expected_unique_pairs") == inventory["expected_unique_pairs"],
                "Child result denominator differs")
    print("[verified] Child output retains provisional-only scope and the assigned source/model bindings.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Install/verify runtime and package, load the original model on GPU, then exit without classifying")
    parser.add_argument("--max-pairs", type=int, help="Bound newly committed pairs in this invocation; default processes this laptop's entire assigned input")
    parser.add_argument("--resume-after-stop", action="store_true", help="Explicitly clear output/STOP and resume")
    parser.add_argument("--skip-install", action="store_true", help="Use this already prepared Python 3.12 interpreter; versions must exactly match")
    args = parser.parse_args(argv)
    require(args.max_pairs is None or args.max_pairs > 0, "--max-pairs must be positive")
    require(not (args.check and args.resume_after_stop), "--check does not clear STOP; use --resume-after-stop for execution")
    root = Path(__file__).resolve().parent
    if (root / "output/STOP").exists() and not args.resume_after_stop and not args.check:
        print("[paused] output/STOP exists. Resume explicitly with --resume-after-stop.", flush=True)
        return 0
    manifest, files = package_contract(root)
    verify_files(files, REQUIRED_FILES & {"bootstrap.py", "requirements.txt", "run.sh"})
    original_args = list(sys.argv[1:] if argv is None else argv)
    code = prepare_runtime(root, args.skip_install, original_args)
    if code is not None:
        return code
    verify_files(files)
    configure_offline(root)
    gpu = check_gpu()
    print("[gpu] " + json.dumps(gpu, ensure_ascii=False), flush=True)
    bulk = importlib.import_module("kg_provisional_bulk")
    model = bulk.model_contract(root / "model")
    inventory = bulk.inventory(root / "input/manifest.json")
    verify_assignment(manifest, model, inventory)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    receipt = {"schema": "cosmos-portable-worker-check-1", "checked_at": datetime.now(timezone.utc).isoformat(),
               "package_manifest_sha256": digest(root / "package_manifest.json"),
               "python": platform.python_version(), "versions": installed_versions(), "gpu": gpu,
               "model_manifest_sha256": model["student_manifest_sha256"],
               "source_manifest_sha256": inventory["manifest_sha256"],
               "expected_unique_pairs": inventory["expected_unique_pairs"],
               "production_approved": False, "validated_auto_selection": False, "teacher_calls": 0}
    if args.check:
        module = importlib.import_module(bulk.STUDENT_MODULE)
        predictor = module.StudentPredictor(root / "model", device="cuda")
        try:
            require(predictor.manifest_sha256 == model["student_manifest_sha256"], "Loaded model binding differs")
            receipt["original_student_loaded_on_gpu"] = True
        finally:
            predictor.close()
        (logs / ("check_" + stamp + ".json")).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
        print("[check] Package, source inventory, CUDA, and original B model load verified. No articles classified.", flush=True)
        return 0
    (logs / ("preflight_" + stamp + ".json")).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    stop = root / "output/STOP"
    if args.resume_after_stop and stop.exists():
        stop.unlink()
    code = run_batch(root, args.max_pairs, logs / ("worker_" + stamp + ".log"))
    if code == 0:
        verify_child_result(root, bulk, model, inventory)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("[error] " + str(error), file=sys.stderr, flush=True)
        raise SystemExit(2)
