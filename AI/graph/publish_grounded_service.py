"""Publish an immutable, grounded service projection through PostgreSQL views.

Run on the service host as a user authorized to execute docker. No DB passwords
are accepted or printed: psql connects through the PostgreSQL container's socket.
Flyway V7 must already be applied by the matching backend. The first command
captures the old public read model, the second stages a fully verified payload,
and the third atomically switches the selected run. None replaces public scores,
collection rows, users or scraps. Missing public document/relationship identities
are inserted only to preserve the existing user-write foreign-key contract.

Unknown source collection dates remain NULL in the version. A newly inserted
public identity records this import's receipt time because the legacy public
schema requires collection timestamps; this is NOT a reconstructed source date.
The immutable document context explains the distinction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

TABLES = (
    "source_document", "news_article", "disclosure", "company_document",
    "document_evidence", "company_relationship", "relationship_evidence",
    "relationship_score_current", "relationship_score_history",
    "company_metric_history", "graph_snapshot", "graph_snapshot_load",
)
CONTEXTS = ("document_context", "relationship_context", "evidence_context")
LEGACY_RUN = "legacy-service-before-grounded-20260919-v1"
RUN = "lake-grounded-20260919-v1"
OLD_SNAPSHOT = "ab19d794-ad08-51b3-8a91-a060bcdfdafc"
NEW_SNAPSHOT = "a70eb366-22cd-59de-88a3-fc7bceac09ca"
PUBLICATION_SHA = "23f3b08f8bd79d7496c0f1c5cdb799234971ba830b8d3ed0e1f8618de25e52d8"
BACKUP_SHA = "dbe1376026d2fb2808b93dbbd2c32906532cd90252d23434a8ed900e86a44d69"


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return '"' + value + '"'


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def assertion(predicate: str, label: str) -> str:
    return f"DO $assert$ BEGIN IF ({predicate}) IS NOT TRUE THEN RAISE EXCEPTION {literal(label)}; END IF; END $assert$;\n"


def command(args) -> list[str]:
    if args.database not in {"cosmos", "cosmos_grounded_review_20260919"}:
        raise ValueError("Database is not an authorized target")
    return ["docker", "exec", "-i", "cosmos-postgres-1", "psql", "-X", "-qAt",
            "--set=ON_ERROR_STOP=1", "-U", "cosmos", "-d", args.database]


def execute(args, chunks) -> str:
    """Stream SQL/COPY without command interpolation or a multi-GB memory copy."""
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as output:
        process = subprocess.Popen(command(args), stdin=subprocess.PIPE,
                                   stdout=output, stderr=subprocess.STDOUT)
        try:
            assert process.stdin is not None
            for chunk in chunks:
                process.stdin.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            process.stdin.close()
            result = process.wait()
        except BaseException:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.wait()
            raise
    if result:
        raise RuntimeError(f"psql failed ({result}); inspect restricted server log {log}")
    text = log.read_text(encoding="utf-8")
    print(text[-18000:], flush=True)
    return text


def begin() -> str:
    return ("BEGIN ISOLATION LEVEL REPEATABLE READ;\nSET LOCAL lock_timeout='20s';\n"
            "SET LOCAL statement_timeout='30min';\nSET LOCAL standard_conforming_strings=on;\n"
            "SET LOCAL timezone='UTC';\nSELECT pg_advisory_xact_lock(205,20260919);\n"
            + assertion("to_regclass('cosmos_analysis.runs') IS NOT NULL", "Flyway V7 is required"))


def report(run: str) -> str:
    counts = ",".join(f"{literal(t)},(SELECT count(*) FROM cosmos_analysis.{t} WHERE run_id={literal(run)})"
                      for t in TABLES + CONTEXTS)
    return ("SELECT jsonb_build_object('database',current_database(),'activeRun',"
            "(SELECT run_id FROM cosmos_analysis.active_run),'run',"
            f"(SELECT to_jsonb(r) FROM cosmos_analysis.runs r WHERE run_id={literal(run)}),"
            f"'actualCounts',jsonb_build_object({counts}));\n")


def capture_legacy(args):
    qrun = literal(LEGACY_RUN)
    sql = begin()
    sql += assertion(f"(SELECT count(*)=1 FROM public.graph_snapshot WHERE snapshot_id={literal(OLD_SNAPSHOT)}::uuid AND status='PUBLISHED')",
                     "Legacy snapshot changed; inspect before capturing")
    sql += assertion("NOT EXISTS(SELECT 1 FROM cosmos_analysis.active_run)", "Active run already exists; legacy capture cannot overwrite it")
    sql += f"""
