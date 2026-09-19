"""Paired comparison against independent AI references, never human accuracy.

All input pairs remain in the denominator, including length/format abstentions.
Reference choices are not supplied to the local model. This compares two frozen
two-stage policies with equal generation settings and exact original inputs.
The reference is a fallible AI review; disagreement counts are descriptive.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import local_news_relevance as base
import run_relevance_two_stage as baseline
import run_relevance_policy_v2 as revised

SETTINGS = ("model_repo", "model_revision", "receipt_sha256", "prompt_sha256",
            "input_sha256", "rows", "attn_implementation", "quantization", "do_sample",
            "temperature", "top_p", "top_k", "seed_policy", "verification_seed_policy",
            "stage2_receives_stage1_output", "retry_policy", "truncation",
            "max_input_tokens", "max_new_tokens", "versions", "gpu")


def index_unique(rows):
    result = {base.pair_key(row): row for row in rows}
    if len(result) != len(rows) or not rows:
        raise ValueError("Missing or duplicate rows")
    return result


def load_arm(folder, inputs, input_path, runner, schema):
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf8"))
    if protocol["schema"] != schema or protocol["code_hashes"] != runner.code_hashes():
        raise ValueError("Runner/policy protocol mismatch")
    if (protocol.get("policy") != "two_stage" or protocol.get("stage2_receives_stage1_output") is not False
            or protocol.get("retry_policy") != "none" or protocol.get("truncation") is not False):
        raise ValueError("Independent stages/no-retry/full-text protocol differs")
    if protocol["input_sha256"] != base.sha(input_path) or protocol["rows"] != len(inputs):
        raise ValueError("Input protocol mismatch")
    records = index_unique(base.read_rows(folder / "results.jsonl"))
    if set(records) != set(inputs):
        raise ValueError("Paired comparison requires every input, including abstentions")
    for key, record in records.items():
        runner.validate_result(record, inputs[key], protocol)
    return records, protocol


def validate_references(records, inputs, input_path=None):
    references = index_unique(records)
    if set(references) != set(inputs):
        raise ValueError("Reference coverage mismatch")
    for key, ref in references.items():
        row = inputs[key]
        if ref.get("human_gold") is not False or ref.get("reference_type") != "independent_ai_review":
            raise ValueError("References must be explicitly independent AI reviews")
        if ref.get("model_outputs_seen") is not False or ref.get("full_article_text_read") is not True:
            raise ValueError("Reference must attest full original-text review without model outputs")
        if ref.get("input_sha256") != hashlib.sha256(base.canonical(row).encode()).hexdigest():
            raise ValueError("Reference input hash mismatch")
        if ref.get("expected_selection") not in ("채택", "제외", "보류"):
            raise ValueError("Invalid reference choice")
        label = ref.get("expected_label", ref.get("label"))
        time_status = ref.get("expected_time_status", ref.get("time_status"))
        if label not in ("직접관련", "간접관련", "단순언급", "판단보류"):
            raise ValueError("Missing or incompatible relevance label")
        if time_status not in ("신규", "과거", "불명"):
            raise ValueError("Missing or incompatible reference time status")
        if ("label" in ref and ref["label"] != label) or ("time_status" in ref and ref["time_status"] != time_status):
            raise ValueError("Conflicting reference label/time fields")
        if input_path is not None and "input_file_sha256" in ref and ref["input_file_sha256"] != base.sha(input_path):
            raise ValueError("Reference input file hash differs")
        for field in ("title", "body"):
            if field + "_sha256" in ref and ref[field + "_sha256"] != hashlib.sha256(row[field].encode()).hexdigest():
                raise ValueError("Reference reviewed text hash differs")
            if "reviewed_" + field + "_chars" in ref and ref["reviewed_" + field + "_chars"] != len(row[field]):
                raise ValueError("Reference reviewed text length differs")
        quotes = ref.get("evidence_sentences")
        if not isinstance(quotes, list) or any(not isinstance(q, str) or not q.strip()
                or not any(q in row[field] for field in ("title", "body")) for q in quotes):
            raise ValueError("Reference quotation is not literal source text")
        if ref["expected_selection"] != "보류" and not quotes:
            raise ValueError("Reference decision requires evidence")
    return references


def measure(records, references):
    confusion = Counter((references[k]["expected_selection"], r["effective_selection"])
                        for k, r in records.items())
    return {"pairs": len(records), "selections": dict(Counter(r["effective_selection"] for r in records.values())),
            "reference_by_model": {a: {b: confusion[a, b] for b in ("채택", "제외", "보류")}
                                   for a in ("채택", "제외", "보류")},
            "adopted_reference_adopt": confusion["채택", "채택"],
            "adopted_reference_exclude": confusion["제외", "채택"],
            "adopted_reference_hold": confusion["보류", "채택"],
            "missed_reference_adopt": confusion["채택", "제외"] + confusion["채택", "보류"],
            "choice_agreement_count": sum(confusion[x, x] for x in ("채택", "제외", "보류")),
            "invalid_pairs": sum(bool(r["validation_errors"]) for r in records.values()),
            "errors": dict(Counter(e for r in records.values() for e in r["validation_errors"])),
            "inference_seconds": round(sum(r["elapsed_seconds"] for r in records.values()), 4)}


def compare(args):
    if args.out.exists():
        raise FileExistsError("Use a new comparison directory; previous results are immutable")
    paths = [args.inputs, *args.references, args.baseline / "protocol.json", args.baseline / "results.jsonl",
             args.revised / "protocol.json", args.revised / "results.jsonl", Path(__file__)]
    source_hashes = {str(path.resolve()): base.sha(path) for path in paths}
    inputs = index_unique(base.read_rows(args.inputs))
    for row in inputs.values():
        base.validate_input(row)
    before, p1 = load_arm(args.baseline, inputs, args.inputs, baseline, "relevance-two-stage-1")
    after, p2 = load_arm(args.revised, inputs, args.inputs, revised, "relevance-two-stage-policy-v2-1")
    if any(p1[k] != p2[k] for k in SETTINGS):
        raise ValueError("Unequal generation/model/input settings")
    refs = validate_references([r for path in args.references for r in base.read_rows(path)], inputs, args.inputs)
    summary = {"pairs": len(inputs), "reference_counts": dict(Counter(r["expected_selection"] for r in refs.values())),
               "baseline": measure(before, refs), "revised": measure(after, refs),
               "human_gold": False, "reference_type": "independent_ai_review",
               "all_hold_agreement_count": sum(r["expected_selection"] == "보류" for r in refs.values()),
               "all_adopt_reference_adopt_count": sum(r["expected_selection"] == "채택" for r in refs.values()),
               "gnn_training_performed": False, "population_accuracy_claim": False,
               "input_sha256": base.sha(args.inputs),
               "protocol_sha256": {"baseline": base.sha(args.baseline / "protocol.json"),
                                   "revised": base.sha(args.revised / "protocol.json")},
               "reference_sha256": {str(p): base.sha(p) for p in args.references}, "source_sha256": source_hashes,
               "code_hashes": {"baseline": p1["code_hashes"], "revised": p2["code_hashes"]}}
    changes = []
    for key, row in inputs.items():
        if before[key]["effective_selection"] != after[key]["effective_selection"]:
            changes.append({"article_id": key[0], "ticker": key[1], "title": row["title"],
                            "baseline": before[key]["effective_selection"], "revised": after[key]["effective_selection"],
                            "reference": refs[key]["expected_selection"], "reference_reason": refs[key].get("reason")})
    summary["changed_choices"] = len(changes)
    if any(base.sha(path) != digest for path, digest in source_hashes.items()):
        raise ValueError("Source/result/reference changed while comparing")
    if baseline.code_hashes() != p1["code_hashes"] or revised.code_hashes() != p2["code_hashes"]:
        raise ValueError("Runtime dependency changed while comparing")
    args.out.mkdir(parents=True, exist_ok=False)
    base.write_json(args.out / "summary.json", summary)
    (args.out / "changed_choices.jsonl").write_text("".join(base.canonical(r) + "\n" for r in changes), encoding="utf8")
    print(base.canonical(summary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "baseline", "revised", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--references", type=Path, nargs="+", required=True)
    compare(parser.parse_args())
