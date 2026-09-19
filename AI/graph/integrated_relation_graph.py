"""Auditable NEWS/DISCLOSURE relation evidence, window scores and GraphRAG retrieval.

This is a research artifact, not a production loader or a price-impact model.
Every relation retains a literal quotation in an immutable source document.
The four public relation types remain separate; same-industry and co-mention
are not relation evidence. Direction is preserved for supply and investment.

The existing rel-v0.3-components display formula is reused. A news assertion
without a calibrated confidence receives one *document support unit*, not a
probability of correctness. Syndicated/repeated stories are not independent
events; per-document deduplication alone does not establish independence.

Known calendar dates with missing timezone become available after the END of
that date in UTC-12 (next day 12:00 UTC), explicitly marked conservative-day
availability. This is later than the existing display-only UTC-day policy.
Malformed dates are quarantined. Graph training must use available_at STRICTLY
before the feature cutoff. This is observed evidence, not proof that a contract
or an ownership position remains in force forever.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from relationship_score_components import TYPES, aggregate, normalize, timestamp

SCHEMA = "cosmos-integrated-relation-graph-1"
UTC = dt.timezone.utc


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_jsonl(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def available_time(raw):
    """Return an aware timestamp plus its policy, never infer local timezone."""
    require(isinstance(raw, str) and raw.strip(), "Missing publication date")
    raw = raw.strip()
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid publication date: " + raw) from error
    if value.tzinfo is None:
        value = dt.datetime.combine(value.date() + dt.timedelta(days=1), dt.time(12), UTC)
        precision = "calendar_day_finished_worldwide_utc_minus_12"
    else:
        value = value.astimezone(UTC)
        precision = "timezone_aware_publication"
    require(2000 <= value.year <= 2100, "Publication year outside supported range")
    return value.isoformat().replace("+00:00", "Z"), precision


def validate_evidence(row, document):
    require(row.get("source_kind") in {"NEWS", "DISCLOSURE"}, "Unknown evidence source")
    require(row.get("rel_type") in TYPES, "Unsupported relation type")
    require(all(isinstance(row.get(k), str) and row[k].strip() == row[k] and row[k]
                for k in ("evidence_id", "record_id", "document_id", "src_ticker", "dst_ticker")), "Missing evidence identity")
    require(row["src_ticker"] != row["dst_ticker"], "Self relation")
    require(type(row.get("directed")) is bool and row["directed"] == (row["rel_type"] in {"SUPPLY", "INVEST"}), "Direction/type mismatch")
    require(row["document_id"] == document["document_id"], "Document identity mismatch")
    require(row["source_kind"] == document["source_kind"], "Document source mismatch")
    require(row["record_id"] == document["record_id"], "Filing/article identity mismatch")
    text = document["text"]
    require(hashlib.sha256(text.encode("utf-8")).hexdigest() == document["text_sha256"], "Source text hash mismatch")
    start, end = row.get("quote_start"), row.get("quote_end")
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(text)
            and text[start:end] == row.get("quote"), "Quotation differs from source span")
    require(row.get("published_at") == document.get("published_at")
            and row.get("source_url") == document.get("source_url"), "Source metadata mismatch")
    if row["source_kind"] == "NEWS":
        for endpoint in ("source", "target"):
            require(isinstance(row.get(endpoint + "_quote"), str) and bool(row[endpoint + "_quote"])
                    and row[endpoint + "_quote"] in row["quote"], "Missing company in literal news evidence")
    else:
        # A DART table row names the holder; the reporting corporation is
        # identified separately by filing metadata, never inserted into quote.
        role = row.get("filing_role", "target" if row["rel_type"] == "INVEST" else "source")
        require(role in {"source", "target"}, "Unknown reporting-company role")
        require(document.get("filing_ticker") == row["src_ticker" if role == "source" else "dst_ticker"], "Disclosure reporting entity mismatch")
        require(bool(document.get("filing_corp_code")), "Missing reporting corporation identity")
        other = "target" if role == "source" else "source"
        require(isinstance(row.get(other + "_quote"), str) and bool(row[other + "_quote"])
                and row[other + "_quote"] in row["quote"], "Missing other party in literal filing evidence")
    availability_raw = row["published_at"][:10] if row.get("published_at_precision") == "date" else row["published_at"]
    available, policy = available_time(availability_raw)
    if "available_at" in row:
        require(row["available_at"] == available, "Evidence availability mismatch")
    weight = float(row.get("support_weight", 1.0))
    require(math.isfinite(weight) and 0 <= weight <= 1, "Invalid document support weight")
    return {**row, "available_at": available, "availability_policy": policy,
            "support_weight": weight, "support_weight_is_probability": False,
            "price_effect_established": False, "production_approved": False}


def init_db(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE documents(document_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, src_ticker TEXT NOT NULL,
            dst_ticker TEXT NOT NULL, rel_type TEXT NOT NULL, source_kind TEXT NOT NULL,
            available_at TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents,
            payload TEXT NOT NULL);
        CREATE INDEX evidence_pair ON evidence(rel_type,src_ticker,dst_ticker);
        CREATE INDEX evidence_time ON evidence(available_at);
    """)
    return connection


