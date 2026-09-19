"""Score a fixed, openly developed diagnostic set, never population accuracy.

The references are synthetic/AI reviews rather than independent human labels.
Passing only permits the separately frozen learning pilot. Source IDs, full row
coverage, execution failures and actual Qwen outputs are retained. No reference
is substituted for a model answer, and no failed criterion is relaxed here.
"""
from __future__ import annotations

import argparse
from collections import Counter
import importlib
from pathlib import Path

import knowledge_graph_news_preview as preview
import transfer_kg_reselection as transfer
import kg_target_aliases as aliases


def evaluate(root, run, teacher_module):
    if teacher_module not in ("kg_reasoned_teacher", "kg_reasoned_teacher_v2", "kg_reasoned_teacher_v3"):
        raise ValueError("Unrecognized development teacher")
    teacher = importlib.import_module(teacher_module)
    root, run = Path(root).resolve(), Path(run).resolve()
    read = lambda path: __import__("json").loads(path.read_text(encoding="utf8"))
    plan, reference = read(root / "acceptance_plan.json"), read(root / "reference.json")
    inputs = preview.read_jsonl(root / "inputs.jsonl")
    results = preview.read_jsonl(run / "results.jsonl")
    protocol, completion = read(run / "protocol.json"), read(run / "completion_receipt.json")
    if (plan["reference_sha256"] != transfer.sha(root / "reference.json")
            or plan["inputs_sha256"] != transfer.sha(root / "inputs.jsonl")
            or protocol["input_sha256"] != plan["inputs_sha256"]
            or not completion["complete"] or completion["pairs"] != len(inputs)
            or completion["results_sha256"] != transfer.sha(run / "results.jsonl")
            or completion["protocol_sha256"] != teacher.digest(protocol)):
        raise ValueError("Development source/result receipt mismatch")
    teacher.validate_labels(inputs, results, protocol)
    expected = {preview.pair_key(row): row for row in reference["entries"]}
    actual = {preview.pair_key(row): row for row in results}
    if not (len(expected) == len(reference["entries"]) == len(actual) == len(inputs) == plan["total_pairs"]
            and set(expected) == set(actual) == {preview.pair_key(row) for row in inputs}):
        raise ValueError("Development pair coverage differs")
    counts, failures, entries = Counter(), Counter(), []
    for row in inputs:
        ref, result = expected[preview.pair_key(row)], actual[preview.pair_key(row)]
        if ref["input_sha256"] != preview.canonical_input_sha256(row) or ref["human_gold"] is not False:
            raise ValueError("Reference does not bind the original source as non-gold")
        synthetic = ref["origin"] == "synthetic_contrast_case"
        matched = result["decision"] in ref["acceptable_labels"]
        unsafe_include = result["decision"] == "include" and "include" not in ref["acceptable_labels"]
        counts["matching_references"] += matched
        counts["synthetic_pairs"] += synthetic
        counts["synthetic_matching_references"] += synthetic and matched
        counts["unusable"] += result["decision"] is None
        counts["unexpected_include"] += unsafe_include
        failures.update(result["errors"])
        entries.append({"article_id": row["article_id"], "ticker": row["ticker"],
            "origin": ref["origin"], "expected": ref["acceptable_labels"], "decision": result["decision"],
            "matches_reference": matched, "errors": result["errors"],
            "economic_path": (result["prediction"] or {}).get("economic_path"), "human_gold": False})
    gates = {"enough_diagnostic_agreement": counts["matching_references"] >= plan["minimum_total_acceptable_matches"],
        "all_synthetic_contrasts_match": counts["synthetic_matching_references"] == counts["synthetic_pairs"],
        "execution_failures_within_limit": counts["unusable"] <= plan["maximum_unusable_pairs"],
        "no_unexpected_includes": counts["unexpected_include"] <= plan["maximum_include_when_not_in_acceptable_labels"]}
    report = {"schema": "reasoned_teacher_development_report_1", "pairs": len(inputs), "counts": dict(counts),
        "gates": gates, "accepted": all(gates.values()), "execution_errors": dict(failures),
        "entries": entries, "teacher_module": teacher_module, "human_gold": False,
        "factual_accuracy_established": False, "population_accuracy_claim": False,
        "reference_kind": plan["reference_kind"], "inference_seconds": completion["inference_seconds"]}
    report_path = run / "development_report.json"
    transfer.atomic_json(report_path, report)
    files = [root / name for name in ("inputs.jsonl", "reference.json", "acceptance_plan.json", "receipt.json")]
    files += [run / name for name in ("protocol.json", "results.jsonl", "completion_receipt.json", "development_report.json")]
    files += [Path(__file__).resolve()]
    receipt = {"accepted": report["accepted"], "teacher_version": teacher.VERSION,
        "teacher_code_hashes": teacher.code_hashes(), "teacher_prompt_sha256": teacher.digest(teacher.CRITERIA),
        "alias_provenance": aliases.provenance(), "human_gold": False, "factual_accuracy_established": False,
        "reference_kind": plan["reference_kind"], "gates": gates,
        "bound_files": {str(path): transfer.sha(path) for path in files}}
    transfer.atomic_json(run / "challenge_receipt.json", receipt)
    return {key: report[key] for key in ("pairs", "counts", "gates", "accepted", "execution_errors", "inference_seconds")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--teacher-module", default="kg_reasoned_teacher_v2")
    args = parser.parse_args()
    print(preview.canonical(evaluate(args.root, args.run, args.teacher_module)))
