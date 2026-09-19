"""Compare two selectors on identical pairs against fallible AI references.

Every abstention/format error remains in the denominator. No return labels are
read, no population/price accuracy is claimed, and candidates remain pending
human review. Development and fresh cohorts must be evaluated in separate runs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import local_news_relevance as base
import run_relevance_followup as followup


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def frozen_path(value):
    """Freeze inventory paths are repository-relative, including Windows paths."""
    path = Path(value.replace("\\", "/"))
    return (path if path.is_absolute() else REPOSITORY_ROOT / path).resolve()


def validate_fresh_freeze(args, inputs, refs):
    if not getattr(args, "freeze", None):
        raise ValueError("Fresh comparison requires --freeze")
    freeze_path = Path(args.freeze).resolve()
    freeze_sha = base.sha(freeze_path)
    freeze = json.loads(freeze_path.read_text(encoding="utf8"))
    if (freeze.get("human_gold") is not False or freeze.get("model_outputs_seen_by_reviewers") is not False
            or freeze.get("new_model_inference_not_started") is not True
            or freeze.get("input_reference_hashes_and_quotes_verified") is not True
            or type(freeze.get("pairs")) is not int or freeze["pairs"] != len(inputs)
            or not inputs or any(row.get("case_kind") != "real" for row in inputs.values())):
        raise ValueError("Fresh reference freeze provenance or cohort differs")
    files = freeze.get("files")
    if not isinstance(files, dict) or any(not isinstance(name, str) for name in files):
        raise ValueError("Fresh freeze requires a file inventory")
    pinned = {frozen_path(name): digest for name, digest in files.items()}
    requested = {Path(path).resolve() for path in [args.inputs, *args.reference]}
    if len(pinned) != len(files) or set(pinned) != requested:
        raise ValueError("Fresh freeze must pin exactly the requested input and reference files")
    for path, expected in pinned.items():
        if not path.is_file() or base.sha(path) != expected:
            raise ValueError("Fresh frozen input/reference file changed")
    policy_path = freeze_path.with_name("experiment_freeze.json")
    if not policy_path.is_file() or base.sha(policy_path) != freeze.get("policy_freeze_sha256"):
        raise ValueError("Fresh policy freeze hash differs")
    policy = json.loads(policy_path.read_text(encoding="utf8"))
    if (policy.get("policy_fixed_before_new_article_content_and_reference_inspection") is not True
            or policy.get("fresh_predictions_inspected") is not False
            or policy.get("fresh_reference_decisions_inspected") is not False
            or policy.get("fresh_article_content_inspected_by_policy_authors") is not False
            or policy.get("code_hashes") != followup.code_hashes("grounded")):
        raise ValueError("Fresh experimental policy is not the precommitted runtime")
    policy_sources = policy.get("sources")
    if not isinstance(policy_sources, dict):
        raise ValueError("Policy freeze is missing its input sources")
    policy_pins = {frozen_path(name): digest for name, digest in policy_sources.items()}
    input_sha = pinned[Path(args.inputs).resolve()]
    if policy_pins.get(Path(args.inputs).resolve()) != input_sha:
        raise ValueError("Fresh input is not pinned by the experimental policy freeze")
    for key, row in inputs.items():
        ref = refs[key]
        expected = hashlib.sha256(base.canonical(row).encode("utf8")).hexdigest()
        if ref.get("input_sha256") != expected or ref.get("input_file_sha256") != input_sha:
            raise ValueError("Fresh reference input row/file hash differs")
        if ref.get("model_outputs_seen") is not False or ref.get("full_article_text_read") is not True:
            raise ValueError("Fresh reference must attest full-text review without model outputs")
        for name, source in (("body_sha256", "body"), ("title_sha256", "title")):
            if name in ref and ref[name] != hashlib.sha256(row[source].encode("utf8")).hexdigest():
                raise ValueError("Fresh reviewed text hash differs")
        for name, source in (("reviewed_body_chars", "body"), ("reviewed_title_chars", "title")):
            if name in ref and ref[name] != len(row[source]):
                raise ValueError("Fresh reviewed text length differs")
    pinned[freeze_path] = freeze_sha
    pinned[policy_path] = freeze["policy_freeze_sha256"]
    return pinned


def indexed(paths):
    rows = {}
    for path in paths:
        for row in base.read_rows(path):
            key = base.pair_key(row)
            if key in rows:
                raise ValueError("Duplicate pair")
            rows[key] = row
    return rows


def load_results(path, inputs, input_path):
    path = Path(path)
    protocol = json.loads(path.with_name("protocol.json").read_text(encoding="utf8"))
    if protocol["input_sha256"] != base.sha(input_path) or protocol["rows"] != len(inputs):
        raise ValueError("Result protocol input mismatch")
    rows = indexed([path])
    if set(rows) != set(inputs):
        raise ValueError("Missing or extra results")
    if protocol.get("schema") == "relevance-followup-1":
        if protocol["code_hashes"] != followup.code_hashes(protocol["policy"]):
            raise ValueError("Follow-up runtime code changed")
        for key, result in rows.items():
            followup.validate_result(result, inputs[key], protocol)
    else:
        import relevance_sentence_evidence
        if (protocol["code_sha256"] != base.sha(base.__file__)
            or protocol["evidence_code_sha256"] != base.sha(relevance_sentence_evidence.__file__)
            or protocol["attention_code_sha256"] != base.sha(Path(base.__file__).with_name("local_relevance_attention.py"))):
            raise ValueError("Original pilot code changed")
        for key, result in rows.items():
            base.validate_resume_result(result, inputs[key], protocol["max_input_tokens"],
                protocol["max_new_tokens"], protocol["evidence_mode"])
    return rows, protocol


def validate_references(inputs, refs):
    if set(refs) != set(inputs):
        raise ValueError("Missing or extra references")
    for key, ref in refs.items():
        if ((inputs[key]["case_kind"] == "real" and ref.get("human_gold") is not False)
            or ref.get("human_gold") is True or ref.get("expected_selection") not in ("채택", "제외", "보류")):
            raise ValueError("Reference must explicitly be a non-gold AI/synthetic decision")
        if inputs[key]["case_kind"] == "real" and ref.get("reference_type") != "independent_ai_review":
            raise ValueError("Only economic-relevance independent AI references are supported")
        if ref.get("expected_label") not in ("직접관련", "간접관련", "단순언급", "판단보류"):
            raise ValueError("Missing or incompatible reference label")
        ref_time = ref.get("expected_time_status", ref.get("time_status"))
        if ref_time not in ("신규", "과거", "불명") or (
            "time_status" in ref and ref["time_status"] != ref_time
        ):
            raise ValueError("Missing or incompatible reference event time")
        for quote in ref.get("evidence_sentences", []):
            if not isinstance(quote, str) or not quote or not any(quote in inputs[key][f] for f in ("title", "body")):
                raise ValueError("Reference quote not found verbatim in input")


def statistics(results, refs, keys):
    matrix = {a: {b: 0 for b in ("채택", "제외", "보류")} for a in ("채택", "제외", "보류")}
    for key in keys:
        matrix[refs[key]["expected_selection"]][results[key]["effective_selection"]] += 1
    return {"pairs": len(keys), "selection_counts": dict(Counter(results[k]["effective_selection"] for k in keys)),
        "reference_counts": dict(Counter(refs[k]["expected_selection"] for k in keys)),
        "matrix_rows_reference_columns_prediction": matrix,
        "selection_agreement_count": sum(matrix[x][x] for x in matrix),
        "model_adopted": sum(matrix[x]["채택"] for x in matrix),
        "adopted_and_reference_adopted": matrix["채택"]["채택"],
        "adopted_reference_excluded": matrix["제외"]["채택"],
        "adopted_reference_held": matrix["보류"]["채택"],
        "reference_adopted_but_not_selected": matrix["채택"]["제외"] + matrix["채택"]["보류"],
        "invalid_results": sum(bool(results[k]["validation_errors"]) for k in keys),
        "validation_errors": dict(Counter(e for k in keys for e in results[k]["validation_errors"])),
        "elapsed_seconds": sum(results[k]["elapsed_seconds"] for k in keys)}


def write_rows(path, rows):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        for row in rows:
            stream.write(base.canonical(row) + "\n")


def compare(args):
    if args.out.exists():
        raise FileExistsError("Use a new output directory")
    if args.cohort == "fresh" and not getattr(args, "freeze", None):
        raise ValueError("Fresh comparison requires --freeze")
    source_paths = [args.inputs, *args.reference, args.baseline, args.grounded,
                    args.baseline.with_name("protocol.json"), args.grounded.with_name("protocol.json"), Path(__file__)]
    source_hashes = {Path(path).resolve(): base.sha(path) for path in source_paths}
    inputs, refs = indexed([args.inputs]), indexed(args.reference)
    validate_references(inputs, refs)
    if args.cohort == "fresh":
        fresh_hashes = validate_fresh_freeze(args, inputs, refs)
        if any(path in source_hashes and source_hashes[path] != digest for path, digest in fresh_hashes.items()):
            raise ValueError("Fresh frozen source changed during comparison")
        source_hashes.update(fresh_hashes)
    baseline, baseline_protocol = load_results(args.baseline, inputs, args.inputs)
    grounded, grounded_protocol = load_results(args.grounded, inputs, args.inputs)
    if grounded_protocol.get("policy") != "grounded" or baseline_protocol.get("policy", "baseline") != "baseline":
        raise ValueError("Comparison policy roles differ")
    for field in ("model_repo", "model_revision", "receipt_sha256", "prompt_sha256", "temperature", "top_p", "top_k",
                  "max_input_tokens", "max_new_tokens", "attn_implementation", "do_sample", "seed_policy",
                  "quantization", "versions", "gpu", "truncation"):
        if baseline_protocol[field] != grounded_protocol[field]:
            raise ValueError("Uncontrolled runtime difference: " + field)
    summary = {"cohort": args.cohort, "human_gold": False, "human_review_completed": 0,
        "population_accuracy": None, "gnn_performance": None, "by_kind": {}, "sources": {}}
    for kind in ("real", "synthetic"):
        keys = [key for key, row in inputs.items() if row["case_kind"] == kind]
        if not keys:
            continue
        summary["by_kind"][kind] = {"baseline": statistics(baseline, refs, keys),
            "grounded": statistics(grounded, refs, keys),
            "changed_selection_count": sum(baseline[k]["effective_selection"] != grounded[k]["effective_selection"] for k in keys)}
    summary["sources"] = {str(path): digest for path, digest in source_hashes.items()}
    summary["fresh_freeze_verified"] = args.cohort == "fresh"
    audit, candidates, human_packet = [], [], []
    for key, row in inputs.items():
        audit.append({"article_id": key[0], "ticker": key[1], "case_kind": row["case_kind"],
            "baseline": baseline[key], "grounded": grounded[key], "ai_reference": refs[key], "human_gold": False})
        if row["case_kind"] != "real":
            continue
        human_packet.append({**row, "human_selection": None, "human_label": None, "human_time_status": None,
            "human_evidence_quotes": [], "human_reason": "", "review_status": "pending"})
        if grounded[key]["effective_selection"] == refs[key]["expected_selection"] == "채택":
            candidates.append({**row, "relevance": grounded[key]["prediction"], "fact_audit": grounded[key]["fact_audit"],
                "ai_reference": refs[key], "candidate_only": True, "human_reviewed": False,
                "gnn_labels_generated": False, "service_published": False})
    summary["review_candidates"] = len(candidates)
    for path, digest in source_hashes.items():
        if base.sha(path) != digest:
            raise ValueError("Source or freeze changed during comparison")
    args.out.mkdir(parents=True, exist_ok=False)
    write_rows(args.out / "pair_comparison.jsonl", audit)
    write_rows(args.out / "review_candidates.jsonl", candidates)
    write_rows(args.out / "human_review_packet.jsonl", human_packet)
    summary["output_sha256"] = {name: base.sha(args.out / name) for name in
        ("pair_comparison.jsonl", "review_candidates.jsonl", "human_review_packet.jsonl")}
    base.write_json(args.out / "summary.json", summary)
    print(base.canonical({"cohort": args.cohort, "by_kind": summary["by_kind"], "review_candidates": len(candidates)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--reference", type=Path, nargs="+", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--grounded", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cohort", choices=("development", "fresh"), required=True)
    parser.add_argument("--freeze", type=Path, help="Required for fresh: fresh_reference_freeze.json beside experiment_freeze.json")
    compare(parser.parse_args())
