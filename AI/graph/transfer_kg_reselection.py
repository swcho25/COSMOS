"""Wait for a fixed server export, verify its archive, then run local inference.

This coordinator reads only one explicitly named server trial run. It never
publishes to HDFS or changes a service. A downloaded archive is untrusted: only
manifest-listed regular input files and fixed JSON controls may be extracted.
The local GPU worker owns durable per-chunk resume and its source/code locks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

HERE = Path(__file__).resolve().parent
HOST = "ubuntu@j15c205a.p.ssafy.io"
SSH = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/OpenSSH/ssh.exe"
KEY = Path.home() / ".ssh/J15C205T.pem"


class CoordinatorLock:
    """One downloader/extractor/worker launcher; OS releases lock after a crash."""
    def __init__(self, path):
        self.path, self.stream = path, None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Another coordinator owns this transfer run") from exc
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = item
        return result
    def reject(value):
        raise ValueError("Nonfinite JSON: " + value)
    return json.loads(value, object_pairs_hook=pairs, parse_constant=reject)


def valid_sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_transfer(value):
    if (not isinstance(value, dict) or not valid_sha(value.get("archive_sha256")) or
            not valid_sha(value.get("manifest_sha256")) or type(value.get("archive_bytes")) is not int or
            value["archive_bytes"] < 1):
        raise ValueError("Invalid transfer manifest")
    return value


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def remote_path(value):
    path = PurePosixPath(value)
    if (str(path) != value or path.parent != PurePosixPath("/home/ubuntu/ner-work")
            or not path.name.startswith("kg_reselection_")
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-." for c in path.name)):
        raise ValueError("Expected a named kg_reselection trial directory")
    return path


def ssh(command, **kwargs):
    return subprocess.run([str(SSH), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "-i", str(KEY), HOST, command], **kwargs)


def extract_verified(archive, destination, transfer):
    """Validate every member before writing; retain/resume identical local files."""
    validate_transfer(transfer)
    if sha(archive) != transfer["archive_sha256"] or archive.stat().st_size != transfer["archive_bytes"]:
        raise ValueError("Transfer archive bytes differ")
    with tarfile.open(archive, "r:gz") as tar:
        # getmembers retains metadata only; article contents are streamed.
        members = tar.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or any(not member.isfile() for member in members):
            raise ValueError("Duplicate/nonregular archive member")
        by_name = {member.name: member for member in members}
        if "manifest.json" not in by_name or by_name["manifest.json"].size > 16 * 1024 * 1024:
            raise ValueError("Missing/oversized manifest")
        raw = tar.extractfile(by_name["manifest.json"]).read()
        if hashlib.sha256(raw).hexdigest() != transfer["manifest_sha256"]:
            raise ValueError("Manifest changed")
        manifest = strict_json(raw)
        if manifest.get("schema") != "cosmos-kg-candidate-export-1" or manifest.get("complete") is not True:
            raise ValueError("Incomplete candidate export")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("Expected a nonempty input manifest")
        for item in files:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or
                    not valid_sha(item.get("sha256")) or
                    any(type(item.get(key)) is not int or item[key] < 0 for key in ("rows", "bytes"))):
                raise ValueError("Invalid input manifest member")
        expected = {item["path"]: item for item in files}
        if len(expected) != len(manifest["files"]):
            raise ValueError("Duplicate input manifest paths")
        if any(PurePosixPath(name).parent != PurePosixPath("inputs") for name in expected):
            raise ValueError("Input manifest must contain only direct inputs/ members")
        controls = {"manifest.json", "protocol.json", "_VERIFIED"}
        if set(names) != set(expected) | controls:
            raise ValueError("Archive members differ from manifest")
        for name in names:
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
                    or str(path) != name or (name not in controls and path.parent != PurePosixPath("inputs"))):
                raise ValueError("Unsafe archive path")
            if name in controls and by_name[name].size > 16 * 1024 * 1024:
                raise ValueError("Oversized completion control")
            if name in expected and by_name[name].size != expected[name]["bytes"]:
                raise ValueError("Input member length differs")
        total = manifest.get("expected_unique_pairs")
        if type(total) is not int or total < 1 or total != sum(item["rows"] for item in files):
            raise ValueError("Input manifest denominator differs")
        control_bytes = {name: tar.extractfile(by_name[name]).read() for name in controls}
        marker = strict_json(control_bytes["_VERIFIED"])
        if marker.get("manifest_sha256") != transfer["manifest_sha256"]:
            raise ValueError("Export completion marker differs")
        if (not valid_sha(manifest.get("protocol_sha256")) or
                hashlib.sha256(control_bytes["protocol.json"]).hexdigest() != manifest["protocol_sha256"]):
            raise ValueError("Export protocol bytes differ")
        strict_json(control_bytes["protocol.json"])
        destination.mkdir(parents=True, exist_ok=True)
        # Do not expose a verification marker until every input has been checked.
        ordered = [member for member in members if member.name not in controls]
        ordered += [by_name[name] for name in ("protocol.json", "manifest.json", "_VERIFIED")]
        for member in ordered:
            original_target = destination / member.name
            if original_target.is_symlink():
                raise ValueError("Existing extracted path is a symbolic link")
            target = original_target.resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("Extraction escaped destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            item = expected.get(member.name)
            if item and member.size != item["bytes"]:
                raise ValueError("Input member length differs")
            if target.exists():
                if item and target.stat().st_size == item["bytes"] and sha(target) == item["sha256"]:
                    continue
                if not item and target.read_bytes() == tar.extractfile(member).read():
                    continue
                raise ValueError("Existing extracted bytes differ")
            partial = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
            with tar.extractfile(member) as source, partial.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if item and sha(partial) != item["sha256"]:
                raise ValueError("Input member checksum differs")
            os.replace(partial, target)
        marker = strict_json((destination / "_VERIFIED").read_text(encoding="utf8"))
        if marker.get("manifest_sha256") != sha(destination / "manifest.json"):
            raise ValueError("Export completion marker differs")
        return manifest


def run_locked(args):
    remote = remote_path(args.remote_run)
    args.out.mkdir(parents=True, exist_ok=True)
    plan = {"remote_run": str(remote), "host": HOST, "batch_size": args.batch_size,
            "max_batch_tokens": args.max_batch_tokens, "max_pairs": args.max_pairs,
            "download_only": bool(getattr(args, "download_only", False)),
            "code_sha256": sha(__file__), "service_published": False}
    locked = args.out / "transfer_plan.json"
    if locked.exists() and json.loads(locked.read_text(encoding="utf8")) != plan:
        raise ValueError("Transfer resume plan changed")
    if not locked.exists():
        atomic_json(locked, plan)
    status_file = args.out / "transfer_status.json"
    while True:
        try:
            result = ssh("cat " + shlex.quote(str(remote / "status.json")), capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            atomic_json(status_file, {"state": "WAITING_FOR_SERVER_CONNECTION", "updated_epoch": time.time(),
                                      "pid": os.getpid(), "ssh_timeout_seconds": 45})
            time.sleep(30)
            continue
        if result.returncode == 0:
            status = strict_json(result.stdout)
            atomic_json(status_file, {"state": "WAITING_FOR_SERVER", "server_status": status,
                                    "updated_epoch": time.time(), "pid": os.getpid()})
            if status["state"] == "FAILED":
                raise RuntimeError("Server export failed; inspect its status/log")
            if status["state"] == "READY_FOR_DOWNLOAD":
                break
        else:
            atomic_json(status_file, {"state": "WAITING_FOR_SERVER_CONNECTION", "updated_epoch": time.time(),
                                    "pid": os.getpid(), "ssh_exit": result.returncode})
        time.sleep(30)
    while True:
        try:
            result = ssh("cat " + shlex.quote(str(remote / "transfer_manifest.json")), capture_output=True, text=True, timeout=45)
            if result.returncode == 0:
                break
            retry = {"ssh_exit": result.returncode}
        except subprocess.TimeoutExpired:
            retry = {"ssh_timeout_seconds": 45}
        atomic_json(status_file, {"state": "WAITING_FOR_TRANSFER_MANIFEST_CONNECTION", "updated_epoch": time.time(),
                                 "pid": os.getpid(), **retry})
        time.sleep(30)
    transfer = validate_transfer(strict_json(result.stdout))
    archive = args.out / "candidate_inputs.tar.gz"
    if not archive.exists():
        if shutil.disk_usage(args.out).free < transfer["archive_bytes"] * 3 + 20 * 1024**3:
            raise RuntimeError("Insufficient free disk for archive and inputs")
        atomic_json(status_file, {"state": "DOWNLOADING", "updated_epoch": time.time(), "pid": os.getpid()})
        partial = archive.with_suffix(".gz.part")
        with partial.open("wb") as stream:
            ssh("cat " + shlex.quote(str(remote / "candidate_inputs.tar.gz")), stdout=stream, check=True)
        if sha(partial) != transfer["archive_sha256"]:
            raise ValueError("Downloaded archive checksum mismatch")
        os.replace(partial, archive)
    atomic_json(args.out / "transfer_manifest.json", transfer)
    atomic_json(status_file, {"state": "VERIFYING_AND_EXTRACTING", "updated_epoch": time.time(), "pid": os.getpid()})
    manifest = extract_verified(archive, args.out / "source", transfer)
    if getattr(args, "download_only", False):
        atomic_json(status_file, {"state": "DOWNLOAD_VERIFIED", "updated_epoch": time.time(),
                                "pid": os.getpid(), "expected_unique_pairs": manifest["expected_unique_pairs"],
                                "inference_started": False})
        return manifest
    atomic_json(status_file, {"state": "LOCAL_INFERENCE", "updated_epoch": time.time(), "pid": os.getpid(),
                            "expected_unique_pairs": manifest["expected_unique_pairs"]})
    command = [sys.executable, "-X", "utf8", str(HERE / "knowledge_graph_reselection_worker.py"),
               "--manifest", str(args.out / "source/manifest.json"), "--out", str(args.out / "inference"),
               "--batch-size", str(args.batch_size), "--max-batch-tokens", str(args.max_batch_tokens)]
    if args.max_pairs:
        command += ["--max-pairs", str(args.max_pairs)]
    if (args.out / "inference").exists():
        command.append("--resume")
    with (args.out / "worker.log").open("ab") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    atomic_json(status_file, {"state": "WORKER_EXITED", "updated_epoch": time.time(), "pid": os.getpid(),
                            "completion_requires_worker_receipt": True})


def run(args):
    args.out.mkdir(parents=True, exist_ok=True)
    with CoordinatorLock(args.out / ".transfer.lock"):
        try:
            return run_locked(args)
        except Exception as error:
            atomic_json(args.out / "transfer_error.json", {"type": type(error).__name__, "error": str(error),
                                                           "at_epoch": time.time()})
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-run", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, choices=(1, 2), default=1)
    parser.add_argument("--max-batch-tokens", type=int, default=8192)
    parser.add_argument("--max-pairs", type=int)
    parser.add_argument("--download-only", action="store_true",
                        help="Stop after archive verification; never launch the full inference worker")
    args = parser.parse_args()
    run(args)
