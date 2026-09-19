"""Validate factorized, source-grounded news reviews without inventing gold.

All axes concern one target event. Literal quotes and schema validation cannot
prove semantic entailment. The rubric separates relevance, novelty, economic
specificity, security scope, claim status and materiality. It derives semantic
candidates only: no model/price access and no automatic GNN eligibility.
Two reviewers' complete axis agreement is descriptive reliability, not truth.
"""
from __future__ import annotations

from collections import Counter
import argparse
import hashlib
import json
from pathlib import Path

import local_news_relevance as base

HERE = Path(__file__).resolve().parent
RUBRIC = HERE / "news_selection_rubric_v1.md"
VERSION = "news-selection-rubric-1"
AXES = {
    "relation": ("direct", "indirect", "mention", "unclear"),
    "novelty": ("new_announcement", "new_development", "repeated_or_background", "unclear"),
    "economic": ("specific_change", "generic_or_context", "none", "unclear"),
    "security_scope": ("not_security_specific", "matching", "mismatched", "unknown"),
    "claim_status": ("asserted_fact", "reported_or_projected", "unclear"),
    "materiality": ("described", "not_established"),
}
FIELDS = set(AXES) | {"schema", "article_id", "ticker", "input_sha256", "rubric_sha256", "reviewer_id",
    "review_context", "event_summary", "economic_path", "reason", "human_gold", "full_article_text_read",
    "model_outputs_seen", "other_review_seen", "reviewed_title_chars", "reviewed_body_chars"}
