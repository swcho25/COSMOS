"""Compare tentative graph selections with two fallible AI reference reviews.

All input pairs remain in the denominator. Agreement is not accuracy; disputed
references are never silently discarded or adjudicated using model predictions.
This evaluator checks raw-run reproducibility and literal evidence, then reports
descriptive outcomes. It never approves production or creates training gold.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import knowledge_graph_news_preview as preview
import local_news_relevance as base
import run_knowledge_graph_relevance as runner


def index(rows, inputs):
    out = {}
    for value in rows:
        key = preview.pair_key(value)
        if key in out or key not in inputs:
            raise ValueError("Duplicate or unknown selection")
        preview.validate_selection(value, inputs[key])
        out[key] = value
    if set(out) != set(inputs):
        raise ValueError("Incomplete selection denominator")
    return out


def evaluate(inputs_path, run_path, a_path, b_path, out):
    out, run_path = Path(out), Path(run_path)
    if out.exists():
        raise ValueError("Refusing to overwrite evaluation")
    raw_inputs = preview.read_jsonl(inputs_path)
    inputs = {preview.pair_key(row): row for row in raw_inputs}
    if not inputs or len(inputs) != len(raw_inputs):
        raise ValueError("Empty/duplicate inputs")
    protocol = json.loads((run_path / "protocol.json").read_text(encoding="utf8"))
    if protocol["input_sha256"] != base.sha(inputs_path) or protocol["code_hashes"] != runner.code_hashes():
        raise ValueError("Run source/code mismatch")
    if protocol.get("pairs") != len(inputs) or protocol.get("policy_version") != runner.policy.VERSION:
        raise ValueError("Protocol pair count or policy differs")
    complete = json.loads((run_path / "summary.json").read_text(encoding="utf8"))
    if complete.get("complete") is not True or complete.get("pairs") != len(inputs):
        raise ValueError("Inference incomplete")
    if set(complete.get("files", {})) != {"protocol.json", "results.jsonl", "selections.jsonl"}:
        raise ValueError("Missing or unexpected inference artifact hashes")
    for name, sha in complete["files"].items():
        if base.sha(run_path / name) != sha:
            raise ValueError("Inference artifact changed")
    raw_results = preview.read_jsonl(run_path / "results.jsonl")
    results = {}
    for result in raw_results:
        key = preview.pair_key(result["selection"])
        if key in results or key not in inputs:
            raise ValueError("Invalid result coverage")
        runner.validate_result(result, inputs[key], protocol)
        if result["selection"]["model_provenance"].get("protocol_sha256") != base.sha(run_path / "protocol.json"):
            raise ValueError("Selection protocol hash differs")
        results[key] = result["selection"]
    model = index(preview.read_jsonl(run_path / "selections.jsonl"), inputs)
    if results != model:
        raise ValueError("Published selections differ from raw validated run")
    if (complete.get("counts") != dict(Counter(x["decision"] for x in model.values()))
            or complete.get("stage_count") != sum(s is not None for x in raw_results for s in x["stages"].values())
            or complete.get("invalid_pairs") != sum(any(s["errors"] for s in x["stages"].values() if s) for x in raw_results)):
        raise ValueError("Summary differs from raw results")
    references = [index(preview.read_jsonl(path), inputs) for path in (a_path, b_path)]
    reviewer_ids = []
    for ref in references:
        identities = set()
        for key, selection in ref.items():
            meta = selection["model_provenance"]
            if selection["policy_version"] != runner.policy.VERSION:
                raise ValueError("Reference policy differs")
            if (meta.get("type") != "independent_ai_reference" or meta.get("model_outputs_seen") is not False
                    or meta.get("other_review_seen") is not False or meta.get("full_article_text_read") is not True
                    or meta.get("human_gold") is not False
                    or meta.get("title_chars") != len(inputs[key]["title"])
                    or meta.get("body_chars") != len(inputs[key]["body"])):
                raise ValueError("Reference provenance/coverage missing")
            identities.add(meta.get("reviewer_id"))
        if len(identities) != 1 or not next(iter(identities)):
            raise ValueError("Reference reviewer identity missing")
        reviewer_ids.append(next(iter(identities)))
    if reviewer_ids[0] == reviewer_ids[1]:
        raise ValueError("References need different reviewers")
    paired, joint, accepted, excluded = [], Counter(), Counter(), Counter()
    for key, row in inputs.items():
        first, second = [ref[key] for ref in references]
        a, b, selected = first["decision"], second["decision"], model[key]["decision"]
        consensus = a if a == b else "disputed"
        joint[(a, b, selected)] += 1
        if selected == "include":
            accepted[consensus] += 1
        if selected == "exclude":
            excluded[consensus] += 1
        paired.append({"article_id": key[0], "ticker": key[1], "title": row["title"],
                       "reference_status": consensus, "first": first, "second": second, "model": model[key]})
    summary = {"complete": True, "input_pairs": len(inputs),
        "model_counts": dict(Counter(x["decision"] for x in model.values())),
        "reference_a_counts": dict(Counter(x["decision"] for x in references[0].values())),
        "reference_b_counts": dict(Counter(x["decision"] for x in references[1].values())),
        "reference_consensus_counts": dict(Counter(x["reference_status"] for x in paired)),
        "model_include_reference_breakdown": dict(accepted), "model_exclude_reference_breakdown": dict(excluded),
        "joint_counts": [{"reference_a": a, "reference_b": b, "model": m, "count": n} for (a, b, m), n in sorted(joint.items())],
        "agreement_with_a": sum(x["model"]["decision"] == x["first"]["decision"] for x in paired),
        "agreement_with_b": sum(x["model"]["decision"] == x["second"]["decision"] for x in paired),
        "all_include_baseline_false_links_against_agreed_exclude": sum(x["reference_status"] == "exclude" for x in paired),
        "model_false_links_against_agreed_exclude": accepted["exclude"],
        "model_missed_links_against_agreed_include": sum(x["reference_status"] == "include" and x["model"]["decision"] != "include" for x in paired),
        "format_or_length_failure_pairs": complete["invalid_pairs"],
        "metrics_are": "AI_reference_agreement_not_accuracy", "human_gold": False,
        "production_approved": False, "gnn_training_performed": False,
        "source_sha256": {str(path): base.sha(path) for path in (Path(inputs_path), Path(a_path), Path(b_path), run_path / "summary.json")}}
    out.mkdir(parents=True)
    with (out / "paired.jsonl").open("x", encoding="utf8", newline="\n") as stream:
        for value in paired:
            stream.write(base.canonical(value) + "\n")
    base.write_json(out / "summary.json", summary)
    print(base.canonical(summary))
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--first", type=Path, required=True)
    p.add_argument("--second", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    evaluate(a.inputs, a.run, a.first, a.second, a.out)
