"""Rebuild only provisional relation evidence from an immutable completed KG.

The existing articles, news/company decisions and sentiments are never changed.
Only articles with at least two candidate_ready endpoints enter a fixed queue.
Four relation types are supported: PARTNER, COMPETE, SUPPLY, INVEST; extractor
subtypes are retained. Article-specific identity concerns can defer endpoints,
without revoking their original news edges. Rule matches are not human gold or
validated financial effects. Repeated identical sentences in one article are
one evidence item; alternative source offsets are retained, not counted as
independent events. FULL/WAL transactions commit a whole batch with its cursor.

Example: python rebuild_grounded_relations.py --source-dir artifacts/kg_grounded_full_20260918_v1
 --out artifacts/kg_grounded_relations_20260919_v2 --endpoint-deny endpoint_review.json
Pass --resume after interruption; an existing STOP file pauses at a batch boundary.
--limit-articles creates an explicitly bounded separate pilot, never full coverage.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import build_grounded_kg as common

HERE = Path(__file__).resolve().parent
SCHEMA = "cosmos-grounded-relations-revision-1"
TYPES = frozenset(("PARTNER", "COMPETE", "SUPPLY", "INVEST"))
FLAGS = {**common.FLAGS, "scope": "relation_only_revision_of_provisional_candidate_graph"}
require, sha, digest, read, atomic = common.preview.require, common.sha, common.digest, common.read, common.atomic
canonical, meta, put = common.canonical, common.meta, common.put


def code_assets():
    names = ("rebuild_grounded_relations.py", "kg_grounded_relations_v2.py",
             "kg_grounded_relations.py", "build_grounded_kg.py", "knowledge_graph_news_preview.py")
    return {name: sha(HERE / name) for name in names}


def source_contract(source):
    source = Path(source).resolve()
    manifest = read(source / "manifest.json")
    require(manifest.get("schema") == "cosmos-grounded-candidate-kg-1", "Unexpected parent KG schema")
    require(manifest.get("artifact_complete") is True and manifest.get("graph_materialized") is True,
            "Parent KG is not complete")
    for key in ("human_gold", "production_approved", "graph_quality_validated", "ready_for_gnn_training", "price_effect_established"):
        require(manifest.get(key) is False, "Parent qualification flag differs: " + key)
    require(sha(source / "protocol.json") == manifest["protocol_sha256"], "Parent protocol changed")
    require(sha(source / "state.sqlite3") == manifest["state_sqlite_sha256"], "Parent SQLite changed")
    wal = source / "state.sqlite3-wal"
    require(not wal.exists() or wal.stat().st_size == 0, "Parent has uncheckpointed WAL")
    return {"directory": str(source), "manifest_sha256": sha(source / "manifest.json"),
            "state_sqlite_sha256": manifest["state_sqlite_sha256"], "protocol_sha256": manifest["protocol_sha256"],
            "graph_news": manifest["files"]["news_nodes"]["rows"],
            "candidate_ready_pairs": manifest["decision_counts"]["candidate_ready"],
            "bounded_parent": manifest.get("bounded_pilot", False)}


def endpoint_policy(path):
    if path is None:
        return {"path": None, "sha256": None, "article_companies": []}
    path = Path(path).resolve()
    value = read(path)
    require(isinstance(value, dict) and set(value) <= {"schema", "tickers", "article_companies"}, "Unknown endpoint policy fields")
    require(value.get("tickers", []) == [], "Whole-company endpoint denial is unsupported")
    items, seen = value.get("article_companies", []), set()
    require(isinstance(items, list), "article_companies must be a list")
    for item in items:
        require(isinstance(item, dict) and set(item) == {"article_id", "ticker", "reason"}, "Invalid article endpoint policy")
        require(all(isinstance(v, str) and v.strip() for v in item.values()), "Invalid endpoint identifiers")
        key = (item["article_id"], item["ticker"])
        require(key not in seen, "Duplicate endpoint policy")
        seen.add(key)
    return {"path": str(path), "sha256": sha(path), "article_companies": items}


def queue_contract(source, limit=None):
    # Group the small ready subset once. Do not rescan its join for each batch.
    rows = source.execute("SELECT a.rowid,a.article_id,q.n FROM articles a JOIN "
                          "(SELECT article_id,COUNT(*) n FROM pairs WHERE decision='candidate_ready' "
                          "GROUP BY article_id HAVING COUNT(*)>=2) q USING(article_id) ORDER BY a.rowid").fetchall()
    total = source.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    graph_news, ready_pairs = source.execute("SELECT COUNT(DISTINCT article_id),COUNT(*) FROM pairs WHERE decision='candidate_ready'").fetchone()
    selected = rows if limit is None else rows[:limit]
    queue = [(i + 1, *row) for i, row in enumerate(selected)]
    return queue, {"parent_articles": total, "parent_graph_news": graph_news,
                   "parent_candidate_ready_pairs": ready_pairs, "multi_endpoint_articles": len(rows),
                   "articles_without_multiple_ready_endpoints": total - len(rows),
                   "queued_articles": len(queue), "multi_endpoint_articles_outside_pilot": len(rows) - len(queue),
                   "queue_sha256": digest(queue)}


def schema(db, queue):
    db.executescript("""BEGIN IMMEDIATE;
      CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE work_queue(seq INTEGER PRIMARY KEY,source_rowid INTEGER UNIQUE,article_id TEXT UNIQUE,ready_count INTEGER NOT NULL);
      CREATE TABLE relations(edge_id TEXT PRIMARY KEY,article_id TEXT,src_ticker TEXT,dst_ticker TEXT,rel_type TEXT,directed INTEGER,payload TEXT NOT NULL);
      CREATE INDEX relations_article ON relations(article_id,edge_id);
      CREATE TABLE relation_reviews(article_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE article_receipts(seq INTEGER PRIMARY KEY,article_id TEXT UNIQUE,payload_sha256 TEXT NOT NULL);
    """)
    with db:
        db.executemany("INSERT INTO work_queue VALUES (?,?,?,?)", queue)
        put(db, "cursor", 0)
        put(db, "stage", "relations")
        put(db, "relation_counts", {})


def article_digest(article_id, edges, reviews):
    return digest({"article_id": article_id, "edges": sorted(edges, key=lambda e: e["edge_id"]), "reviews": reviews})


def validate_checkpoint(db, protocol):
    queue = db.execute("SELECT seq,source_rowid,article_id,ready_count FROM work_queue ORDER BY seq").fetchall()
    require(digest(queue) == protocol["coverage"]["queue_sha256"], "Work queue changed")
    cursor = meta(db, "cursor")
    require(type(cursor) is int and 0 <= cursor <= len(queue), "Invalid cursor")
    receipts = db.execute("SELECT seq,article_id,payload_sha256 FROM article_receipts ORDER BY seq").fetchall()
    require(len(receipts) == cursor, "Committed receipt count differs from cursor")
    edges = defaultdict(list)
    counts = Counter()
    for edge_id, article_id, src, dst, kind, directed, payload in db.execute("SELECT * FROM relations"):
        edge = json.loads(payload)
        require((edge["edge_id"], edge["article_id"], edge["src_ticker"], edge["dst_ticker"], edge["rel_type"], int(edge["directed"]))
                == (edge_id, article_id, src, dst, kind, directed) and kind in TYPES, "Relation columns/payload differ")
        require(all(edge.get(k) == v for k, v in FLAGS.items()), "Relation qualification flags differ")
        edges[article_id].append(edge)
        counts[kind] += 1
    reviews = {article: json.loads(payload) for article, payload in db.execute("SELECT * FROM relation_reviews")}
    for i, (seq, article, expected) in enumerate(receipts):
        require((seq, article) == (i + 1, queue[i][2]), "Receipt is not a contiguous queue prefix")
        require(article_digest(article, edges.pop(article, []), reviews.pop(article, [])) == expected,
                "Committed article outputs changed")
    require(not edges and not reviews, "Outputs exist beyond committed cursor")
    require(dict(counts) == meta(db, "relation_counts"), "Relation counts differ")
    require(meta(db, "stage") in {"relations", "export", "complete"}, "Unknown checkpoint stage")
    if meta(db, "stage") != "relations":
        require(cursor == len(queue), "Export requested before queue completion")


def _span(text, start, end, quote, description):
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(text)
            and isinstance(quote, str) and text[start:end] == quote, description + " differs from source")


def process_article(source, article_id, policy, extractor):
    stored = source.execute("SELECT source_sha,cleaned_sha,payload FROM articles WHERE article_id=?", (article_id,)).fetchone()
    require(stored is not None, "Source article missing")
    article = json.loads(stored[2])
    original = {key: article[key] for key in ("article_id", "title", "body", "published_at", "source_url")}
    require(article["article_id"] == article_id and digest(original) == stored[0], "Source article identity/hash differs")
    require(digest({"title": article["cleaned_title"], "body": article["cleaned_body"]}) == stored[1], "Cleaned article hash differs")
    pairs = []
    for ticker, payload in source.execute("SELECT ticker,payload FROM pairs WHERE article_id=? AND decision='candidate_ready' ORDER BY ticker", (article_id,)):
        pair = json.loads(payload)
        require(pair["article_id"] == article_id and pair["ticker"] == ticker
                and pair["materialization_decision"] == "candidate_ready", "Source ready endpoint differs")
        input_row = {**original, "ticker": ticker, "company_name": pair["company_name"]}
        require(common.preview.canonical_input_sha256(input_row) == pair["source_input_sha256"], "Endpoint input hash differs")
        pairs.append(pair)
    allowed = {pair["ticker"] for pair in pairs}
    denials = {r["ticker"]: r["reason"] for r in policy["article_companies"] if r["article_id"] == article_id}
    reviews = [{"decision": "review", "reason": "endpoint_identity_audit_pending", "ticker": ticker,
                "audit_note": reason, "is_asserted_relationship": False,
                "base_news_company_decision_unchanged": True} for ticker, reason in sorted(denials.items()) if ticker in allowed]
    entities = [{"ticker": pair["ticker"], "company_name": pair["company_name"],
                 "aliases": pair["target_context"]["matched_names"]} for pair in pairs if pair["ticker"] not in denials]
    allowed -= denials.keys()
    names_by_ticker = {entity["ticker"]: {name.casefold() for name in [entity["company_name"], *entity["aliases"]]} for entity in entities}
    result = extractor(article["title"], article["body"], entities) if len(entities) >= 2 else {"relationships": [], "review_candidates": []}
    raw = article["title"] + "\n" + article["body"]
    cleaned = common.whitespace(article["cleaned_title"] + "\n" + article["cleaned_body"])
    for review in result["review_candidates"]:
        if "sentence" in review:
            _span(raw, review["sentence_start"], review["sentence_end"], review["sentence"], "Review sentence")
        require(review.get("is_asserted_relationship") is False, "Review cannot assert a relationship")
        reviews.append(review)
    unique = {}
    for edge in result["relationships"]:
        require(edge["rel_type"] in TYPES, "Unsupported relation type")
        require(edge["src_ticker"] in allowed and edge["dst_ticker"] in allowed and edge["src_ticker"] != edge["dst_ticker"],
                "Relation has an ungrounded or deferred endpoint")
        require(type(edge["directed"]) is bool and edge["directed"] == (edge["rel_type"] in {"SUPPLY", "INVEST"}), "Invalid direction flag")
        if not edge["directed"]:
            require(edge["src_ticker"] < edge["dst_ticker"], "Symmetric endpoints must use canonical ticker order")
        _span(raw, edge["sentence_start"], edge["sentence_end"], edge["sentence"], "Relation sentence")
        for endpoint in ("source", "target"):
            _span(raw, edge[endpoint + "_start"], edge[endpoint + "_end"], edge[endpoint + "_quote"], "Relation endpoint")
            require(edge["sentence_start"] <= edge[endpoint + "_start"] < edge[endpoint + "_end"] <= edge["sentence_end"],
                    "Endpoint lies outside its evidence sentence")
            ticker = edge["src_ticker"] if endpoint == "source" else edge["dst_ticker"]
            require(edge[endpoint + "_quote"].casefold() in names_by_ticker[ticker], "Endpoint quote is not a supplied identity alias")
        if common.whitespace(edge["sentence"]) not in cleaned:
            reviews.append({**edge, "decision": "review", "reason": "relation_sentence_not_retained_in_cleaned_article", "is_asserted_relationship": False})
            continue
        identity = [article_id, edge["src_ticker"], edge["dst_ticker"], edge["rel_type"], common.whitespace(edge["sentence"])]
        key = "relationship:" + digest(identity)
        if key in unique:
            unique[key]["duplicate_evidence_spans"].append({k: edge[k] for k in (
                "sentence_start", "sentence_end", "source_start", "source_end", "source_quote", "target_start", "target_end", "target_quote")})
            continue
        unique[key] = {**edge, "edge_id": key, "article_id": article_id, "news_node": "news:" + article_id,
                       "source_node": "company:" + edge["src_ticker"], "target_node": "company:" + edge["dst_ticker"],
                       "source_url": article["source_url"], "published_at": article["published_at"],
                       "event_time": None, "event_time_status": "not_extracted", "duplicate_evidence_spans": [],
                       "offset_basis": "original_title_newline_body", **FLAGS}
    return sorted(unique.values(), key=lambda e: e["edge_id"]), reviews, len(pairs)


def write_status(out, db, state, **extra):
    value = {"schema": SCHEMA, "state": state, "stage": meta(db, "stage"), "processed_articles": meta(db, "cursor"),
             "relation_counts": meta(db, "relation_counts"), "updated_at": common.now(), **FLAGS, **extra}
    atomic(out / "status.json", value)
    print(canonical(value), flush=True)
    return value


def process_batches(source, db, out, protocol, extractor):
    while True:
        if (out / "STOP").exists():
            return write_status(out, db, "paused_stop_file")
        batch = db.execute("SELECT seq,article_id,ready_count FROM work_queue WHERE seq>? ORDER BY seq LIMIT ?",
                           (meta(db, "cursor"), protocol["settings"]["batch_size"])).fetchall()
        if not batch:
            with db:
                put(db, "stage", "export")
            return None
        counts = Counter(meta(db, "relation_counts"))
        with db:
            for seq, article_id, expected in batch:
                edges, reviews, pair_count = process_article(source, article_id, protocol["endpoint_policy"], extractor)
                require(pair_count == expected, "Queue endpoint denominator differs")
                for edge in edges:
                    db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?)", (edge["edge_id"], article_id, edge["src_ticker"],
                               edge["dst_ticker"], edge["rel_type"], int(edge["directed"]), canonical(edge)))
                    counts[edge["rel_type"]] += 1
                if reviews:
                    db.execute("INSERT INTO relation_reviews VALUES (?,?)", (article_id, canonical(reviews)))
                db.execute("INSERT INTO article_receipts VALUES (?,?,?)", (seq, article_id, article_digest(article_id, edges, reviews)))
            put(db, "cursor", batch[-1][0])
            put(db, "relation_counts", dict(counts))
        write_status(out, db, "running")


def verify_manifest(out, protocol, manifest):
    require(manifest.get("schema") == SCHEMA and manifest.get("artifact_complete") is True, "Incomplete relation manifest")
    require(manifest.get("parent_kg") == protocol["parent_kg"] and manifest.get("coverage") == protocol["coverage"], "Manifest source/coverage differs")
    require(all(manifest.get(k) == v for k, v in FLAGS.items()), "Manifest qualification flags differ")
    require(manifest.get("relation_types") == sorted(TYPES)
            and manifest.get("base_news_and_sentiments_unchanged") is True
            and manifest.get("bounded_pilot") == (protocol["settings"]["limit_articles"] is not None), "Manifest contract differs")
    require(manifest["protocol_sha256"] == sha(out / "protocol.json") and manifest["state_sqlite_sha256"] == sha(out / "state.sqlite3"), "Completed protocol/database changed")
    require(set(manifest["files"]) == {"relationship_evidence", "company_relationships", "relation_review"}, "Unexpected export set")
    for item in manifest["files"].values():
        path = (out / item["path"]).resolve()
        require(path.is_relative_to(out.resolve()) and path.is_file(), "Invalid export path")
        require(path.stat().st_size == item["bytes"] and sha(path) == item["sha256"], "Export changed")
        with path.open("rb") as stream:
            require(sum(1 for _ in stream) == item["rows"], "Export denominator differs")


def checkpoint(db):
    result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    require(result is not None and result[0] == 0, "Output WAL checkpoint is busy or failed")


def export(db, out, protocol):
    validate_checkpoint(db, protocol)
    directory = out / "exports" / ("attempt_" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    files = {}
    def write(name, values):
        path, count = directory / (name + ".jsonl"), 0
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for value in values:
                stream.write(canonical(value) + "\n")
                count += 1
        files[name] = {"path": path.relative_to(out).as_posix(), "rows": count, "bytes": path.stat().st_size, "sha256": sha(path)}
    write("relationship_evidence", (json.loads(r[0]) for r in db.execute("SELECT payload FROM relations ORDER BY edge_id")))
    write("company_relationships", ({"src_ticker": src, "dst_ticker": dst, "source_node": "company:" + src,
          "target_node": "company:" + dst, "rel_type": kind, "directed": bool(directed), "evidence_count": count,
          "news_count": news, "evidence_count_unit": "unique_normalized_sentences_not_independent_events", **FLAGS}
          for src, dst, kind, directed, count, news in db.execute("SELECT src_ticker,dst_ticker,rel_type,directed,COUNT(*),COUNT(DISTINCT article_id) FROM relations GROUP BY src_ticker,dst_ticker,rel_type,directed ORDER BY src_ticker,dst_ticker,rel_type,directed")))
    write("relation_review", ({"article_id": article, "candidates": json.loads(payload), **FLAGS}
          for article, payload in db.execute("SELECT article_id,payload FROM relation_reviews ORDER BY article_id")))
    require(code_assets() == protocol["code_and_assets"], "Code changed during run")
    require(source_contract(protocol["parent_kg"]["directory"]) == protocol["parent_kg"], "Parent changed during run")
    require(endpoint_policy(protocol["endpoint_policy"]["path"]) == protocol["endpoint_policy"], "Endpoint policy changed")
    with db:
        put(db, "stage", "complete")
    checkpoint(db)
    require(db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "Output SQLite quick_check failed")
    manifest = {"schema": SCHEMA, "artifact_complete": True, "parent_kg": protocol["parent_kg"],
                "protocol_sha256": sha(out / "protocol.json"), "state_sqlite_sha256": sha(out / "state.sqlite3"),
                "coverage": protocol["coverage"], "bounded_pilot": protocol["settings"]["limit_articles"] is not None,
                "relation_counts": meta(db, "relation_counts"), "files": files, "finished_at": common.now(),
                "relation_types": sorted(TYPES), "full_population_complete": False,
                "relation_semantics": "explicit_source_assertions_with_subtypes_not_financial_impact",
                "base_news_and_sentiments_unchanged": True, **FLAGS}
    atomic(out / "manifest.json", manifest)
    write_status(out, db, "complete", artifact_complete=True)
    return manifest


def run(args, *, extractor=None):
    source_dir, out = args.source_dir.resolve(), args.out.resolve()
    require(not out.is_relative_to(source_dir) and not source_dir.is_relative_to(out), "Output must be separate from parent KG")
    require(type(args.batch_size) is int and args.batch_size > 0, "Invalid batch size")
    require(args.limit_articles is None or type(args.limit_articles) is int and args.limit_articles > 0, "Invalid article limit")
    parent = source_contract(source_dir)
    policy, assets = endpoint_policy(args.endpoint_deny), code_assets()
    with closing(sqlite3.connect((source_dir / "state.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        source.execute("PRAGMA query_only=ON")
        queue, coverage = queue_contract(source, args.limit_articles)
        require(coverage["parent_graph_news"] == parent["graph_news"] and coverage["parent_candidate_ready_pairs"] == parent["candidate_ready_pairs"], "Parent row counts differ from manifest")
        protocol = {"schema": SCHEMA, "parent_kg": parent, "coverage": coverage, "endpoint_policy": policy,
                    "code_and_assets": assets, "settings": {"batch_size": args.batch_size, "limit_articles": args.limit_articles}, **FLAGS}
        out.mkdir(parents=True, exist_ok=True)
        with common.run_lock(out):
            if (out / "protocol.json").exists():
                require(args.resume and read(out / "protocol.json") == protocol, "Resume source/code/settings/policy differ")
            else:
                require(not args.resume and all(p.name == "run.lock" for p in out.iterdir()), "Output is not an empty new run")
                atomic(out / "protocol.json", protocol)
            db_path = out / "state.sqlite3"
            new_database = not db_path.exists()
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                if new_database or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                    schema(db, queue)
                validate_checkpoint(db, protocol)
                try:
                    if meta(db, "stage") == "complete" and (out / "manifest.json").exists():
                        checkpoint(db)
                        manifest = read(out / "manifest.json")
                        verify_manifest(out, protocol, manifest)
                        require(manifest["relation_counts"] == meta(db, "relation_counts"), "Manifest relation counts differ")
                        return manifest
                    if meta(db, "stage") == "relations":
                        if extractor is None:
                            from kg_grounded_relations_v2 import extract_grounded_relations
                            extractor = extract_grounded_relations
                        paused = process_batches(source, db, out, protocol, extractor)
                        if paused:
                            return paused
                    return export(db, out, protocol)
                except BaseException as error:
                    db.rollback()
                    write_status(out, db, "failed", error={"type": type(error).__name__, "message": str(error)})
                    raise


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, default=HERE / "artifacts/kg_grounded_relations_20260919_v2")
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--endpoint-deny", type=Path)
    p.add_argument("--limit-articles", type=int)
    p.add_argument("--resume", action="store_true")
    return p


if __name__ == "__main__":
    run(parser().parse_args())
