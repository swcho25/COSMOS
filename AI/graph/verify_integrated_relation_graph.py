"""Independent read-only checks of integrated quotes, dates, identities and scores."""
import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sqlite3


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert result.tzinfo is not None
    return result.astimezone(dt.timezone.utc)


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def verify(directory):
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    for spec in manifest["files"].values():
        path = (directory / spec["path"]).resolve()
        assert path.is_relative_to(directory) and file_sha(path) == spec["sha256"]
        assert path.stat().st_size == spec["bytes"]
        if "rows" in spec:
            assert len(read_rows(path)) == spec["rows"]
    path = directory / manifest["files"]["database"]["path"]
    wal = Path(str(path) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
        documents = {k: json.loads(v) for k, v in con.execute("SELECT document_id,payload FROM documents")}
        db_evidence = {k: json.loads(v) for k, v in con.execute("SELECT evidence_id,payload FROM evidence")}
    finally:
        con.close()
    for document in documents.values():
        assert hashlib.sha256(document["text"].encode("utf-8")).hexdigest() == document["text_sha256"]
    evidence = read_rows(directory / manifest["files"]["temporal_evidence"]["path"])
    assert len({r["evidence_id"] for r in evidence}) == len(evidence)
    assert db_evidence == {r["evidence_id"]: r for r in evidence}
    source_counts, type_counts = Counter(), Counter()
    expected = defaultdict(dict)
    as_of = timestamp(manifest["as_of"])
    for row in evidence:
        doc = documents[row["document_id"]]
        assert doc["text"][row["quote_start"]:row["quote_end"]] == row["quote"]
        assert row["source_kind"] == doc["source_kind"] and row["record_id"] == doc["record_id"]
        assert row["published_at"] == doc["published_at"] and row["source_url"] == doc["source_url"]
        assert row["rel_type"] in {"SUPPLY", "INVEST", "PARTNER", "COMPETE"}
        assert row["directed"] == (row["rel_type"] in {"SUPPLY", "INVEST"})
        assert row["src_ticker"] != row["dst_ticker"]
        if row["source_kind"] == "NEWS":
            assert row["source_quote"] and row["target_quote"]
            assert row["source_quote"] in row["quote"] and row["target_quote"] in row["quote"]
        else:
            role = row["filing_role"]
            assert doc["filing_ticker"] == row["src_ticker" if role == "source" else "dst_ticker"]
            assert doc["filing_corp_code"] == row["filing_corp_code"]
            assert row[("target" if role == "source" else "source") + "_quote"] in row["quote"]
        raw = row["published_at"][:10] if row.get("published_at_precision") == "date" else row["published_at"]
        original = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        expected_time = (dt.datetime.combine(original.date() + dt.timedelta(days=1), dt.time(12), dt.timezone.utc)
                         if original.tzinfo is None else original.astimezone(dt.timezone.utc))
        assert timestamp(row["available_at"]) == expected_time
        source_counts[row["source_kind"]] += 1
        type_counts[row["rel_type"]] += 1
        pair = (row["src_ticker"], row["dst_ticker"])
        if not row["directed"]:
            pair = tuple(sorted(pair))
        for days in (7, 30, 90):
            if as_of - dt.timedelta(days=days) <= expected_time < as_of:
                key = (*pair, row["rel_type"], str(days) + "D")
                doc_key = (row["source_kind"], row["record_id"])
                expected[key][doc_key] = max(expected[key].get(doc_key, 0), row["support_weight"])
    scores = read_rows(directory / manifest["files"]["relationship_scores"]["path"])
    assert len(scores) == len(expected)
    components = Counter()
    for row in scores:
        key = (row["source_stock_code"], row["target_stock_code"], row["relationship_type_code"], row["window_type"])
        docs = expected[key]
        calculated = []
        for source, field in (("NEWS", "news_score"), ("DISCLOSURE", "disclosure_score")):
            weights = [weight for (kind, _), weight in docs.items() if kind == source]
            value = round(100 * (1 - math.exp(-math.fsum(weights) / 5)), 6) if weights else None
            assert row[field] == value, (key, field, row[field], value)
            if value is not None:
                calculated.append(value)
        assert calculated and row["score"] == round(sum(calculated) / len(calculated), 6)
        assert row["evidence_count"] == len(docs)
        components[(row["window_type"], row["news_score"] is not None, row["disclosure_score"] is not None)] += 1
    deferred = manifest["provenance"]["deferred_news_evidence"]
    assert file_sha(directory / deferred["path"]) == deferred["sha256"]
    rejected = read_rows(directory / deferred["path"])
    assert len(rejected) == deferred["rows"]
    assert not {"NEWS:" + r["edge"]["edge_id"] for r in rejected} & db_evidence.keys()
    return {"checks_passed": True, "manifest_sha256": file_sha(directory / "manifest.json"),
            "quote_rows": len(evidence), "documents": len(documents), "sources": dict(source_counts),
            "types": dict(type_counts), "deferred_news_evidence": len(rejected),
            "scores_independently_recomputed": len(scores),
            "components": [{"window": key[0], "news_present": key[1], "disclosure_present": key[2], "rows": count}
                           for key, count in sorted(components.items())],
            "semantic_accuracy_estimated": False, "live_data_coverage_verified": False,
            "verifier_sha256": file_sha(Path(__file__))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError("Keep prior verification reports; select a fresh path")
    report = verify(args.graph)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
