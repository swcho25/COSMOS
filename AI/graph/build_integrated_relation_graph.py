"""Build a separate, literal-evidence NEWS/DISCLOSURE graph and window scores.

The input KG and relation revision are immutable. This does not publish to
HDFS, write a service database, or replace a trained model. A completed
manifest is written last. Interrupted attempts can be rerun into a fresh
directory; source artifacts are never edited. Each normalized document and
evidence identity must agree before deduplication.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path
import sqlite3

from integrated_relation_graph import (finalize, init_db, insert_evidence, json_text,
    read_json, read_jsonl, require, sha)


def manifest_file(directory, manifest, key):
    entry = manifest["files"][key]
    path = (directory / entry["path"]).resolve()
    require(path.is_relative_to(directory.resolve()) and path.is_file(), "Invalid manifest file path")
    require(sha(path) == entry["sha256"], "Input file checksum differs: " + key)
    if "bytes" in entry:
        require(path.stat().st_size == entry["bytes"], "Input file length differs")
    return path


def news_inputs(kg_dir, relation_dir, review=None, excluded=None):
    parent, revision = read_json(kg_dir / "manifest.json"), read_json(relation_dir / "manifest.json")
    require(parent.get("artifact_complete") is True and revision.get("artifact_complete") is True, "Incomplete news input")
    parent_sha = sha(kg_dir / "manifest.json")
    require(sha(kg_dir / "protocol.json") == parent["protocol_sha256"], "Parent protocol differs")
    binding = revision["parent_kg"]
    require(binding["manifest_sha256"] == parent_sha and binding["state_sqlite_sha256"] == parent["state_sqlite_sha256"], "Relation input parent differs")
    database = kg_dir / "state.sqlite3"
    require(not Path(str(database) + "-wal").exists() or Path(str(database) + "-wal").stat().st_size == 0, "Active parent database")
    require(sha(database) == parent["state_sqlite_sha256"], "Parent KG database differs")
    path = manifest_file(relation_dir, revision, "relationship_evidence")
    decisions = {}
    if review is not None:
        require(review.get("schema") == "cosmos-news-relation-review-1"
                and review.get("source_manifest_sha256") == sha(relation_dir / "manifest.json"), "Review belongs to different news relations")
        for decision in review["decisions"]:
            require(decision["decision"] in {"keep", "review", "exclude"}
                    and decision["evidence_id"] not in decisions and bool(decision["reason"]), "Invalid relation review")
            decisions[decision["evidence_id"]] = decision
    seen = set()
    con = sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        for edge in read_jsonl(path):
            seen.add(edge["edge_id"])
            article_id = edge["article_id"]
            found = con.execute("SELECT payload FROM articles WHERE article_id=?", (article_id,)).fetchone()
            require(found is not None, "Relation article is absent")
            import json
            article = json.loads(found[0])
            require(article["article_id"] == article_id, "Article identity differs")
            require(article.get("published_at") == edge.get("published_at")
                    and article.get("source_url") == edge.get("source_url"), "News metadata differs")
            text = article["title"] + "\n" + article["body"]
            for endpoint in ("source", "target"):
                begin, end = edge[endpoint + "_start"], edge[endpoint + "_end"]
                require(type(begin) is int and type(end) is int
                        and edge["sentence_start"] <= begin < end <= edge["sentence_end"]
                        and text[begin:end] == edge[endpoint + "_quote"], "News endpoint span differs")
            document = {"document_id": "NEWS:" + article_id, "record_id": article_id,
                        "source_kind": "NEWS", "text": text, "title": article["title"],
                        "published_at": article["published_at"], "source_url": article.get("source_url"),
                        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "offset_basis": "original_title_newline_body"}
            row = {"evidence_id": "NEWS:" + edge["edge_id"], "record_id": article_id,
                   "document_id": document["document_id"], "source_kind": "NEWS", "title": article["title"],
                   "src_ticker": edge["src_ticker"], "dst_ticker": edge["dst_ticker"],
                   "rel_type": edge["rel_type"], "directed": edge["directed"],
                   "relation_subtype": edge.get("relation_subtype"), "published_at": edge["published_at"],
                   "source_url": edge.get("source_url"), "quote": edge["sentence"],
                   "quote_start": edge["sentence_start"], "quote_end": edge["sentence_end"],
                   "source_quote": edge["source_quote"], "target_quote": edge["target_quote"],
                   "offset_basis": "original_title_newline_body", "support_weight": 1.0,
                   "support_weight_basis": "one_explicit_document_assertion_not_calibrated_confidence",
                   "temporal_validity": edge.get("temporal_validity", "not_established"),
                   "endpoint_validation": edge.get("endpoint_validation"),
                   "source_relation_manifest_sha256": sha(relation_dir / "manifest.json")}
            decision = decisions.get(edge["edge_id"])
            if decision and decision["decision"] != "keep":
                if excluded is not None:
                    excluded.append({"edge": edge, "review": decision})
                continue
            yield row, document
        require(not (decisions.keys() - seen), "Review mentions absent evidence")
    finally:
        con.close()


def disclosure_inputs(directory):
    """Normalized literal records produced by prepare_grounded_disclosures.

    Source document payloads preserve original file text, reporting-company
    metadata and byte/text hashes. No missing original is reconstructed.
    """
    manifest = read_json(directory / "manifest.json")
    require(manifest.get("artifact_complete") is True, "Incomplete disclosure input")
    if manifest.get("schema") == "cosmos-grounded-disclosures-1":
        yield from dart_disclosure_inputs(directory, manifest)
        return
    doc_path = manifest_file(directory, manifest, "documents")
    docs = {}
    for row in read_jsonl(doc_path):
        require(row["document_id"] not in docs, "Duplicate disclosure document identity")
        docs[row["document_id"]] = row
    relation_path = manifest_file(directory, manifest, "relationship_evidence")
    for row in read_jsonl(relation_path):
        require(row["source_kind"] == "DISCLOSURE" and row["document_id"] in docs, "Missing original disclosure file")
        yield row, docs[row["document_id"]]


def dart_disclosure_inputs(directory, manifest):
    records = {r["document_id"]: r for r in read_jsonl(manifest_file(directory, manifest, "disclosure_documents"))}
    source_specs = {r["path"]: r for r in manifest["source_documents"]}
    for raw in read_jsonl(manifest_file(directory, manifest, "disclosure_relationships")):
        record = records[raw["record_id"]]
        require(record["ticker"] == raw["filing_ticker"]
                and record["issuer_corp_code"] == raw["filing_corp_code"]
                and record["report_id"] == raw["report_id"], "Filing identity disagrees with metadata")
        require(record["published_at"] == raw["published_at"]
                and record["source_url"] == raw["source_url"], "Filing source metadata disagrees")
        spec = source_specs[raw["source_document_path"]]
        path = (directory / spec["path"]).resolve()
        require(path.is_relative_to(directory.resolve())
                and sha(path) == spec["sha256"] == raw["source_document_sha256"], "Filing original file differs")
        # newline='' preserves CR/LF codepoint offsets exactly if ever present.
        with path.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
        document_id = "DISCLOSURE:" + raw["record_id"] + ":" + str(raw["source_document_file_index"])
        document = {"document_id": document_id, "record_id": raw["record_id"], "source_kind": "DISCLOSURE",
                    "text": text, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "title": raw["title"], "published_at": raw["published_at"], "source_url": raw["source_url"],
                    "filing_ticker": raw["filing_ticker"], "filing_corp_code": raw["filing_corp_code"],
                    "filing_name": raw["filing_name"], "report_id": raw["report_id"],
                    "observed_at": raw["observed_at"], "source_document_sha256": raw["source_document_sha256"],
                    "offset_basis": raw["offset_basis"]}
        row = {**raw, "document_id": document_id, "support_weight": raw["confidence"],
               "support_weight_basis": raw["confidence_meaning"],
               "source_disclosure_manifest_sha256": sha(directory / "manifest.json")}
        yield row, document


def build(args):
    out = args.out.resolve()
    require(not out.exists(), "Use a fresh output directory; completed or interrupted attempts are retained")
    out.mkdir(parents=True)
    parent = read_json(args.kg_dir / "manifest.json")
    relation = read_json(args.relation_dir / "manifest.json")
    disclosure = read_json(args.disclosure_dir / "manifest.json")
    review = read_json(args.news_review) if args.news_review else None
    excluded_news = []
    with args.companies.open(encoding="utf-8-sig", newline="") as stream:
        companies = {r["ticker"]: r["market"] for r in csv.DictReader(stream)}
    connection = init_db(out / "integrated.sqlite3")
    counts = {"NEWS": 0, "DISCLOSURE": 0}
    try:
        for iterator in (news_inputs(args.kg_dir, args.relation_dir, review, excluded_news), disclosure_inputs(args.disclosure_dir)):
            for row, document in iterator:
                insert_evidence(connection, row, document)
                counts[row["source_kind"]] += 1
                if sum(counts.values()) % 128 == 0:
                    connection.commit()
                    print(json_text({"stage": "integrating", "rows": counts}), flush=True)
        excluded_path = out / "news_evidence_deferred.jsonl"
        with excluded_path.open("x", encoding="utf-8", newline="\n") as stream:
            for row in excluded_news:
                stream.write(json_text(row) + "\n")
        result = finalize(connection, out, companies=companies, as_of=args.as_of,
            provenance={"parent_kg_manifest_sha256": sha(args.kg_dir / "manifest.json"),
                        "news_relation_manifest_sha256": sha(args.relation_dir / "manifest.json"),
                        "disclosure_manifest_sha256": sha(args.disclosure_dir / "manifest.json"),
                        "company_dictionary_sha256": sha(args.companies),
                        "news_review_sha256": sha(args.news_review) if args.news_review else None,
                        "deferred_news_evidence": {"rows": len(excluded_news), "path": excluded_path.name, "sha256": sha(excluded_path)},
                        "builder_sha256": sha(Path(__file__))},
            coverage={"news_full_population_complete": parent.get("full_population_complete", False),
                      "news_graph_news": parent["files"]["news_nodes"]["rows"],
                      "relation_coverage": relation.get("coverage", {k: relation.get(k) for k in ("multi_b_include_articles_available", "multi_b_include_articles_processed", "parent_articles_considered_by_company_count", "limitations")}),
                      "disclosure_coverage": disclosure.get("coverage", {"counts": disclosure.get("counts"), "limitations": disclosure.get("limitations")}),
                      "live_updates_included": False,
                      "display_scores_are_provisional_subset": True})
    except BaseException:
        connection.close()
        raise
    print(json_text(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kg-dir", type=Path, required=True)
    parser.add_argument("--relation-dir", type=Path, required=True)
    parser.add_argument("--disclosure-dir", type=Path, required=True)
    parser.add_argument("--news-review", type=Path)
    parser.add_argument("--companies", type=Path, default=Path(__file__).parents[1] / "ner/data/company_industry.csv")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--out", type=Path, required=True)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
