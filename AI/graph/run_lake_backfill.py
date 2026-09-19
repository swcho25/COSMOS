"""Detached, sequential, restart-checked supervisor for server lake backfills.

CLI: run_lake_backfill.py {launch,run} --run-dir RUN --plan PLAN

Plan schema (all commands are argv arrays; no shell is used):
  schema_version: "lake-backfill-plan-1", run_id, allowed_code_dir,
  allowed_executables: [absolute python/spark-submit paths],
  code_fingerprints/source_fingerprints: [{path, sha256}],
  env?: {NAME: "literal value"},
  steps: [{id, stage, partition?, command, cwd, completion_marker,
           outputs: [{path, sha256?}], counts?: {name: int},
           progress_file?: path, resume_partial?: false}]

Output, marker and progress paths are run-relative; cwd and local command paths
must remain inside RUN or allowed_code_dir. Source fingerprints refer to frozen
local source manifests, not live HDFS directory listings. HDFS/S3 URIs can appear
as command arguments. Every Python entry script must have a code fingerprint.

Persist the entire run directory. Its locked plan and checkpoints are immutable.
Checkpoint outputs must also be immutable: do not list a global SQLite state DB
that later steps update. A matching checkpoint skips a step; changed/missing
outputs fail instead of silently rerunning or replacing them. Without a checkpoint,
partial declared outputs fail unless resume_partial explicitly authorizes a step's
own recovery logic. Retries are never automatic. No outputs are removed.

The Linux production lock is flock(LOCK_EX|LOCK_NB). The Windows fallback exists
for local tests. launch starts a new session with stdin=DEVNULL and server-local
logs, independent of the originating PC/SSH connection. SIGTERM/SIGINT terminate
the active child process group and record TERMINATED; checkpoints remain usable.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid


SCHEMA_VERSION = "lake-backfill-plan-1"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z", re.I)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def plan_digest(plan):
    return hashlib.sha256(canonical_bytes(plan)).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows observers can briefly hold a non-delete-sharing handle.
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(.05)
    finally:
        if temporary.exists():
            temporary.unlink()  # Only this function's own incomplete metadata file.


def exclusive_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical_bytes(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path):
    for attempt in range(20):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(.05)


class RunLock:
    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            self.stream = None
            raise RuntimeError(f"Another supervisor owns lock {self.path}") from error
        return self

    def __exit__(self, *unused):
        if self.stream is not None:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def _name(value, label):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _inside(path, roots):
    resolved = Path(path).resolve()
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ValueError(f"Path escapes approved run/code directories: {path}")
    return resolved


def _run_path(value, run_dir):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"Expected a run-relative output path: {value!r}")
    path = _inside(run_dir / value, [run_dir])
    if path == run_dir:
        raise ValueError("Output path cannot be the run directory")
    reserved = {"status.json", "pid.json", "events.jsonl", "plan.locked.json", ".runner.lock", ".launch.lock", "logs", "checkpoints"}
    if Path(value).parts[0] in reserved:
        raise ValueError("Step artifact path overlaps supervisor metadata")
    return path


def validate_plan(plan, run_dir):
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Expected plan schema {SCHEMA_VERSION}")
    _name(plan.get("run_id"), "run_id")
    run_dir = Path(run_dir).resolve()
    code_value = plan.get("allowed_code_dir")
    if not isinstance(code_value, str) or not Path(code_value).is_absolute():
        raise ValueError("allowed_code_dir must be absolute")
    code_dir = Path(code_value).resolve()
    if not code_dir.is_dir():
        raise ValueError("allowed_code_dir does not exist")
    roots = [run_dir, code_dir]
    environment = plan.get("env", {})
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        or not isinstance(value, str) or "\0" in value
        for key, value in environment.items()
    ):
        raise ValueError("env must map valid environment names to literal string values")
    executables = plan.get("allowed_executables")
    if not isinstance(executables, list) or not executables:
        raise ValueError("Explicit allowed_executables are required")
    executable_paths = set()
    for value in executables:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Executable paths must be absolute")
        path = Path(value).resolve()
        if not path.is_file() or not (PYTHON.fullmatch(Path(value).name) or Path(value).name == "spark-submit"):
            raise ValueError(f"Only explicit Python and spark-submit executables are supported: {value}")
        executable_paths.add(path)
    code_paths, fingerprints = set(), []
    for category in ("code_fingerprints", "source_fingerprints"):
        entries = plan.get(category)
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Nonempty {category} are required")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError(f"Invalid {category} entry")
            path = Path(entry["path"])
            if not path.is_absolute():
                raise ValueError("Fingerprint paths must be absolute")
            path = _inside(path, roots)
            expected = entry.get("sha256")
            if not isinstance(expected, str) or not HASH.fullmatch(expected):
                raise ValueError("Expected lowercase SHA256 fingerprint")
            fingerprints.append((path, expected, category))
            if category == "code_fingerprints":
                code_paths.add(path)
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("At least one step is required")
    ids, outputs_seen = set(), set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Step must be an object")
        name = _name(step.get("id"), "step id")
        _name(step.get("stage"), "stage")
        if name in ids:
            raise ValueError(f"Duplicate step id: {name}")
        ids.add(name)
        cwd_value = step.get("cwd")
        if not isinstance(cwd_value, str) or not Path(cwd_value).is_absolute():
            raise ValueError("Step cwd must be absolute")
        cwd = _inside(cwd_value, roots)
        if not cwd.is_dir():
            raise ValueError(f"Step cwd does not exist: {cwd}")
        command = step.get("command")
        if not isinstance(command, list) or len(command) < 2 or any(not isinstance(arg, str) or "\0" in arg for arg in command):
            raise ValueError("command must be a nonempty list of literal argv strings")
        if not Path(command[0]).is_absolute() or Path(command[0]).resolve() not in executable_paths:
            raise ValueError("Command executable is not explicitly allowed")
        if any(arg in ("-c", "-m", "--command") for arg in command[1:]):
            raise ValueError("Use a fingerprinted Python script, not inline/module commands")
        scripts = []
        for arg in command[1:]:
            if "://" in arg:
                continue
            # Recognize --flag=/absolute/path as well as separate argv paths.
            candidate = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
            if Path(candidate).is_absolute():
                _inside(candidate, roots)
            elif ".." in Path(candidate).parts:
                raise ValueError("Relative parent traversal is not allowed in command arguments")
            if candidate.endswith(".py"):
                script = _inside(Path(candidate) if Path(candidate).is_absolute() else cwd / candidate, roots)
                if script not in code_paths:
                    raise ValueError(f"Entry script has no pinned code fingerprint: {script}")
                scripts.append(script)
        if not scripts:
            raise ValueError("Each step must name a fingerprinted .py entry script")
        marker = _run_path(step.get("completion_marker"), run_dir)
        entries = step.get("outputs")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Each step needs explicit immutable outputs")
        step_paths = {marker}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Output entries must be objects")
            path = _run_path(entry.get("path"), run_dir)
            if entry.get("sha256") is not None and not HASH.fullmatch(str(entry["sha256"])):
                raise ValueError("Invalid expected output SHA256")
            step_paths.add(path)
        if step_paths & outputs_seen:
            raise ValueError("Different steps cannot own the same checkpoint output/marker")
        outputs_seen.update(step_paths)
        if step.get("progress_file") is not None:
            _run_path(step["progress_file"], run_dir)
        counts = step.get("counts", {})
        if not isinstance(counts, dict) or any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) or v < 0 for k, v in counts.items()):
            raise ValueError("counts must be nonnegative integer counters")
        if not isinstance(step.get("resume_partial", False), bool):
            raise ValueError("resume_partial must be boolean")
    return fingerprints


def verify_fingerprints(fingerprints):
    for path, expected, category in fingerprints:
        if not path.is_file() or digest_file(path) != expected:
            raise ValueError(f"{category} mismatch: {path}")


def freeze_plan(plan, run_dir):
    locked = run_dir / "plan.locked.json"
    expected = plan_digest(plan)
    if locked.exists():
        if plan_digest(read_json(locked)) != expected:
            raise ValueError("Restart refused: plan hash differs from the locked plan")
    else:
        exclusive_json(locked, plan)
    return expected


def output_hashes(step, run_dir):
    wanted = {step["completion_marker"]: None}
    for output in step["outputs"]:
        wanted[output["path"]] = output.get("sha256")
    observed = []
    for relative, expected in sorted(wanted.items()):
        path = _run_path(relative, run_dir)
        if not path.is_file():
            raise ValueError(f"Missing completed output: {relative}")
        actual = digest_file(path)
        if expected is not None and actual != expected:
            raise ValueError(f"Expected output SHA256 differs: {relative}")
        observed.append({"path": relative, "sha256": actual, "bytes": path.stat().st_size})
    return observed


def checkpoint_matches(checkpoint, step, run_dir, digest):
    if checkpoint.get("plan_sha256") != digest or checkpoint.get("step_id") != step["id"]:
        raise ValueError("Checkpoint does not belong to this immutable plan/step")
    observed = output_hashes(step, run_dir)
    if checkpoint.get("outputs") != observed:
        raise ValueError(f"Completed checkpoint output hash changed: {step['id']}")
    return True


class Terminated(Exception):
    pass


def _stop_child(child, grace):
    if child.poll() is not None:
        return
    if os.name == "nt":
        child.terminate()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            child.kill()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def execute(plan, run_dir):
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    with RunLock(run_dir / ".runner.lock"):
        return _execute_locked(plan, run_dir)


def _execute_locked(plan, run_dir):
    started = now()
    control = {"stop": False, "signal": None}
    previous_handlers = {}
    child = None
    status = {"run_id": plan.get("run_id"), "state": "STARTING", "runner_pid": os.getpid(),
              "child_pid": None, "stage": None, "partition": None, "step_id": None,
              "completed_steps": 0, "total_steps": len(plan.get("steps", [])), "counts": {},
              "active_counts": {}, "started_at": started, "updated_at": started}
    for folder in ("logs", "checkpoints"):
        (run_dir / folder).mkdir(exist_ok=True)
    event_path = run_dir / "events.jsonl"

    def update(**values):
        status.update(values, updated_at=now())
        atomic_json(run_dir / "status.json", status)

    def event(kind, **values):
        with event_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"time": now(), "event": kind, **values}, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()

    def signal_handler(signum, unused):
        control.update(stop=True, signal=signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, signal_handler)
    try:
        atomic_json(run_dir / "pid.json", {"pid": os.getpid(), "host": socket.gethostname(),
                                          "run_id": plan.get("run_id"), "started_at": started,
                                          "launch_token": os.environ.get("COSMOS_SUPERVISOR_LAUNCH_TOKEN")})
        update()
        fingerprints = validate_plan(plan, run_dir)
        digest = freeze_plan(plan, run_dir)
        update(plan_sha256=digest)
        verify_fingerprints(fingerprints)
        event("STARTED", plan_sha256=digest)
        for index, step in enumerate(plan["steps"], 1):
            if control["stop"]:
                raise Terminated("Termination requested between steps")
            update(state="RUNNING", stage=step["stage"], partition=step.get("partition"),
                   step_id=step["id"], step_index=index, active_counts={}, progress=None)
            verify_fingerprints(fingerprints)
            checkpoint_path = run_dir / "checkpoints" / (step["id"] + ".json")
            if checkpoint_path.exists():
                checkpoint_matches(read_json(checkpoint_path), step, run_dir, digest)
                event("STEP_SKIPPED", step_id=step["id"], reason="verified_checkpoint")
            else:
                declared = [step["completion_marker"], *[entry["path"] for entry in step["outputs"]]]
                if not step.get("resume_partial", False) and any(_run_path(path, run_dir).exists() for path in declared):
                    raise ValueError(f"Partial outputs already exist for {step['id']}; explicit resume_partial is required")
                attempt = uuid.uuid4().hex
                stdout_path = run_dir / "logs" / f"{step['id']}.{attempt}.log"
                step_started = now()
                event("STEP_STARTED", step_id=step["id"], command=step["command"], stdout_log=str(stdout_path))
                with stdout_path.open("xb") as stream:
                    child = subprocess.Popen(step["command"], cwd=step["cwd"], stdin=subprocess.DEVNULL,
                                             stdout=stream, stderr=subprocess.STDOUT, shell=False,
                                             start_new_session=True, close_fds=True,
                                             env={**os.environ, **plan.get("env", {})})
                    update(child_pid=child.pid, stdout_log=str(stdout_path))
                    previous_progress = None
                    while child.poll() is None:
                        if control["stop"]:
                            update(state="TERMINATING", termination_signal=control["signal"])
                            _stop_child(child, 20)
                            break
                        progress_path = step.get("progress_file")
                        if progress_path:
                            path = _run_path(progress_path, run_dir)
                            if path.is_file() and path.stat().st_size <= 1024 * 1024:
                                try:
                                    progress = read_json(path)
                                    if progress != previous_progress and isinstance(progress, dict):
                                        counts = progress.get("counts", {})
                                        if isinstance(counts, dict) and all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in counts.items()):
                                            update(active_counts=counts, progress=progress.get("message"))
                                            previous_progress = progress
                                except (OSError, ValueError):
                                    pass  # A concurrently replaced progress file is advisory.
                        time.sleep(0.25)
                    returncode = child.wait()
                child = None
                update(child_pid=None)
                if returncode != 0:
                    if control["stop"]:
                        raise Terminated(f"Step {step['id']} stopped with code {returncode}")
                    raise RuntimeError(f"Step {step['id']} failed with exit code {returncode}; see {stdout_path}")
                verify_fingerprints(fingerprints)
                checkpoint = {"step_id": step["id"], "plan_sha256": digest,
                              "outputs": output_hashes(step, run_dir), "counts": step.get("counts", {}),
                              "started_at": step_started, "completed_at": now(), "stdout_log": str(stdout_path)}
                exclusive_json(checkpoint_path, checkpoint)
                event("STEP_COMPLETED", step_id=step["id"], outputs=checkpoint["outputs"])
            for name, value in step.get("counts", {}).items():
                status["counts"][name] = status["counts"].get(name, 0) + value
            update(completed_steps=index, active_counts={})
        if control["stop"]:
            raise Terminated("Termination requested after completed step")
        update(state="COMPLETED", finished_at=now(), child_pid=None)
        event("COMPLETED", completed_steps=status["completed_steps"], counts=status["counts"])
        return 0
    except Terminated as error:
        update(state="TERMINATED", error=str(error), termination_signal=control["signal"], finished_at=now(), child_pid=None)
        event("TERMINATED", error=str(error))
        return 128 + (control["signal"] or signal.SIGTERM)
    except Exception as error:
        if child is not None:
            _stop_child(child, 20)
            child = None
        update(state="FAILED", error_type=type(error).__name__, error=str(error), finished_at=now(), child_pid=None)
        event("FAILED", error_type=type(error).__name__, error=str(error))
        return 1
    finally:
        if child is not None:
            _stop_child(child, 20)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def launch(plan, run_dir):
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    with RunLock(run_dir / ".launch.lock"):
        with RunLock(run_dir / ".runner.lock"):
            fingerprints = validate_plan(plan, run_dir)
            verify_fingerprints(fingerprints)
            digest = freeze_plan(plan, run_dir)
        log_path = run_dir / "logs" / ("supervisor-" + uuid.uuid4().hex + ".log")
        launch_token = uuid.uuid4().hex
        argv = [sys.executable, str(Path(__file__).resolve()), "run", "--run-dir", str(run_dir),
                "--plan", str(run_dir / "plan.locked.json")]
        with log_path.open("xb") as stream:
            process = subprocess.Popen(argv, cwd=str(run_dir), stdin=subprocess.DEVNULL,
                                       stdout=stream, stderr=subprocess.STDOUT, shell=False,
                                       start_new_session=True, close_fds=True,
                                       env={**os.environ, **plan.get("env", {}),
                                            "COSMOS_SUPERVISOR_LAUNCH_TOKEN": launch_token})
        # Hold the launch lock until the child has claimed its runner lock/PID.
        for unused in range(100):
            try:
                pid_record = read_json(run_dir / "pid.json")
                if pid_record.get("launch_token") == launch_token:
                    supervisor_pid = pid_record["pid"]
                    if read_json(run_dir / "status.json").get("runner_pid") == supervisor_pid:
                        break
            except (OSError, ValueError):
                pass
            if process.poll() is not None:
                raise RuntimeError(f"Supervisor exited during launch ({process.returncode}); see {log_path}")
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Supervisor PID {process.pid} did not confirm startup; inspect {log_path}")
        receipt = {"pid": supervisor_pid, "launcher_pid": process.pid,
                   "run_id": plan["run_id"], "plan_sha256": digest,
                   "run_dir": str(run_dir), "status_file": str(run_dir / "status.json"),
                   "supervisor_log": str(log_path), "detached": True, "launched_at": now()}
        exclusive_json(run_dir / ("launch-" + str(process.pid) + ".json"), receipt)
        # Reap the launcher while this caller lives, without delaying its exit.
        threading.Thread(target=process.wait, daemon=True).start()
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "launch"))
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    plan = read_json(args.plan)
    if args.mode == "launch":
        print(json.dumps(launch(plan, args.run_dir), ensure_ascii=False), flush=True)
    else:
        raise SystemExit(execute(plan, args.run_dir))


if __name__ == "__main__":
    main()