def insert_evidence(connection, row, document):
    row = validate_evidence(row, document)
    encoded = json_text(document)
    old = connection.execute("SELECT payload FROM documents WHERE document_id=?", (document["document_id"],)).fetchone()
    require(old is None or old[0] == encoded, "Conflicting versions of one source document")
    connection.execute("INSERT OR IGNORE INTO documents VALUES (?,?)", (document["document_id"], encoded))
    existing = connection.execute("SELECT payload FROM evidence WHERE evidence_id=?", (row["evidence_id"],)).fetchone()
    encoded_row = json_text(row)
    require(existing is None or existing[0] == encoded_row, "Conflicting evidence identity")
    connection.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?)",
                       (row["evidence_id"], row["src_ticker"], row["dst_ticker"], row["rel_type"],
                        row["source_kind"], row["available_at"], row["document_id"], encoded_row))
    return row


def score_rows(evidence, companies, as_of):
    rows = []
    for row in evidence:
        rows.append(normalize({"src_ticker": row["src_ticker"], "dst_ticker": row["dst_ticker"],
                               "rel_type": row["rel_type"], "record_id": row["record_id"],
                               "published_date": row["available_at"], "confidence": row["support_weight"]}, row["source_kind"]))
    return aggregate(rows, companies, timestamp(as_of))


def finalize(connection, directory, *, companies, as_of, provenance, coverage):
    directory = Path(directory)
    connection.commit()
    require(connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "SQLite integrity failure")
    require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "Broken evidence source reference")
    evidence = [json.loads(r[0]) for r in connection.execute("SELECT payload FROM evidence ORDER BY evidence_id")]
    scores, unknown = score_rows(evidence, companies, as_of)
    require(not unknown, "Unknown company in score inputs: " + str(unknown))
    for score in scores:
        score["confidence_meaning"] = "document_support_weight_not_prediction_probability"
        score["production_approved"] = False
        score["coverage_complete"] = False
    counts = {kind: sum(row["source_kind"] == kind for row in evidence) for kind in ("NEWS", "DISCLOSURE")}
    relations = {(*(sorted((r["src_ticker"], r["dst_ticker"])) if not r["directed"] else (r["src_ticker"], r["dst_ticker"])), r["rel_type"]) for r in evidence}
    connection.close()
    files = {}
    for key, name, rows in (("temporal_evidence", "temporal_evidence.jsonl", evidence),
                             ("relationship_scores", "relationship_scores.jsonl", scores)):
        path = directory / name
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json_text(row) + "\n")
        files[key] = {"path": name, "sha256": sha(path), "rows": len(rows), "bytes": path.stat().st_size}
    database = directory / "integrated.sqlite3"
    files["database"] = {"path": database.name, "sha256": sha(database), "bytes": database.stat().st_size}
    manifest = {"schema": SCHEMA, "artifact_complete": True, "as_of": as_of,
                "files": files, "provenance": provenance, "coverage": coverage,
                "evidence_counts": counts, "typed_relationships": len(relations),
                "score_rows": len(scores), "by_window": {f"{w}D": sum(s["window_type"] == f"{w}D" for s in scores) for w in (7, 30, 90)},
                "code_sha256": sha(Path(__file__)), "scoring_code_sha256": sha(Path(__file__).with_name("relationship_score_components.py")),
                "temporal_policy": "publication_time_replay_retrospective_collection_strict_prior_cutoff_not_persistent_contract_state",
                "live_historical_collection_availability_verified": False,
                "score_policy": "rel-v0.3-components_document_support_not_probability_or_price_effect",
                "human_gold": False, "graph_quality_validated": False,
                "production_approved": False, "price_effect_established": False}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