INSERT INTO cosmos_analysis.runs(run_id,snapshot_id,as_of_at,publication_sha256,status,metadata,published_at)
SELECT {qrun},snapshot_id,as_of_at,{literal(BACKUP_SHA)},'VALIDATED',
jsonb_build_object('display_name','이전 분석 데이터','origin','public tables captured before grounded publication',
                  'verification','legacy operational data, not newly grounded',
                  'backup_sha256',{literal(BACKUP_SHA)}),published_at
FROM public.graph_snapshot WHERE snapshot_id={literal(OLD_SNAPSHOT)}::uuid;
"""
    for table in TABLES:
        sql += f"INSERT INTO cosmos_analysis.{table} SELECT {qrun}, p.* FROM public.{table} p;\n"
    sql += f"""
INSERT INTO cosmos_analysis.document_context
(run_id,document_id,source_revision_id,sort_at,published_at_raw,publication_precision,payload)
SELECT {qrun},document_id,'legacy:'||document_id,
       coalesce(published_at,last_collected_at,first_collected_at,created_at),
       published_at::text,CASE WHEN published_at IS NULL THEN 'UNKNOWN' ELSE 'LEGACY_TIMESTAMP' END,
       jsonb_build_object('origin','legacy service public data','source_time_verified',false)
FROM public.source_document;
INSERT INTO cosmos_analysis.evidence_context
(run_id,evidence_id,source_revision_id,source_kind,available_at,published_at_raw,publication_precision,payload)
SELECT {qrun},e.evidence_id,'legacy:'||e.document_id,d.document_type,
       coalesce(d.published_at,d.last_collected_at,d.first_collected_at,d.created_at),
       d.published_at::text,CASE WHEN d.published_at IS NULL THEN 'UNKNOWN' ELSE 'LEGACY_TIMESTAMP' END,
       jsonb_build_object('origin','legacy service public data')