PENDING_CHECKS = ["public_availability", "historical_target_security", "event_deduplication",
                  "forward_label_alignment", "price_data_quality"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def input_sha(row):
    return hashlib.sha256(base.canonical(row).encode("utf8")).hexdigest()


def derive(review):
    v = {axis: review[axis]["value"] for axis in AXES}
    if v["relation"] == "mention" or v["economic"] == "none":
        return "exclude"
    if v["relation"] in ("direct", "indirect") and v["novelty"] == "repeated_or_background":
        return "exclude"
    if (v["relation"] in ("direct", "indirect")
            and v["novelty"] in ("new_announcement", "new_development")
            and v["economic"] == "specific_change"
            and v["security_scope"] in ("not_security_specific", "matching")
            and v["claim_status"] in ("asserted_fact", "reported_or_projected")):
        return "candidate"
    return "review"


def validate(review, row, *, context=None):
    base.validate_input(row)
    require(isinstance(review, dict) and set(review) == FIELDS, "Review field contract differs")
    require(all(isinstance(value[key], str) and value[key].strip()
                for value in (review, row) for key in ("article_id", "ticker")), "IDs must be nonempty strings")
    require(row.get("company_context") is None, "This rubric version uses original article text only")
    require(review["schema"] == VERSION and review["rubric_sha256"] == base.sha(RUBRIC), "Rubric version/hash differs")
    require(base.pair_key(review) == base.pair_key(row) and review["input_sha256"] == input_sha(row), "Input identity/hash differs")
    require(review["review_context"] in ("fresh_independent_ai", "development_relabel"), "Unknown review context")
    require(context is None or review["review_context"] == context, "Review context differs")
    for key in ("reviewer_id", "event_summary", "reason"):
        require(isinstance(review[key], str) and review[key].strip(), "Missing text: " + key)
    require(isinstance(review["economic_path"], str), "Economic path must be text")
    require(review["human_gold"] is False and review["full_article_text_read"] is True,
            "AI review cannot become human gold or use partial text")
    require(type(review["model_outputs_seen"]) is bool and type(review["other_review_seen"]) is bool, "Missing exposure flags")
    if review["review_context"] == "fresh_independent_ai":
        require(not review["model_outputs_seen"] and not review["other_review_seen"], "Fresh reviews must be independent")
    for source in ("title", "body"):
        require(type(review["reviewed_" + source + "_chars"]) is int
                and review["reviewed_" + source + "_chars"] == len(row[source]), "Reviewed length differs")
    for axis, choices in AXES.items():
        item = review[axis]
        require(isinstance(item, dict) and set(item) == {"value", "evidence"}, "Axis schema differs: " + axis)
        require(isinstance(item["value"], str) and item["value"] in choices, "Unknown axis value: " + axis)
        require(isinstance(item["evidence"], list), "Evidence must be a list")
        seen = set()
        for quote in item["evidence"]:
            require(isinstance(quote, dict) and set(quote) == {"source", "quote"}, "Quote schema differs")
            require(quote["source"] in ("title", "body") and isinstance(quote["quote"], str)
                    and quote["quote"].strip() and quote["quote"] in row[quote["source"]], "Quote is not literal source text")
            identity = (quote["source"], quote["quote"])
            require(identity not in seen, "Repeated quote within one axis")
            seen.add(identity)
        evidence_optional = ((axis in ("relation", "novelty", "economic", "claim_status") and item["value"] == "unclear")
            or (axis == "security_scope" and item["value"] in ("unknown", "not_security_specific"))
            or (axis == "materiality" and item["value"] == "not_established"))
        require(bool(item["evidence"]) or evidence_optional, "Asserted axis needs evidence: " + axis)
    values = {axis: review[axis]["value"] for axis in AXES}
    require(not (values["relation"] == "mention" and values["economic"] == "specific_change"),
            "Incidental mention conflicts with a specific target economic event")
    if values["economic"] == "specific_change":
        require(bool(review["economic_path"].strip()), "Specific event needs a source-grounded economic path")
    return {"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": input_sha(row),
            "candidate_status": derive(review), "training_readiness": "pending_checks",
            "ready_for_gnn_training": False, "usable_as_training_gold": False,
            "human_gold": False, "pending_checks": list(PENDING_CHECKS)}


def combine(inputs, first, second):
    """Compare every input, preserving each source review and disagreement."""
    require(inputs and first and second, "Empty comparison")
    def indexed(rows):
        pairs = [base.pair_key(r) for r in rows]
        require(len(pairs) == len(set(pairs)), "Duplicate pairs")
        return dict(zip(pairs, rows))
    source, one, two = indexed(inputs), indexed(first), indexed(second)
    require(set(source) == set(one) == set(two), "Both reviewers must cover every input")
    require(len({r["reviewer_id"] for r in first}) == len({r["reviewer_id"] for r in second}) == 1, "One reviewer per arm required")
    require(first[0]["reviewer_id"] != second[0]["reviewer_id"], "Reviewers must be distinct")
    decisions, axis_agreement = [], Counter()
    for key, row in source.items():
        a = validate(one[key], row, context="fresh_independent_ai")
        b = validate(two[key], row, context="fresh_independent_ai")
        differences = [axis for axis in AXES if one[key][axis]["value"] != two[key][axis]["value"]]
        axis_agreement.update(axis for axis in AXES if axis not in differences)
        agreement = ("disputed" if differences else "agreed_" + a["candidate_status"])
        decisions.append({**a, "candidate_status": a["candidate_status"] if not differences else "review",
            "agreement_status": agreement, "disagreement_axes": differences,
            "reviewer_a_status": a["candidate_status"], "reviewer_b_status": b["candidate_status"],
            "human_review_required": True, "review_a": one[key], "review_b": two[key]})
    summary = {"schema": VERSION, "pairs": len(source), "reference_type": "two_independent_ai_reviews",
        "agreement_scope": "axis_values_only_not_event_or_evidence_entailment",
        "human_gold": False, "accuracy_measured": False, "ready_for_gnn_training_count": 0,
        "agreement_counts": dict(Counter(x["agreement_status"] for x in decisions)),
        "reviewer_a_candidate_counts": dict(Counter(x["reviewer_a_status"] for x in decisions)),
        "reviewer_b_candidate_counts": dict(Counter(x["reviewer_b_status"] for x in decisions)),
        "axis_agreement_counts": dict(axis_agreement),
        "decision_agreement_count": sum(x["reviewer_a_status"] == x["reviewer_b_status"] for x in decisions),
        "all_axes_agreement_count": sum(not x["disagreement_axes"] for x in decisions),
        "limitations": ["AI agreement is not semantic truth or human accuracy.",
            "Same model family can share errors even without seeing each other's decisions.",
            "Technical data/label eligibility is not checked and no row is training gold.",
            "Agreement of value categories does not prove agreement of event interpretation or quotations."]}
    return decisions, summary


def read_rows(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "Duplicate JSON key")
            value[key] = item
        return value
    def nonfinite(value):
        raise ValueError("Nonfinite JSON value: " + value)
    with Path(path).open(encoding="utf8") as stream:
        return [json.loads(line, object_pairs_hook=unique, parse_constant=nonfinite) for line in stream if line.strip()]


def run(inputs, first, second, out):
    paths = [Path(x).resolve() for x in (inputs, first, second)]
    out = Path(out).resolve()
    require(not out.exists(), "Output must be new")
    dependencies = [*paths, RUBRIC, Path(__file__), Path(base.__file__)]
    before = {str(p): base.sha(p) for p in dependencies}
    decisions, summary = combine(*(read_rows(p) for p in paths))
    summary["source_sha256"] = before
    require(all(base.sha(Path(p)) == checksum for p, checksum in before.items()), "Review dependencies changed")
    out.mkdir(parents=True, exist_ok=False)
    for name, rows in (("paired_reviews.jsonl", decisions),
                       ("human_review_queue.jsonl", [r for r in decisions if r["agreement_status"] == "disputed"])):
        with (out / name).open("x", encoding="utf8", newline="\n") as stream:
            stream.write("".join(base.canonical(r) + "\n" for r in rows))
    with (out / "summary.json").open("x", encoding="utf8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("inputs", "first", "second", "out"):
        parser.add_argument("--" + field, type=Path, required=True)
    args = parser.parse_args()
    print(base.canonical(run(args.inputs, args.first, args.second, args.out)))
