"""Apply an economic-relevance selection to one local lake-analysis result.

This opt-in adapter belongs immediately after lake_news_normalize.analyze_article
and before window aggregation or ID rewriting. Map the cleaned row's record_id
to each input's article_id; do not substitute document_id or an already rewritten
relationship record_id. Inputs contain exactly one article/company pair for each
original mention ticker, and selections must cover every pair exactly once.

Only included mentions may feed company aggregates. An existing relationship
candidate survives only if BOTH endpoints are included. This adapter never
creates relationships and does not establish that the upstream relationship
type/direction is correct: literal relation extraction remains the upstream
analyzer's responsibility. A past but economically relevant article can survive.

Source selections are pinned by the preview module's canonical seven-field
digest. Every original mention span and relationship sentence/endpoint is also
checked against the supplied title + newline + body. analyze_article does not
return a full source-text hash, so those checks cannot independently prove that
its entire original input body was identical. That boundary is explicit in the
returned audit as analysis_source_full_hash_verified=false. The caller must
construct these inputs from the same cleaned row passed to analyze_article.

The function returns a deep copy and performs no file, HDFS, database, model, or
service operations. It is a local integration point, not automatic production
adoption or evidence of classification accuracy. Empty-mention articles may pass
through only with empty inputs, selections, and relationship lists.
"""
from __future__ import annotations

from copy import deepcopy

import knowledge_graph_news_preview as preview


VERSION = "knowledge-graph-lake-gate-1"
OFFSET_BASIS = "cleaned_title_newline_body"


def _span(text: str, start, end, *, label: str) -> str:
    preview.require(type(start) is int and type(end) is int and 0 <= start < end <= len(text),
                    "Invalid " + label + " offsets")
    return text[start:end]


def apply_selection(analysis: dict, inputs: list[dict], selections: list[dict]) -> dict:
    """Filter one in-memory analyzer result; never mutate any supplied object."""
    preview.require(isinstance(analysis, dict), "Analysis must be an object")
    preview.require("knowledge_graph_selection" not in analysis, "Selection gate was already applied")
    for key in ("mentions", "relationships", "analysis_flags"):
        preview.require(isinstance(analysis.get(key), list), "Analysis requires a list: " + key)
    preview.require(all(isinstance(flag, str) for flag in analysis["analysis_flags"]), "Analysis flags must be strings")
    preview.require(isinstance(inputs, list) and isinstance(selections, list), "Inputs and selections must be lists")
    mentions, relationships = analysis["mentions"], analysis["relationships"]
    if not mentions:
        preview.require(not inputs and not selections and not relationships,
                        "An article without mentions requires empty inputs, selections and relationships")
        result = deepcopy(analysis)
        result["analysis_flags"] = sorted(set(result["analysis_flags"] + ["economic_relevance_gate_local_only"]))
        result["knowledge_graph_selection"] = {
            "schema": VERSION, "article_id": None, "status": "no_company_candidates",
            "selection_audit": [], "included_tickers": [],
            "dropped_mentions": 0, "dropped_relationships": 0,
            "human_gold": False, "production_approved": False,
            "analysis_source_full_hash_verified": False,
        }
        return result

    # This checks exact seven-field input hashes, literal selection evidence,
    # duplicate pairs, consistent article content and complete pair coverage.
    payloads, summary = preview.prepare_preview(inputs, selections)
    article_ids = {row["article_id"] for row in inputs}
    preview.require(len(article_ids) == 1, "One gate call must contain exactly one article_id")
    article_id = next(iter(article_ids))
    input_tickers = {row["ticker"] for row in inputs}
    by_ticker = {}
    text = inputs[0]["title"] + "\n" + inputs[0]["body"]
    for mention in mentions:
        preview.require(isinstance(mention, dict), "Mention must be an object")
        preview.require(mention.get("record_id") == article_id, "Mention record_id differs from article_id")
        ticker = mention.get("ticker")
        preview.require(isinstance(ticker, str) and ticker.strip(), "Mention ticker is missing")
        preview.require(ticker not in by_ticker, "Duplicate original mention ticker")
        preview.require(mention.get("offset_basis") == OFFSET_BASIS, "Mention offset basis differs")
        spans = mention.get("spans")
        preview.require(isinstance(spans, list) and bool(spans), "Original mention requires literal spans")
        for span in spans:
            preview.require(isinstance(span, dict), "Mention span must be an object")
            literal = _span(text, span.get("start"), span.get("end"), label="mention")
            preview.require(span.get("quote") == literal, "Original mention quote differs from input text")
        by_ticker[ticker] = mention
    preview.require(set(by_ticker) == input_tickers,
                    "Input tickers must exactly cover every original mention ticker")
    for relation in relationships:
        preview.require(isinstance(relation, dict), "Relationship must be an object")
        preview.require(relation.get("record_id") == article_id, "Relationship record_id differs from article_id")
        source, target = relation.get("src_ticker"), relation.get("dst_ticker")
        preview.require(isinstance(source, str) and isinstance(target, str), "Relationship endpoints must be strings")
        preview.require(source != target and source in by_ticker and target in by_ticker,
                        "Relationship endpoint is not an original distinct mention")
        preview.require(relation.get("offset_basis") == OFFSET_BASIS, "Relationship offset basis differs")
        start, end = relation.get("sentence_start"), relation.get("sentence_end")
        sentence = _span(text, start, end, label="relationship sentence")
        preview.require(relation.get("sentence") == sentence, "Relationship sentence differs from input text")
        for ticker, prefix in ((source, "source"), (target, "target")):
            left, right = relation.get(prefix + "_start"), relation.get(prefix + "_end")
            _span(text, left, right, label="relationship " + prefix)
            preview.require(start <= left < right <= end, "Relationship endpoint lies outside its sentence")
            preview.require(any(span["start"] == left and span["end"] == right
                                for span in by_ticker[ticker]["spans"]),
                            "Relationship endpoint differs from the original mention spans")

    included = {row["ticker"] for row in selections if row["decision"] == "include"}
    result = deepcopy(analysis)
    result["mentions"] = [row for row in result["mentions"] if row["ticker"] in included]
    result["relationships"] = [row for row in result["relationships"]
                               if row["src_ticker"] in included and row["dst_ticker"] in included]
    result["analysis_flags"] = sorted(set(result["analysis_flags"] + [
        "economic_relevance_gate_local_only", "economic_relevance_does_not_validate_relationship_semantics",
    ]))
    dropped_mentions = len(mentions) - len(result["mentions"])
    dropped_relationships = len(relationships) - len(result["relationships"])
    if dropped_mentions or dropped_relationships:
        result["analysis_flags"].append("economic_relevance_excluded_or_review_filtered")
        result["analysis_flags"].sort()
    result["knowledge_graph_selection"] = {
        "schema": VERSION, "article_id": article_id, "status": "applied_locally",
        "selection_audit": deepcopy(payloads["selection_audit.jsonl"]),
        "decision_counts": summary["decision_counts"], "included_tickers": sorted(included),
        "input_sha256_by_ticker": {row["ticker"]: preview.canonical_input_sha256(row) for row in inputs},
        "original_mentions": len(mentions), "retained_mentions": len(result["mentions"]),
        "original_relationships": len(relationships), "retained_relationships": len(result["relationships"]),
        "dropped_mentions": dropped_mentions, "dropped_relationships": dropped_relationships,
        "input_selection_hashes_verified": True, "analysis_literal_offsets_verified": True,
        "analysis_source_full_hash_verified": False,
        "company_relationship_semantics_verified": False,
        "human_gold": False, "ready_for_gnn_training": False, "production_approved": False,
    }
    return result