FROM public.document_evidence e JOIN public.source_document d USING(document_id);
INSERT INTO cosmos_analysis.relationship_context(run_id,relationship_id,explanation,payload)
SELECT {qrun},relationship_id,NULL,jsonb_build_object('origin','legacy service public data')
FROM public.company_relationship;
INSERT INTO cosmos_analysis.active_run(singleton,run_id) VALUES(true,{qrun});
"""
    count_fields = ",".join(f"{literal(t)},(SELECT count(*) FROM cosmos_analysis.{t} WHERE run_id={qrun})" for t in TABLES)
    sql += f"UPDATE cosmos_analysis.runs SET counts=jsonb_build_object({count_fields}) WHERE run_id={qrun};\n"
    sql += report(LEGACY_RUN) + "COMMIT;\n"
    sql += "ANALYZE cosmos_analysis.runs;\nANALYZE cosmos_analysis.active_run;\n"
    execute(args, [sql])


def copy_json(path: Path):
    # JSON escapes all ASCII control characters; these CSV control bytes can
    # therefore never be delimiters or quotes inside a serialized JSON record.
    yield "CREATE TEMP TABLE payload_rows (data jsonb) ON COMMIT DROP;\n"
    yield "COPY payload_rows(data) FROM STDIN WITH (FORMAT csv, DELIMITER E'\\x1e', QUOTE E'\\x1f');\n"
    with path.open("rb") as handle:
        for line in handle:
            if any(c in line for c in (b"\x1e", b"\x1f", b"\x00")):
                raise ValueError(f"Unescaped control byte in {path.name}")
            yield line if line.endswith(b"\n") else line + b"\n"
    yield "\\.\n"


def validate_version(run: str) -> str:
    r = literal(run)
    checks = [
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.source_document v LEFT JOIN public.data_source p USING(source_id) WHERE v.run_id={r} AND p.source_id IS NULL)", "Unknown source id"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.news_article n LEFT JOIN cosmos_analysis.source_document d ON d.run_id=n.run_id AND d.document_id=n.document_id WHERE n.run_id={r} AND (d.document_id IS NULL OR d.document_type<>'NEWS'))", "News document FK/type mismatch"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.disclosure n LEFT JOIN cosmos_analysis.source_document d ON d.run_id=n.run_id AND d.document_id=n.document_id LEFT JOIN public.company c ON c.company_id=n.filing_company_id WHERE n.run_id={r} AND (d.document_id IS NULL OR d.document_type<>'DISCLOSURE' OR c.company_id IS NULL))", "Disclosure document/company mismatch"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.company_document n LEFT JOIN cosmos_analysis.source_document d ON d.run_id=n.run_id AND d.document_id=n.document_id LEFT JOIN public.company c USING(company_id) WHERE n.run_id={r} AND (d.document_id IS NULL OR c.company_id IS NULL))", "Company-document FK mismatch"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.document_evidence e LEFT JOIN cosmos_analysis.company_document c ON c.run_id=e.run_id AND c.document_id=e.document_id AND c.company_id=e.company_id WHERE e.run_id={r} AND c.company_id IS NULL)", "Evidence company-document mismatch"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.relationship_evidence e LEFT JOIN cosmos_analysis.company_relationship r ON r.run_id=e.run_id AND r.relationship_id=e.relationship_id LEFT JOIN cosmos_analysis.document_evidence d ON d.run_id=e.run_id AND d.evidence_id=e.evidence_id WHERE e.run_id={r} AND (r.relationship_id IS NULL OR d.evidence_id IS NULL OR d.document_id<>e.document_id))", "Relationship evidence FK mismatch"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.company_relationship v LEFT JOIN public.company s ON s.company_id=v.source_company_id LEFT JOIN public.company t ON t.company_id=v.target_company_id LEFT JOIN public.relationship_type k USING(relationship_type_id) WHERE v.run_id={r} AND (s.company_id IS NULL OR t.company_id IS NULL OR k.relationship_type_id IS NULL OR s.company_id=t.company_id))", "Relationship entity mismatch"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.source_document d LEFT JOIN cosmos_analysis.document_context c ON c.run_id=d.run_id AND c.document_id=d.document_id WHERE d.run_id={r} AND c.document_id IS NULL)", "Document context missing"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.document_evidence d LEFT JOIN cosmos_analysis.evidence_context c ON c.run_id=d.run_id AND c.evidence_id=d.evidence_id WHERE d.run_id={r} AND c.evidence_id IS NULL)", "Evidence provenance missing"),
        (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.news_article n JOIN public.news_article p USING(document_id) WHERE n.run_id={r} AND p.canonical_url_hash<>n.canonical_url_hash)", "Public news identity collision"),
    ]
    for table in ("relationship_score_current", "relationship_score_history"):
        checks += [
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.{table} s LEFT JOIN cosmos_analysis.company_relationship r ON r.run_id=s.run_id AND r.relationship_id=s.relationship_id LEFT JOIN cosmos_analysis.graph_snapshot g ON g.run_id=s.run_id AND g.snapshot_id=s.snapshot_id WHERE s.run_id={r} AND (r.relationship_id IS NULL OR g.snapshot_id IS NULL))", f"{table} snapshot/relationship mismatch"),
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.{table} WHERE run_id={r} AND (window_type NOT IN ('7D','30D','90D') OR score IS NULL OR score<0 OR score>100 OR (news_score IS NULL AND disclosure_score IS NULL) OR abs(score-CASE WHEN news_score IS NULL THEN disclosure_score WHEN disclosure_score IS NULL THEN news_score ELSE (news_score+disclosure_score)/2 END)>0.0001))", f"{table} component formula mismatch"),
        ]
    if run == RUN:
        checks += [
            (f"(SELECT count(*)=1 FROM cosmos_analysis.graph_snapshot WHERE run_id={r}) AND EXISTS(SELECT 1 FROM cosmos_analysis.graph_snapshot s JOIN cosmos_analysis.runs m ON m.run_id=s.run_id AND m.snapshot_id=s.snapshot_id AND m.as_of_at=s.as_of_at WHERE s.run_id={r} AND s.snapshot_id={literal(NEW_SNAPSHOT)}::uuid AND s.status='PUBLISHED')", "Snapshot row and run metadata differ"),
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.relationship_score_current s JOIN cosmos_analysis.runs m USING(run_id) WHERE s.run_id={r} AND (s.snapshot_id<>m.snapshot_id OR s.as_of_at<>m.as_of_at))", "Current score snapshot/as-of differs"),
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.relationship_score_history s JOIN cosmos_analysis.runs m USING(run_id) WHERE s.run_id={r} AND (s.snapshot_id<>m.snapshot_id OR s.period_end<>m.as_of_at OR s.period_start<>m.as_of_at-make_interval(days=>replace(s.window_type,'D','')::integer)))", "History score UTC window differs"),
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.company_document WHERE run_id={r} AND (impact_score IS NOT NULL OR confidence IS NOT NULL OR relevance_score IS NOT NULL))", "Uncomputed model values must remain NULL"),
            (f"(SELECT count(*)=196941 FROM cosmos_analysis.source_document WHERE run_id={r} AND document_type='NEWS' AND published_at IS NULL)", "Unknown news publication count changed"),
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.source_document WHERE run_id={r} AND document_type='NEWS' AND (first_collected_at IS NOT NULL OR last_collected_at IS NOT NULL))", "Unknown collection time was fabricated"),
            (f"(SELECT count(*)=344 FROM cosmos_analysis.company_metric_history WHERE run_id={r} AND positive_count IS NULL AND negative_count IS NULL)", "Unknown sentiment counts changed"),
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.company_document WHERE run_id={r} AND sentiment IS NOT NULL AND sentiment NOT IN ('POSITIVE','NEGATIVE','NEUTRAL'))", "Invalid sentiment category"),
            (f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.relationship_score_current WHERE run_id={r} AND (confidence IS NOT NULL OR as_of_at<>'2026-09-16T00:00:00Z'::timestamptz))", "Score confidence/as-of changed"),
        ]
    return "".join(assertion(condition, label) for condition, label in checks)


def public_identities(run: str) -> str:
    r = literal(run)
    sql = ""
    # These are stable FK anchors only. Existing records are never overwritten.
    for table in ("source_document", "news_article", "disclosure", "company_relationship"):
        substitutions = ""
        if table == "source_document":
            substitutions = " || jsonb_build_object('first_collected_at',CURRENT_TIMESTAMP,'last_collected_at',CURRENT_TIMESTAMP)"
        sql += f"""
