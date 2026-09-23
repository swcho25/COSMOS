"""Walk enriched batches, extract relations, and add them to the live graph.

    python -m services.relation_loader.runner \
        --root hdfs://.../analyzed/news/company-sentiment/model_version=... \
        --state-file /var/lib/cosmos-relation-loader/state.json --commit

Runs once or twice a day rather than every ten minutes: "삼성전자가 SK하이닉스와
경쟁한다" does not change between passes, and each pass rewrites the scores of
every relation it touches.

Reads the same batches the sentiment loader reads. Those carry the article text,
which is all relation extraction needs - no model, no second pass over HDFS.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pyarrow.parquet as pq

from .postgres import load_relations

COLUMNS = ["url_hash", "title", "content", "language", "companies"]


class RunnerError(RuntimeError):
    pass


def hdfs_call(hdfs_bin: str, *args: str, binary: bool = False, timeout: int = 300):
    result = subprocess.run([hdfs_bin, "dfs", *args], capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RunnerError(f"hdfs {' '.join(args)} failed: "
                          f"{result.stderr.decode('utf-8', 'replace').strip()[:400]}")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def discover_batches(hdfs_bin: str, root: str) -> list[str]:
    listing = hdfs_call(hdfs_bin, "-ls", "-R", root)
    batches, succeeded = set(), set()
    for line in listing.splitlines():
        path = line.rsplit(" ", 1)[-1].strip()
        if path.endswith("/data.parquet"):
            batches.add(path[: -len("/data.parquet")])
        elif path.endswith("/_SUCCESS"):
            succeeded.add(path[: -len("/_SUCCESS")])
    return sorted(batches & succeeded)


def read_batch(hdfs_bin: str, batch: str) -> list[dict]:
    from relations import article_relations

    blob = hdfs_call(hdfs_bin, "-cat", f"{batch}/data.parquet", binary=True, timeout=600)
    table = pq.read_table(io.BytesIO(blob), columns=COLUMNS)
    rows = []
    for record in table.to_pylist():
        for hit in article_relations(record.get("title") or "", record.get("content") or "",
                                     record.get("companies") or [],
                                     record.get("language") or "ko"):
            rows.append({**hit, "url_hash": record["url_hash"]})
    return rows


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"completed": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state.get("completed"), list):
        raise RunnerError(f"state file is not a relation-loader state: {path}")
    return state


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def run(connection, *, root: str, state_path: Path, hdfs_bin: str,
        commit: bool, max_batches: int, drop_enumerations: bool) -> dict:
    state = read_state(state_path)
    done = set(state["completed"])
    pending = [b for b in discover_batches(hdfs_bin, root) if b not in done]
    if max_batches:
        pending = pending[:max_batches]

    totals = {"batches": 0, "incoming": 0, "resolved": 0, "relations_added": 0,
              "sentences_added": 0, "evidence_added": 0, "scores_added": 0, "unmatched": 0}
    for batch in pending:
        rows = read_batch(hdfs_bin, batch)
        if drop_enumerations:
            rows = [r for r in rows if not r.get("enumeration")]
        report = load_relations(connection, rows, commit=commit)
        totals["batches"] += 1
        for key in totals:
            if key != "batches":
                totals[key] += report.get(key, 0)
        print(json.dumps({"batch": batch.rsplit("/", 1)[-1], **report},
                         ensure_ascii=False), flush=True)
        if commit:
            done.add(batch)
            write_state(state_path, {"completed": sorted(done)})
    return {**totals, "pending_at_start": len(pending),
            "mode": "commit" if commit else "dry-run"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--dsn", default=os.environ.get("COSMOS_DSN"))
    parser.add_argument("--hdfs-bin", default=os.environ.get("HDFS_BIN", "/opt/hadoop/bin/hdfs"))
    parser.add_argument("--max-batches", type=int, default=0, help="0 reads every pending batch")
    parser.add_argument("--keep-enumerations", action="store_true",
                        help="Keep 'A·B·C 와 협약' rows, where the parties contracted "
                             "with a fourth party rather than with each other")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true")
    mode.add_argument("--dry-run", action="store_true",
                      help="Default: exercise all writes then roll them back")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn or COSMOS_DSN is required")

    from services.sentiment_loader.runner import connect_when_free

    with connect_when_free(args.dsn) as connection:
        connection.autocommit = True
        summary = run(connection, root=args.root, state_path=args.state_file,
                      hdfs_bin=args.hdfs_bin, commit=args.commit,
                      max_batches=args.max_batches,
                      drop_enumerations=not args.keep_enumerations)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
