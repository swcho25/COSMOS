"""Compare full-cohort v1/v3 selection or v3 without/with dated context.

General mode changes policy, compact input representation, extra-output-key
handling, and sampling to greedy decoding together; it is not a prompt-only
ablation. Context mode holds runtime fixed and permits only company_name and
company_context to differ. References are fallible independent AI reviews, not
human gold, population accuracy, or observed stock-price effects.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import local_news_relevance as base
import compare_relevance_policy_v2 as reference_contract
import run_relevance_two_stage as v1
import run_relevance_policy_v3 as v3

CHOICES = ("채택", "제외", "보류")
SHARED_SETTINGS = (
    "model_repo", "model_revision", "receipt_sha256", "prompt_sha256", "rows",
    "attn_implementation", "quantization", "seed_policy", "verification_seed_policy",
    "stage2_receives_stage1_output", "retry_policy", "truncation",
    "max_input_tokens", "max_new_tokens", "versions", "gpu",
)
GENERATION = ("do_sample", "temperature", "top_p", "top_k")
PROTOCOL_KEYS = set(SHARED_SETTINGS) | set(GENERATION) | {"schema", "policy", "code_hashes", "input_sha256"}
INPUT_KEYS = {"article_id", "ticker", "company_name", "published_at", "title", "body",
              "company_context", "source_split", "case_kind"}
STAGE_KEYS = {"raw_output", "prediction", "validation_errors", "generation_complete",
              "input_tokens", "output_tokens", "elapsed_seconds", "peak_cuda_allocated_bytes"}
RESULT_KEYS = {"article_id", "ticker", "case_kind", "source_split", "policy", "stages", "input_sha256",
               "validation_errors", "effective_selection", "peak_cuda_allocated_bytes", "elapsed_seconds",
               "input_tokens", "output_tokens"}
LENGTH_ERROR = "input_too_long_full_article_not_processed"
COMPLETE_ERROR = "generation_incomplete"
SEED = "sha256('20260916:' + canonical([article_id,ticker])) first 8 hex"
VERIFY_SEED = "sha256('20260916:verify:' + canonical([article_id,ticker])) first 8 hex"


def same(a, b):
    """JSON-exact equality also distinguishes False from 0 and 1 from True."""
    return base.canonical(a) == base.canonical(b)


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON value: " + value)


def read_json(text):
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def read_rows(path):
    # Match the frozen runner's file-level decoding, while rejecting duplicate
    # object keys and non-finite numbers that ordinary json.loads would accept.
    rows = [read_json(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("JSONL requires nonempty object rows")
    return rows


def indexed(rows):
    for row in rows:
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("article_id", "ticker")):
            raise ValueError("Pair identity must contain nonempty strings")
    return reference_contract.index_unique(rows)


def validate_inputs(before_rows, after_rows, mode):
    if mode not in ("general", "context"):
        raise ValueError("Unknown comparison mode")
    before, after = indexed(before_rows), indexed(after_rows)
    if list(before) != list(after):
        raise ValueError("Paired inputs must have identical keys and order")
    changes = Counter()
    for key in before:
        one, two = before[key], after[key]
        for row in (one, two):
            if not INPUT_KEYS.issubset(row):
                raise ValueError("Missing input fields, including explicit company_context")
            base.validate_input(row)
        if set(one) != set(two):
            raise ValueError("Full input field shape differs")
        if one["company_context"] is not None:
            raise ValueError("Before arm must have null company_context")
        different = {field for field in one if not same(one[field], two[field])}
        allowed = {"company_name", "company_context"} if mode == "context" else set()
        if different - allowed:
            raise ValueError("Undeclared input changes: " + ", ".join(sorted(different - allowed)))
        if mode == "general" and two["company_context"] is not None:
            raise ValueError("General comparison requires null company_context")
        if "company_name" in different and two["company_context"] is None:
            raise ValueError("Changed company name requires dated source context")
        if two["company_context"] is not None:
            context = two["company_context"]
            if not context["source"].strip() or not context["as_of"].strip():
                raise ValueError("Dated context source/as_of must not be blank")
        changes.update(different)
        changes["any_input_change"] += bool(different)
        changes["after_context_present"] += two["company_context"] is not None
    return before, after, dict(changes)


def validate_protocol(protocol, runner, input_path, count):
    if not isinstance(protocol, dict) or set(protocol) != PROTOCOL_KEYS:
        raise ValueError("Unexpected protocol field shape")
    schema = "relevance-two-stage-1" if runner is v1 else "relevance-two-stage-policy-v3-1"
    if protocol["schema"] != schema or not same(protocol["code_hashes"], runner.code_hashes()):
        raise ValueError("Runner/policy schema or code hash mismatch")
    if protocol["policy"] != "two_stage" or type(protocol["rows"]) is not int or protocol["rows"] != count:
        raise ValueError("Wrong protocol policy or row count")
    if protocol["input_sha256"] != base.sha(input_path):
        raise ValueError("Input file hash mismatch")
    for name in ("receipt_sha256", "prompt_sha256", "input_sha256"):
        if not isinstance(protocol[name], str) or re.fullmatch(r"[0-9a-f]{64}", protocol[name]) is None:
            raise ValueError("Invalid digest: " + name)
    for name in ("model_repo", "model_revision", "gpu"):
        if not isinstance(protocol[name], str) or not protocol[name].strip():
            raise ValueError("Missing model/runtime value: " + name)
    versions = protocol["versions"]
    if (not isinstance(versions, dict) or set(versions) != {"torch", "transformers", "bitsandbytes", "accelerate"}
            or any(not isinstance(value, str) or not value.strip() for value in versions.values())):
        raise ValueError("Incomplete runtime versions")
    for name in ("max_input_tokens", "max_new_tokens"):
        if type(protocol[name]) is not int or protocol[name] <= 0:
            raise ValueError("Invalid token cap: " + name)
    if (protocol["stage2_receives_stage1_output"] is not False or protocol["truncation"] is not False
            or protocol["retry_policy"] != "none" or protocol["seed_policy"] != SEED
            or protocol["verification_seed_policy"] != VERIFY_SEED
            or protocol["attn_implementation"] != "sdpa_explicit_kv"
            or protocol["quantization"] != "NF4 double quantization bf16"):
        raise ValueError("Independent full-text/no-retry/runtime contract differs")
    expected = {"do_sample": True, "temperature": 0.7, "top_p": 0.8, "top_k": 20} if runner is v1 else {
        "do_sample": False, "temperature": None, "top_p": None, "top_k": None}
    if any(not same(protocol[key], value) for key, value in expected.items()):
        raise ValueError("Undeclared generation configuration")


def load_arm(folder, inputs, input_path, runner):
    if (folder / "inference.lock").exists():
        raise ValueError("Comparison requires a completed, unlocked inference run")
    protocol = read_json((folder / "protocol.json").read_text(encoding="utf-8-sig"))
    validate_protocol(protocol, runner, input_path, len(inputs))
    records = indexed(read_rows(folder / "results.jsonl"))
    if set(records) != set(inputs):
        raise ValueError("All input pairs, including failures and abstentions, are required")
    for key, record in records.items():
        if not RESULT_KEYS.issubset(record) or set(record) - RESULT_KEYS - {"finished_at"}:
            raise ValueError("Unexpected result field shape")
        stages = record.get("stages")
        if not isinstance(stages, dict) or set(stages) != {"screen", "verify"}:
            raise ValueError("Invalid stage shape")
        for stage in stages.values():
            if stage is None:
                continue
            expected = STAGE_KEYS | ({"ignored_output_fields"} if runner is v3 else set())
            if not isinstance(stage, dict) or set(stage) != expected or not isinstance(stage["raw_output"], str):
                raise ValueError("Unexpected stage field shape")
        if type(record["peak_cuda_allocated_bytes"]) is not int:
            raise ValueError("Peak memory metric must be integer")
        runner.validate_result(record, inputs[key], protocol)
    return records, protocol


def validate_references(rows, inputs, path):
    indexed(rows)
    refs = reference_contract.validate_references(rows, inputs, path)
    for ref in refs.values():
        label = ref.get("expected_label", ref.get("label"))
        time = ref.get("expected_time_status", ref.get("time_status"))
        if ref["expected_selection"] == "채택" and (label not in ("직접관련", "간접관련") or time != "신규"):
            raise ValueError("Adopted reference must specify related, current information")
        if label == "판단보류" and ref["expected_selection"] != "보류":
            raise ValueError("Uncertain reference cannot force a choice")
    return refs


def measure(records, refs):
    value = reference_contract.measure(records, refs)
    value["denominator_all_pairs"] = len(refs)
    value["selections"] = {choice: value["selections"].get(choice, 0) for choice in CHOICES}
    length_pairs = format_pairs = incomplete_pairs = ignored_pairs = ignored_stages = second_pass = 0
    ignored_counts = Counter()
    stage_errors = {stage: Counter() for stage in ("screen", "verify")}
    for record in records.values():
        errors = [e.split(":", 1)[-1] for e in record["validation_errors"]]
        length_pairs += LENGTH_ERROR in errors
        incomplete_pairs += COMPLETE_ERROR in errors
        format_pairs += any(e not in (LENGTH_ERROR, COMPLETE_ERROR) for e in errors)
        record_ignored = False
        for name, stage in record.get("stages", {}).items():
            if stage is None:
                continue
            second_pass += name == "verify"
            stage_errors[name].update(stage["validation_errors"])
            fields = stage.get("ignored_output_fields", [])
            ignored_counts.update(fields)
            ignored_stages += bool(fields)
            record_ignored |= bool(fields)
        ignored_pairs += record_ignored
    value.update(input_length_exceeded_pairs=length_pairs, format_or_evidence_invalid_pairs=format_pairs,
                 generation_incomplete_pairs=incomplete_pairs, second_pass_count=second_pass,
                 stage_error_counts={key: dict(value) for key, value in stage_errors.items()},
                 ignored_output_fields={"pairs": ignored_pairs, "stages": ignored_stages,
                                        "field_occurrences": sum(ignored_counts.values()), "by_field": dict(ignored_counts)})
    return value


def compare(args):
    if args.out.exists():
        raise FileExistsError("Use a new output directory; previous comparisons are immutable")
    paths = [args.before_inputs, args.after_inputs, *args.references,
             args.before_run / "protocol.json", args.before_run / "results.jsonl",
             args.after_run / "protocol.json", args.after_run / "results.jsonl",
             Path(__file__), Path(reference_contract.__file__)]
    source_hashes = {str(path.resolve()): base.sha(path) for path in paths}
    before_inputs, after_inputs, changes = validate_inputs(read_rows(args.before_inputs), read_rows(args.after_inputs), args.mode)
    runner_before = v1 if args.mode == "general" else v3
    before, p1 = load_arm(args.before_run, before_inputs, args.before_inputs, runner_before)
    after, p2 = load_arm(args.after_run, after_inputs, args.after_inputs, v3)
    if any(not same(p1[key], p2[key]) for key in SHARED_SETTINGS):
        raise ValueError("Unequal model, token caps, versions, GPU or shared runtime settings")
    if args.mode == "context" and not same(p1["code_hashes"], p2["code_hashes"]):
        raise ValueError("Context comparison must use identical policy/runtime code")
    # General arms have identical null-context rows. Context references explicitly
    # review the dated-context input, never silently accept the no-context hash.
    ref_inputs = before_inputs if args.mode == "general" else after_inputs
    ref_path = args.before_inputs if args.mode == "general" else args.after_inputs
    refs = validate_references([r for path in args.references for r in read_rows(path)], ref_inputs, ref_path)
    constants = {}
    for choice in CHOICES:
        constants[choice] = measure({key: {"effective_selection": choice, "validation_errors": [], "elapsed_seconds": 0.0}
                                     for key in refs}, refs)
    summary = {
        "schema": "relevance-policy-v3-comparison-1", "mode": args.mode, "pairs": len(refs),
        "denominator_policy": "Every input pair, including invalid, overlength, incomplete and abstained results.",
        "before": measure(before, refs), "after": measure(after, refs), "all_constant_baselines": constants,
        "reference_counts": {choice: sum(r["expected_selection"] == choice for r in refs.values()) for choice in CHOICES},
        "human_gold": False, "reference_type": "independent_ai_review",
        "reference_hash_condition": "before_inputs" if args.mode == "general" else "after_inputs",
        "population_accuracy_claim": False, "gnn_training_performed": False, "actual_stock_effect_evaluated": False,
        "input_change_counts": changes,
        "declared_candidate_changes": (["v1 policy to compact current-event v3 policy", "lossless compact evidence catalog",
                                        "unsolicited JSON fields audited and ignored", "sampling .7/.8/20 to greedy"]
                                       if args.mode == "general" else ["dated company_context and contemporaneous company_name only"]),
        "policy_only_effect_identified": False,
        "model_settings": {key: p1[key] for key in SHARED_SETTINGS},
        "generation_settings": {"before": {k: p1[k] for k in GENERATION}, "after": {k: p2[k] for k in GENERATION}},
        "input_sha256": {"before": base.sha(args.before_inputs), "after": base.sha(args.after_inputs)},
        "source_sha256": source_hashes, "code_hashes": {"before": p1["code_hashes"], "after": p2["code_hashes"]},
        "limitations": ["AI-reference agreement is not independent human accuracy or population precision.",
                        "Exact quotation and valid format do not establish correct economic interpretation.",
                        "Context source truth/publication availability must also be audited in the registry; this comparator validates input schema and as_of.",
                        "Length and format counters can overlap; none are removed from the all-pair denominator."],
    }
    pairs = [{"article_id": key[0], "ticker": key[1], "title": after_inputs[key]["title"],
              "before": before[key], "after": after[key], "reference": refs[key],
              "choice_changed": before[key]["effective_selection"] != after[key]["effective_selection"]} for key in after_inputs]
    summary["changed_choices"] = sum(row["choice_changed"] for row in pairs)
    if any(base.sha(path) != digest for path, digest in source_hashes.items()):
        raise ValueError("Source/result/reference changed while comparing")
    if (runner_before.code_hashes() != p1["code_hashes"] or v3.code_hashes() != p2["code_hashes"]
            or (args.before_run / "inference.lock").exists() or (args.after_run / "inference.lock").exists()):
        raise ValueError("Runtime changed or inference restarted while comparing")
    args.out.mkdir(parents=True, exist_ok=False)
    base.write_json(args.out / "summary.json", summary)
    (args.out / "pair_comparison.jsonl").write_text("".join(base.canonical(row) + "\n" for row in pairs), encoding="utf8")
    print(base.canonical(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before-inputs", "after-inputs", "before-run", "after-run", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--references", nargs="+", type=Path, required=True)
    parser.add_argument("--mode", choices=("general", "context"), required=True)
    compare(parser.parse_args())