INSERT INTO public.{table}
SELECT decoded.* FROM cosmos_analysis.{table} v
CROSS JOIN LATERAL jsonb_populate_record(NULL::public.{table},to_jsonb(v)-'run_id'{substitutions}) decoded
WHERE v.run_id={r}
ON CONFLICT DO NOTHING;
"""
    sql += assertion(f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.news_article v LEFT JOIN public.news_article p ON p.document_id=v.document_id AND p.canonical_url_hash=v.canonical_url_hash WHERE v.run_id={r} AND p.document_id IS NULL)", "Concurrent news identity changed; rebuild mapping before retry")
    sql += assertion(f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.disclosure v LEFT JOIN public.disclosure p ON p.document_id=v.document_id AND p.filing_system=v.filing_system AND p.dart_receipt_no IS NOT DISTINCT FROM v.dart_receipt_no AND p.sec_cik IS NOT DISTINCT FROM v.sec_cik AND p.sec_accession_no IS NOT DISTINCT FROM v.sec_accession_no WHERE v.run_id={r} AND p.document_id IS NULL)", "Disclosure identity changed; rebuild mapping before retry")
    sql += assertion(f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.company_relationship v LEFT JOIN public.company_relationship p ON p.relationship_id=v.relationship_id AND p.source_company_id=v.source_company_id AND p.target_company_id=v.target_company_id AND p.relationship_type_id=v.relationship_type_id WHERE v.run_id={r} AND p.relationship_id IS NULL)", "Relationship identity mismatch")
    return sql


def stage(args):
    directory = Path(args.payload).resolve()
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Build tool and this publisher share a deliberately small, explicit format.
    if manifest["run_id"] != RUN or manifest["publication_sha256"] != PUBLICATION_SHA:
        raise ValueError("Unexpected grounded run or HDFS publication hash")
    if manifest['snapshot_id'] != NEW_SNAPSHOT or manifest['as_of'] not in {
            '2026-09-16T00:00:00Z', '2026-09-16T00:00:00+00:00'}:
        raise ValueError('Unexpected snapshot id or source as-of')
    all_files = manifest["files"]
    files = {('data_source' if name == 'data_sources' else name): value
             for name, value in all_files.items() if name != 'mapping'}
    if manifest.get('status') != 'PASS':
        raise ValueError('Payload validation did not pass')
    if set(files) != set(TABLES + CONTEXTS + ("data_source",)):
        raise ValueError("Payload table set differs from the version contract")
    for table, info in all_files.items():
        path = (directory / info["path"]).resolve()
        if directory not in path.parents or digest(path) != info["sha256"]:
            raise ValueError(f"Payload file checksum or path mismatch: {table}")
    qrun = literal(RUN)
    metadata = {"display_name": "근거 검증 분석 데이터", "payload_manifest_sha256": digest(manifest_path),
                "source_as_of": "2026-09-16T00:00:00Z", "automatic_refresh": False,
                "upstream_collection_time": "unknown; public-only identity import receipt is not source collection time",
                "gnn_impact": "not computed in this data run", "explanation": "retrieved exact evidence; no generative LLM"}
    counts = {table: info["rows"] for table, info in files.items() if table != "data_source"}

    def chunks():
        yield begin()
        yield assertion(f"(SELECT run_id={literal(LEGACY_RUN)} FROM cosmos_analysis.active_run)", "Expected legacy active run before staging")
        yield assertion(f"NOT EXISTS(SELECT 1 FROM cosmos_analysis.runs WHERE run_id={qrun})", "Immutable run already exists; use audit/activate, not overwrite")
        yield f"INSERT INTO cosmos_analysis.runs(run_id,snapshot_id,as_of_at,publication_sha256,status,counts,metadata,supersedes_run_id) VALUES({qrun},{literal(NEW_SNAPSHOT)}::uuid,'2026-09-16T00:00:00Z',{literal(PUBLICATION_SHA)},'STAGING',{literal(json.dumps(counts))}::jsonb,{literal(json.dumps(metadata,ensure_ascii=False))}::jsonb,{literal(LEGACY_RUN)});\n"
        for table in ("data_source",) + TABLES + CONTEXTS:
            info = files[table]
            yield from copy_json(directory / info["path"])
            yield assertion(f"(SELECT count(*)={int(info['rows'])} FROM payload_rows)", f"Wrong payload row count: {table}")
            if table == "data_source":
                yield "INSERT INTO public.data_source SELECT decoded.* FROM payload_rows CROSS JOIN LATERAL jsonb_populate_record(NULL::public.data_source,data) decoded ON CONFLICT DO NOTHING;\n"
            else:
                yield f"INSERT INTO cosmos_analysis.{table} SELECT decoded.* FROM payload_rows CROSS JOIN LATERAL jsonb_populate_record(NULL::cosmos_analysis.{table},data || jsonb_build_object('run_id',{qrun})) decoded;\n"
            yield "DROP TABLE payload_rows;\n"
            if table != 'data_source':
                # A new run is absent from old column statistics. Before this
                # ANALYZE PostgreSQL can estimate each run as one row and choose
                # an O(N^2) unparameterized nested-loop FK audit.
                yield f"ANALYZE cosmos_analysis.{table};\n"
        yield validate_version(RUN)
        yield public_identities(RUN)
        yield f"UPDATE cosmos_analysis.runs SET status='VALIDATED' WHERE run_id={qrun};\n"
        yield report(RUN)
        yield "COMMIT;\n"
        for table in TABLES + CONTEXTS + ("runs", "active_run"):
            yield f"ANALYZE cosmos_analysis.{table};\n"
    execute(args, chunks())


def activate(args):
    if args.run_id not in {RUN, LEGACY_RUN} or args.expected_active not in {RUN, LEGACY_RUN}:
        raise ValueError("Unrecognized run selection")
    r = literal(args.run_id)
    sql = begin()
    sql += assertion(f"(SELECT run_id={literal(args.expected_active)} FROM cosmos_analysis.active_run)", "Active run changed concurrently")
    sql += assertion(f"(SELECT status IN ('VALIDATED','PUBLISHED') FROM cosmos_analysis.runs WHERE run_id={r})", "Run is not validated")
    if args.run_id == RUN:
        sql += validate_version(RUN)
        sql += assertion(f"(SELECT publication_sha256={literal(PUBLICATION_SHA)} FROM cosmos_analysis.runs WHERE run_id={r})", "Publication checksum mismatch")
    sql += f"UPDATE cosmos_analysis.active_run SET run_id={r} WHERE singleton;\n"
    sql += f"UPDATE cosmos_analysis.runs SET status='PUBLISHED',published_at=coalesce(published_at,CURRENT_TIMESTAMP) WHERE run_id={r};\n"
    sql += report(args.run_id) + "COMMIT;\n"
    # A newly created singleton table can otherwise be estimated as 1,330 rows,
    # producing expensive joins across current_* views after publication.
    sql += "ANALYZE cosmos_analysis.runs;\nANALYZE cosmos_analysis.active_run;\n"
    execute(args, [sql])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture-legacy", "stage", "activate", "audit"])
    parser.add_argument("--database", required=True)
    parser.add_argument("--payload")
    parser.add_argument("--run-id", default=RUN)
    parser.add_argument("--expected-active", default=LEGACY_RUN)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    start = time.monotonic()
    if args.action == "capture-legacy":
        capture_legacy(args)
    elif args.action == "stage":
        stage(args)
    elif args.action == "activate":
        activate(args)
    else:
        execute(args, [report(args.run_id)])
    print(json.dumps({"action": args.action, "database": args.database,
                      "elapsed_seconds": round(time.monotonic()-start, 2), "status": "PASS"}), flush=True)


if __name__ == "__main__":
    main()