class IntegratedRelationRetriever:
    """Exact typed relation GraphRAG across verified news and disclosure evidence."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        path = self.directory / "manifest.json"
        self.manifest = read_json(path)
        require(self.manifest.get("schema") == SCHEMA and self.manifest.get("artifact_complete") is True, "Incomplete integrated graph")
        for spec in self.manifest["files"].values():
            file = (self.directory / spec["path"]).resolve()
            require(file.is_relative_to(self.directory) and sha(file) == spec["sha256"], "Integrated artifact hash differs")
        self.database = self.directory / self.manifest["files"]["database"]["path"]
        self._require_no_wal()
        self.provenance = {"integrated_manifest_sha256": sha(path), "database_sha256": sha(self.database),
                           "retriever_code_sha256": sha(Path(__file__)), "retrieval_policy": "exact_typed_relation_both_sources_v1"}

    def _require_no_wal(self):
        path = Path(str(self.database) + "-wal")
        require(not path.exists() or path.stat().st_size == 0, "Integrated graph has an uncheckpointed WAL")

    def retrieve(self, src_ticker, dst_ticker, rel_type, *, as_of=None, limit=5):
        require(src_ticker != dst_ticker and all(isinstance(t, str) and t.strip() == t and t for t in (src_ticker, dst_ticker)), "Two distinct company identifiers required")
        require(rel_type in TYPES, "Unknown relation type")
        require(type(limit) is int and 1 <= limit <= 20, "limit must be 1..20")
        cutoff = timestamp(as_of) if as_of else None
        symmetric = rel_type in {"PARTNER", "COMPETE"}
        condition, params = "e.src_ticker=? AND e.dst_ticker=?", [rel_type, src_ticker, dst_ticker]
        if symmetric:
            condition = "(" + condition + ") OR (e.src_ticker=? AND e.dst_ticker=?)"
            params += [dst_ticker, src_ticker]
        self._require_no_wal()
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            rows = connection.execute(f"SELECT e.payload,d.payload FROM evidence e JOIN documents d USING(document_id) WHERE e.rel_type=? AND ({condition})", params).fetchall()
        finally:
            connection.close()
        eligible, excluded = [], 0
        for raw, source in rows:
            row = validate_evidence(json.loads(raw), json.loads(source))
            if cutoff is not None and timestamp(row["available_at"]) >= cutoff:
                excluded += 1
                continue
            eligible.append(row)
        eligible.sort(key=lambda r: (timestamp(r["available_at"]), r["evidence_id"]), reverse=True)
        picked, seen = [], set()
        for row in eligible:
            key = (row["source_kind"], row["record_id"], row["quote"])
            if key in seen:
                continue
            seen.add(key)
            if len(picked) >= limit:
                continue
            number = "E" + str(len(picked) + 1)
            picked.append({**row, "id": number, "citation": "[" + number + "]", "text": row["quote"],
                           "kind": row["rel_type"], "date": row["available_at"][:10]})
        return {"schema": SCHEMA, "status": "FOUND" if picked else "NO_EVIDENCE",
                "relationship": {"src_ticker": src_ticker, "dst_ticker": dst_ticker, "rel_type": rel_type, "directed": not symmetric},
                "as_of": as_of, "evidence": picked, "eligible_unique_evidence": len(seen),
                "source_kind": "NEWS_AND_DISCLOSURE", "disclosures_searched": True,
                "sources_returned": sorted({r["source_kind"] for r in picked}),
                "excluded_unknown_or_after_cutoff": excluded, "retrieval_provenance": self.provenance,
                "production_approved": False, "graph_quality_validated": False, "price_effect_established": False}


class IntegratedRelationExplainer:
    """Use verified news paraphrases; retain filing tables as literal evidence.

    Filing rows often omit the reporting-company name. Sentence-only NLI cannot
    establish their metadata-to-company binding, so a prose generation would
    be misleadingly marked verified. Such results explicitly use an extractive
    explanation and expose the separately verified filing identity.
    """
    def __init__(self, retriever, bundle=None, device="cpu", *, generator=None, verifier=None):
        from grounded_relation_graphrag import GroundedRelationExplainer
        options = {} if verifier is None else {"verifier": verifier}
        self.retriever = retriever
        self.news_explainer = GroundedRelationExplainer(retriever, bundle, device, generator=generator, **options)

    def explain(self, src_ticker, dst_ticker, rel_type, *, as_of=None, limit=5):
        result = self.retriever.retrieve(src_ticker, dst_ticker, rel_type, as_of=as_of, limit=limit)
        if not any(e["source_kind"] == "DISCLOSURE" for e in result["evidence"]):
            return self.news_explainer.explain(src_ticker, dst_ticker, rel_type, as_of=as_of, limit=limit)
        chosen = [{"evidence_id": e["id"], "quote": e["quote"]} for e in result["evidence"][:2]]
        return {**result, "status": "EXTRACTIVE_FALLBACK", "mode": "extractive",
                "claims": chosen, "evidence_ids": [c["evidence_id"] for c in chosen],
                "explanation": " ".join("“" + c["quote"] + "” [" + c["evidence_id"] + "]" for c in chosen),
                "generator_provenance": None, "verification_scores": [],
                "fallback_reason": "disclosure_table_reporting_entity_is_separate_verified_metadata",
                "note": "공시 표의 보고기업은 공시 메타데이터로 확인했습니다. 표 원문을 그대로 제시하며 현재 지분이나 주가 영향을 뜻하지 않습니다."}
