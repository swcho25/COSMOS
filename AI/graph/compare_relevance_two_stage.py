"""Evaluate frozen two-stage selections, retaining all errors and abstentions.

References are fallible AI judgments, never human gold. Also report the first
pass alone so a conservative verifier cannot conceal lost relevant articles.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import local_news_relevance as base
import run_relevance_followup as previous
import run_relevance_two_stage as runner
import compare_relevance_followup as comparison


def verify_freeze(args, inputs, refs):
    freeze = json.loads(args.freeze.read_text(encoding="utf8"))
    if (freeze.get("human_gold") is not False or freeze.get("model_outputs_seen_by_reviewers") is not False
            or freeze.get("inference_not_started") is not True or freeze.get("pairs") != len(inputs)):
        raise ValueError("Invalid pre-inference reference freeze")
    requested = {Path(p).resolve() for p in [args.inputs, *args.reference]}
    files = {Path(p).resolve(): h for p, h in freeze["files"].items()}
    if set(files) != requested or len(files) != len(freeze["files"]):
        raise ValueError("Frozen input/reference inventory differs")
    for path, digest in files.items():
        if base.sha(path) != digest:
            raise ValueError("Frozen input/reference changed")
    policy_path = args.freeze.with_name("policy_freeze.json")
    if base.sha(policy_path) != freeze["policy_freeze_sha256"]:
        raise ValueError("Policy freeze changed")
    fixed = json.loads(policy_path.read_text(encoding="utf8"))
    if (fixed.get("fixed_before_fresh_article_reference_or_output_inspection") is not True
            or fixed["code_hashes"] != runner.code_hashes()
            or fixed["baseline_code_hashes"] != previous.code_hashes("baseline")
            or fixed["prompt_sha256"] != base.sha(Path(base.__file__).with_name("news_relevance_prompt.txt"))):
        raise ValueError("Runtime/prompt is not the frozen experiment")
    if fixed["input_sha256"] != files[args.inputs.resolve()]:
        raise ValueError("Policy frozen against different input")
    for key, row in inputs.items():
        ref = refs[key]
        if (ref.get("model_outputs_seen") is not False or ref.get("full_article_text_read") is not True
                or ref.get("input_file_sha256") != files[args.inputs.resolve()]
                or ref.get("input_sha256") != hashlib.sha256(base.canonical(row).encode()).hexdigest()):
            raise ValueError("Reference full-text provenance differs")
        for name, field in (("body_sha256", "body"), ("title_sha256", "title")):
            if name in ref and ref[name] != hashlib.sha256(row[field].encode()).hexdigest():
                raise ValueError("Reference text hash differs")
        for name, field in (("reviewed_body_chars", "body"), ("reviewed_title_chars", "title")):
            if name in ref and ref[name] != len(row[field]):
                raise ValueError("Reference text length differs")
    return files | {args.freeze.resolve(): base.sha(args.freeze), policy_path.resolve(): base.sha(policy_path)}


def compare(args):
    if args.out.exists():
        raise FileExistsError("Use a new comparison directory")
    sources = {p.resolve(): base.sha(p) for p in [args.inputs, *args.reference, args.baseline, args.two_stage,
        args.baseline.with_name("protocol.json"), args.two_stage.with_name("protocol.json"), args.freeze, Path(__file__)]}
    inputs, refs = comparison.indexed([args.inputs]), comparison.indexed(args.reference)
    comparison.validate_references(inputs, refs)
    pins = verify_freeze(args, inputs, refs)
    if any(path in sources and sources[path] != digest for path, digest in pins.items()):
        raise ValueError("Frozen source changed during read")
    sources.update(pins)
    baseline, bp = comparison.load_results(args.baseline, inputs, args.inputs)
    if bp.get("policy") != "baseline":
        raise ValueError("Wrong baseline policy")
    second = comparison.indexed([args.two_stage])
    tp = json.loads(args.two_stage.with_name("protocol.json").read_text(encoding="utf8"))
    if (set(second) != set(inputs) or tp["input_sha256"] != base.sha(args.inputs) or tp["rows"] != len(inputs)
            or tp.get("schema") != "relevance-two-stage-1" or tp.get("policy") != "two_stage"
            or tp["code_hashes"] != runner.code_hashes() or tp.get("stage2_receives_stage1_output") is not False
            or tp.get("retry_policy") != "none"
            or tp.get("verification_seed_policy") != "sha256('20260916:verify:' + canonical([article_id,ticker])) first 8 hex"):
        raise ValueError("Two-stage input/runtime differs")
    for key, result in second.items():
        runner.validate_result(result, inputs[key], tp)
    for name in ("model_repo", "model_revision", "receipt_sha256", "prompt_sha256", "temperature", "top_p", "top_k",
                 "max_input_tokens", "max_new_tokens", "attn_implementation", "do_sample", "seed_policy",
                 "quantization", "versions", "gpu", "truncation"):
        if bp[name] != tp[name]:
            raise ValueError("Uncontrolled difference: " + name)
    first = {}
    for key, result in second.items():
        stage = result["stages"]["screen"]
        first[key] = {"effective_selection": "보류" if stage["validation_errors"] else
            {"adopt": "채택", "exclude": "제외", "hold": "보류"}[stage["prediction"]["decision"]],
            "validation_errors": stage["validation_errors"], "elapsed_seconds": stage["elapsed_seconds"]}
    keys = list(inputs)
    if any(r["case_kind"] != "real" for r in inputs.values()):
        raise ValueError("Fresh cohort must contain real news only")
    stats = {name: comparison.statistics(result, refs, keys) for name, result in
             (("baseline", baseline), ("screen_only", first), ("two_stage", second))}
    summary = {"cohort": "fresh_two_stage", "pairs": len(inputs), "human_gold": False,
        "human_review_completed": 0, "population_accuracy": None, "gnn_performance": None,
        "metrics": stats, "second_pass_count": sum(r["stages"]["verify"] is not None for r in second.values()),
        "second_pass_changed_adoptions": sum(first[k]["effective_selection"] == "채택" and second[k]["effective_selection"] != "채택" for k in keys),
        "sources": {str(p): h for p, h in sources.items()}}
    audit, candidates, conflicts = [], [], []
    for key, row in inputs.items():
        audit.append({"article_id": key[0], "ticker": key[1], "baseline": baseline[key],
                      "two_stage": second[key], "ai_reference": refs[key], "human_gold": False})
        if second[key]["effective_selection"] == "채택":
            item = {**row, "stages": second[key]["stages"], "ai_reference": refs[key], "candidate_only": True,
                    "human_reviewed": False, "gnn_labels_generated": False, "service_published": False}
            (candidates if refs[key]["expected_selection"] == "채택" else conflicts).append(item)
    for path, digest in sources.items():
        if base.sha(path) != digest:
            raise ValueError("Source changed while comparing")
    args.out.mkdir(parents=True, exist_ok=False)
    for name, records in (("pair_comparison.jsonl", audit), ("review_candidates.jsonl", candidates), ("disputed_adoptions.jsonl", conflicts)):
        comparison.write_rows(args.out / name, records)
    summary.update(review_candidates=len(candidates), disputed_adoptions=len(conflicts),
        output_sha256={name: base.sha(args.out / name) for name in
            ("pair_comparison.jsonl", "review_candidates.jsonl", "disputed_adoptions.jsonl")})
    base.write_json(args.out / "summary.json", summary)
    print(base.canonical({k: v for k, v in summary.items() if k not in ("sources", "output_sha256")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("inputs", "baseline", "two-stage", "freeze", "out"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--reference", type=Path, nargs="+", required=True)
    compare(parser.parse_args())
