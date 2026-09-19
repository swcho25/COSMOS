"""One-shot Linux disk guard for exactly one already-running backfill supervisor.

Launch outside the frozen job code/plan:
  python guard_lake_backfill.py launch --run-dir RUN --pid SUPERVISOR_PID

Defaults: inspect every 30 seconds; send one SIGTERM if RUN's filesystem has
less than 12 GiB free. The supervisor owns orderly termination of its children.
This guard never signals a process group, escalates to SIGKILL, restarts a job,
or installs a scheduler. It exits when the target disappears or its identity
changes. PID, Linux /proc starttime, boot_id and exact supervisor argv are pinned.
pidfd_send_signal prevents PID-reuse races between the final check and signaling.

RUN/status/guard.json is advisory live status. Guard identity files and detached
logs are retained. A per-run flock permits only one active guard. A signal sent
to the guard itself stops only the guard. The existing frozen job is unchanged.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

GIB = 1024 ** 3


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value, *, replace=True):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path):
    import fcntl
    with Path(path).open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another guard owns this run's lock") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def parse_proc_stat(text):
    """Linux comm (field 2) may contain whitespace and closing parentheses."""
    opening, closing = text.find("("), text.rfind(")")
    if opening < 1 or closing <= opening:
        raise ValueError("Malformed /proc stat")
    pid = int(text[:opening].strip())
    fields = text[closing + 1:].split()  # starts at field 3 (state)
    if len(fields) < 20:
        raise ValueError("Truncated /proc stat")
    return {"pid": pid, "state": fields[0], "starttime_ticks": int(fields[19])}


def read_boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def read_process(pid):
    base = Path("/proc") / str(pid)
    try:
        first = parse_proc_stat((base / "stat").read_text())
        if first["state"] in {"Z", "X"}:
            return None
        argv = [x.decode("utf-8", errors="surrogateescape")
                for x in (base / "cmdline").read_bytes().split(b"\0") if x]
        second = parse_proc_stat((base / "stat").read_text())
    except FileNotFoundError:
        return None
    if first["pid"] != pid or first["starttime_ticks"] != second["starttime_ticks"] or second["state"] in {"Z", "X"}:
        return None
    return {**second, "cmdline": argv}


def validate_supervisor(argv, root):
    entries = [index for index, arg in enumerate(argv) if Path(arg).name == "run_lake_backfill.py"]
    if len(entries) != 1 or entries[0] + 1 >= len(argv) or argv[entries[0] + 1] != "run":
        raise ValueError("Target is not the run_lake_backfill.py run supervisor")
    for flag, expected in [("--run-dir", root), ("--plan", root / "plan.locked.json")]:
        if argv.count(flag) != 1:
            raise ValueError(f"Supervisor must have exactly one {flag}")
        index = argv.index(flag)
        if index + 1 >= len(argv) or Path(argv[index + 1]).resolve() != expected:
            raise ValueError(f"Supervisor {flag} does not match this run")


def same_process(observed, identity):
    return observed is not None and all(observed.get(key) == identity[key]
                                        for key in ("pid", "starttime_ticks", "cmdline"))


def below_threshold(free_bytes, minimum_free_gib):
    if isinstance(free_bytes, bool) or not isinstance(free_bytes, int) or free_bytes < 0:
        raise ValueError("free_bytes must be a nonnegative integer")
    if isinstance(minimum_free_gib, bool) or not isinstance(minimum_free_gib, int) or minimum_free_gib <= 0:
        raise ValueError("minimum_free_gib must be a positive integer")
    return free_bytes < minimum_free_gib * GIB


def capture_identity(root, pid, minimum_free_gib, interval_seconds):
    if sys.platform != "linux":
        raise RuntimeError("The guard runs only on Linux")
    if pid <= 1 or pid == os.getpid():
        raise ValueError("Invalid target supervisor PID")
    below_threshold(0, minimum_free_gib)
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    process = read_process(pid)
    if process is None:
        raise ValueError("Target supervisor is not running")
    validate_supervisor(process["cmdline"], root)
    if read_json(root / "pid.json").get("pid") != pid:
        raise ValueError("Target PID differs from this run's pid.json")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Linux pidfd_open and pidfd_send_signal are required for safe targeting")
    return {key: process[key] for key in ("pid", "starttime_ticks", "cmdline")} | {
        "schema_version": "lake-backfill-guard-1", "boot_id": read_boot_id(), "run_dir": str(root),
        "minimum_free_gib": minimum_free_gib, "interval_seconds": interval_seconds,
        "guard_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "captured_at": now()}


def terminate_supervisor(identity, *, process_reader=read_process, boot_reader=read_boot_id):
    """Return a state, sending SIGTERM only to a verified, stable pidfd target."""
    if boot_reader() != identity["boot_id"]:
        return "TARGET_CHANGED"
    before = process_reader(identity["pid"])
    if before is None:
        return "TARGET_EXITED"
    if not same_process(before, identity):
        return "TARGET_CHANGED"
    try:
        descriptor = os.pidfd_open(identity["pid"], 0)
    except ProcessLookupError:
        return "TARGET_EXITED"
    try:
        after = process_reader(identity["pid"])
        if after is None:
            return "TARGET_EXITED"
        if not same_process(after, identity):
            return "TARGET_CHANGED"
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM, None, 0)
        except ProcessLookupError:
            return "TARGET_EXITED"
        return "SIGTERM_SENT"
    finally:
        os.close(descriptor)


def watch(root, identity, launch_token=None):
    if identity["run_dir"] != str(root):
        raise ValueError("Guard identity run directory mismatch")
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != identity["guard_code_sha256"]:
        raise ValueError("Guard code changed between capture and execution")
    validate_supervisor(identity["cmdline"], root)
    status_dir = root / "status"
    stop = threading.Event()
    signaled = False
    previous = {}
    status = {"schema_version": "lake-backfill-guard-status-1", "guard_pid": os.getpid(),
              "host": socket.gethostname(), "supervisor_pid": identity["pid"],
              "supervisor_starttime_ticks": identity["starttime_ticks"], "boot_id": identity["boot_id"],
              "run_dir": str(root), "minimum_free_bytes": identity["minimum_free_gib"] * GIB,
              "interval_seconds": identity["interval_seconds"], "launch_token": launch_token,
              "started_at": now(), "state": "STARTING", "free_bytes": None}

    def update(state, **values):
        status.update(values, state=state, updated_at=now())
        write_json(status_dir / "guard.json", status)
        print(json.dumps(status, ensure_ascii=False), flush=True)

    with locked(status_dir / ".guard.lock"):
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, lambda unused_signum, unused_frame: stop.set())
            while not stop.is_set():
                observed = read_process(identity["pid"])
                if read_boot_id() != identity["boot_id"] or (observed is not None and not same_process(observed, identity)):
                    update("TARGET_CHANGED", finished_at=now())
                    return 0
                if observed is None:
                    update("TARGET_EXITED", finished_at=now())
                    return 0
                free = shutil.disk_usage(root).free
                if below_threshold(free, identity["minimum_free_gib"]) and not signaled:
                    # Do not require a status write to succeed before sending:
                    # ENOSPC is precisely the condition this guard protects against.
                    outcome = terminate_supervisor(identity)
                    signaled = outcome == "SIGTERM_SENT"
                    update(outcome, free_bytes=free, signaled_at=now() if signaled else None)
                    if not signaled:
                        return 0
                else:
                    update("SIGTERM_SENT" if signaled else "RUNNING", free_bytes=free)
                stop.wait(identity["interval_seconds"])
            update("STOPPED", finished_at=now())
            return 0
        except Exception as error:
            try:
                update("FAILED", error_type=type(error).__name__, error=str(error), finished_at=now())
            except OSError:
                print("Guard status could not be written: " + repr(error), file=sys.stderr, flush=True)
            raise
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def launch(root, pid, minimum_free_gib=12, interval_seconds=30):
    status_dir = root / "status"
    status_dir.mkdir(exist_ok=True)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    with locked(status_dir / ".guard-launch.lock"):
        with locked(status_dir / ".guard.lock"):
            identity = capture_identity(root, pid, minimum_free_gib, interval_seconds)
        token = uuid.uuid4().hex
        identity_path = status_dir / ("guard-identity-" + token + ".json")
        write_json(identity_path, identity, replace=False)
        log = logs / ("guard-" + token + ".log")
        argv = [sys.executable, str(Path(__file__).resolve()), "run", "--run-dir", str(root),
                "--identity", str(identity_path), "--launch-token", token]
        with log.open("xb") as stream:
            process = subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL,
                                       stdout=stream, stderr=subprocess.STDOUT, shell=False,
                                       start_new_session=True, close_fds=True)
        for unused in range(100):
            try:
                state = read_json(status_dir / "guard.json")
                if state.get("launch_token") == token and state.get("guard_pid") == process.pid:
                    if state["state"] == "FAILED":
                        raise RuntimeError(f"Guard failed during startup; inspect {log}")
                    return {"guard_pid": process.pid, "supervisor_pid": pid, "detached": True,
                            "state": state["state"], "status": str(status_dir / "guard.json"),
                            "log": str(log), "identity": str(identity_path)}
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if process.poll() is not None:
                raise RuntimeError(f"Guard exited during startup ({process.returncode}); inspect {log}")
            time.sleep(.1)
        raise RuntimeError(f"Guard PID {process.pid} did not confirm startup; inspect {log}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("launch", "run"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--minimum-free-gib", type=int, default=12)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--launch-token")
    args = parser.parse_args()
    root = args.run_dir.resolve(strict=True)
    if args.mode == "launch":
        if args.pid is None:
            parser.error("launch requires --pid")
        print(json.dumps(launch(root, args.pid, args.minimum_free_gib, args.interval_seconds)), flush=True)
    else:
        if args.identity is None or args.identity.resolve().parent != root / "status":
            parser.error("run requires an identity file inside RUN/status")
        raise SystemExit(watch(root, read_json(args.identity), args.launch_token))


if __name__ == "__main__":
    main()
