"""Evaluate an offline relevance pilot with isolated real/synthetic diagnostics.

Reference reviews never become model inputs. Prior AI reviews are fallible
current-event proxies, not economic-relevance gold, population accuracy, or GNN
performance evidence. Missing, extra, duplicate or altered pairs fail closed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics

import local_news_relevance as runner


SELECTIONS = ("채택", "제외", "보류")
LABELS = ("직접관련", "간접관련", "단순언급", "판단보류")
TIMES = ("신규", "과거", "불명")
QUOTE_ERRORS = {"invalid_evidence_list", "evidence_not_in_article", "evidence_fragment_too_short",
                "invalid_evidence_id_types", "too_many_evidence_ids", "duplicate_evidence_ids",
                "unknown_evidence_id", "blank_evidence_segment"}
OVERLENGTH = "input_too_long_full_article_not_processed"
ATTENTION_CODE_PATH = Path(__file__).with_name("local_relevance_attention.py")


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate_attention_protocol(protocol):
    """Verify optional v4 attention code without importing Torch or executing it."""
    if "attention_code_sha256" not in protocol:
        if protocol.get("attn_implementation") == "sdpa_explicit_kv":
            raise ValueError("Explicit KV attention protocol is missing its code hash")
        return None
    path = ATTENTION_CODE_PATH.resolve()
    if not path.is_file() or protocol["attention_code_sha256"] != sha(path):
        raise ValueError("Attention implementation differs from the frozen protocol")
    return path


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"Expected object in {path}:{line_number}")
                yield value


def key(row):
    if any(not isinstance(row.get(field), str) or not row[field].strip() for field in ("article_id", "ticker")):
        raise ValueError("Pair requires nonempty string article_id and ticker")
    return row["article_id"], row["ticker"]


def index_rows(paths, role):
    indexed = {}
    for path in paths:
        for row in read_rows(path):
            pair = key(row)
            if pair in indexed:
                raise ValueError(f"Duplicate {role} pair: {pair}")
            indexed[pair] = row
    if not indexed:
        raise ValueError(f"Empty {role} dataset")
    return indexed


def require_same_pairs(inputs, reference, results):
    expected = set(inputs)
    for role, values in (("reference", reference), ("results", results)):
        missing, extra = expected - set(values), set(values) - expected
        if missing or extra:
            raise ValueError(f"{role} pair mismatch: missing={len(missing)}, extra={len(extra)}, "
                             f"missing_examples={sorted(missing)[:3]}, extra_examples={sorted(extra)[:3]}")


def number(value, name, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (integer and not isinstance(value, int))):
        raise ValueError("Invalid nonnegative runtime metric: " + name)
    return value


def validate_result(result, article, protocol=None):
    """Recompute schema/quote checks rather than trusting cached success flags."""
    expected_hash = hashlib.sha256(runner.canonical(article).encode("utf8")).hexdigest()
    if result.get("input_sha256") != expected_hash:
        raise ValueError("Result input_sha256 mismatch: " + str(key(article)))
    if result.get("case_kind") != article["case_kind"] or result.get("source_split") != article.get("source_split"):
        raise ValueError("Result provenance mismatch: " + str(key(article)))
    for name in ("input_tokens", "output_tokens", "peak_cuda_allocated_bytes"):
        number(result.get(name), name, integer=True)
    number(result.get("elapsed_seconds"), "elapsed_seconds")
    if not isinstance(result.get("raw_output"), str) or not isinstance(result.get("generation_complete"), bool):
        raise ValueError("Result raw_output/generation_complete contract missing")
    if not isinstance(result.get("validation_errors"), list) or any(not isinstance(x, str) for x in result["validation_errors"]):
        raise ValueError("Result validation_errors must be a string list")
    if protocol is not None:
        runner.validate_resume_result(result, article, protocol["max_input_tokens"], protocol["max_new_tokens"],
                                      protocol.get("evidence_mode", "verbatim"))
        errors = result["validation_errors"]
    else:
        # The actual runner always supplies protocol.json. This fallback permits
        # evaluation of exported fixtures, while explicitly reporting that token
        # limits cannot be independently checked without their original protocol.
        if result["validation_errors"] == [OVERLENGTH]:
            prediction, errors = None, [OVERLENGTH]
            if result["raw_output"] or result["output_tokens"] or result["generation_complete"]:
                raise ValueError("Overlength record contains generated output")
        else:
            prediction, errors = runner.validate_prediction(result["raw_output"], article,
                                                            result.get("evidence_mode", "verbatim"))
            if not result["generation_complete"]:
                errors.append("generation_incomplete")
        selection = "보류" if errors else prediction["selection"]
        if (prediction != result.get("prediction") or errors != result["validation_errors"]
                or selection != result.get("effective_selection")):
            raise ValueError("Result cached validation/prediction differs from raw output")
    if result.get("effective_selection") not in SELECTIONS:
        raise ValueError("Invalid effective selection")
    return errors


def validate_reference(reference, article):
    if reference.get("expected_selection") not in SELECTIONS:
        raise ValueError("Reference requires a three-way expected_selection")
    for name, choices in (("expected_label", LABELS), ("expected_time_status", TIMES)):
        if reference.get(name) is not None and reference[name] not in choices:
            raise ValueError("Invalid reference enum: " + name)
    for name in ("case_kind", "source_split"):
        if name in reference and reference[name] != article.get(name):
            raise ValueError("Reference provenance differs from input")
    if article["case_kind"] == "real" and reference.get("reference_type") not in {"prior_ai_review", "independently_reviewed", "independent_ai_review"}:
        raise ValueError("Real reference requires explicit reference_type")


def matrix(row_labels, column_labels):
    return {row: dict.fromkeys(column_labels, 0) for row in row_labels}


def runtime_summary(results):
    seconds = [row["elapsed_seconds"] for row in results]
    elapsed = math.fsum(seconds)
    input_tokens = sum(row["input_tokens"] for row in results)
    output_tokens = sum(row["output_tokens"] for row in results)
    gpu_peak = max((row["peak_cuda_allocated_bytes"] for row in results), default=None)
    return {
        "pairs": len(results), "total_elapsed_seconds": elapsed,
        "mean_seconds_per_pair": statistics.mean(seconds) if seconds else None,
        "median_seconds_per_pair": statistics.median(seconds) if seconds else None,
        "max_seconds_per_pair": max(seconds, default=None),
        "input_tokens_total": input_tokens, "output_tokens_total": output_tokens,
        "generated_pairs": sum(row["output_tokens"] > 0 for row in results),
        "input_tokens_on_generated_pairs": sum(row["input_tokens"] for row in results if row["output_tokens"] > 0),
        "output_tokens_per_elapsed_second": output_tokens / elapsed if elapsed else None,
        "seconds_per_output_token": elapsed / output_tokens if output_tokens else None,
        "peak_cuda_allocated_bytes": gpu_peak,
        "peak_cuda_allocated_gib": gpu_peak / 1024 ** 3 if gpu_peak is not None else None,
        "timing_definition": "Per-pair runner elapsed time, including encoding/generation/validation; excludes model download/load and evaluator time",
        "gpu_definition": "Maximum torch CUDA allocated memory across pairs; not total device usage, reserved VRAM, or utilization",
    }


def summarize_kind(audits, results):
    effective = matrix(SELECTIONS, SELECTIONS)
    raw = matrix(SELECTIONS, SELECTIONS + ("invalid_or_missing",))
    labels = matrix(LABELS, LABELS + ("invalid_or_missing",))
    times = matrix(TIMES, TIMES + ("invalid_or_missing",))
    errors, reference_types = Counter(), Counter()
    for row in audits:
        effective[row["expected_selection"]][row["effective_selection"]] += 1
        raw[row["expected_selection"]][row["raw_selection"] if row["raw_selection"] in SELECTIONS else "invalid_or_missing"] += 1
        if row["expected_label"] is not None:
            predicted = row["predicted_label"] if row["predicted_label"] in LABELS else "invalid_or_missing"
            labels[row["expected_label"]][predicted] += 1
        if row["expected_time_status"] is not None:
            predicted = row["predicted_time_status"] if row["predicted_time_status"] in TIMES else "invalid_or_missing"
            times[row["expected_time_status"]][predicted] += 1
        errors.update(row["validation_errors"])
        reference_types[row["reference_type"]] += 1
    return {
        "pairs": len(audits), "selection_matrix_axes": {"rows": "reference", "columns": "prediction"},
        "effective_selection_matrix": effective, "raw_selection_matrix": raw,
        "effective_selection_counts": dict(Counter(row["effective_selection"] for row in audits)),
        "reference_selection_counts": dict(Counter(row["expected_selection"] for row in audits)),
        "selection_proxy_agreement_count": sum(row["effective_selection"] == row["expected_selection"] for row in audits),
        "selection_proxy_disagreement_count": sum(row["effective_selection"] != row["expected_selection"] for row in audits),
        "label_reference_pairs": sum(row["expected_label"] is not None for row in audits), "label_matrix": labels,
        "time_reference_pairs": sum(row["expected_time_status"] is not None for row in audits), "time_status_matrix": times,
        "invalid_count": sum(bool(row["validation_errors"]) for row in audits),
        "quote_failure_count": sum(row["quote_failure"] for row in audits),
        "incomplete_generation_count": sum("generation_incomplete" in row["validation_errors"] for row in audits),
        "overlength_count": sum(OVERLENGTH in row["validation_errors"] for row in audits),
        "valid_abstention_count": sum(not row["validation_errors"] and row["effective_selection"] == "보류" for row in audits),
        "invalid_forced_abstention_count": sum(bool(row["validation_errors"]) and row["effective_selection"] == "보류" for row in audits),
        "validation_error_counts": dict(errors), "reference_types": dict(reference_types),
        "review_packet_pairs": sum(bool(row["review_reasons"]) for row in audits),
        "runtime": runtime_summary(results),
    }


def compare(inputs, reference, results, protocol=None):
    require_same_pairs(inputs, reference, results)
    audits, packets = [], []
    for pair in inputs:  # Retain the runner input order for readable review.
        article, ref, result = inputs[pair], reference[pair], results[pair]
        runner.validate_input(article)
        validate_reference(ref, article)
        errors = validate_result(result, article, protocol)
        prediction = result["prediction"] if isinstance(result["prediction"], dict) else {}
        reasons = []
        if ref["expected_selection"] != result["effective_selection"]:
            reasons.append("selection_proxy_disagreement")
        for reference_field, prediction_field in (("expected_label", "label"), ("expected_time_status", "time_status")):
            if ref.get(reference_field) is not None and ref[reference_field] != prediction.get(prediction_field):
                reasons.append(prediction_field + "_reference_disagreement")
        if errors:
            reasons.append("invalid_output")
        quote_failure = bool(QUOTE_ERRORS & set(errors)) or any(error.startswith("invalid_evidence_catalog:") for error in errors)
        if quote_failure:
            reasons.append("quote_validation_failure")
        audit = {
            "article_id": pair[0], "ticker": pair[1], "case_kind": article["case_kind"],
            "expected_selection": ref["expected_selection"], "effective_selection": result["effective_selection"],
            "raw_selection": prediction.get("selection"), "expected_label": ref.get("expected_label"),
            "predicted_label": prediction.get("label"), "expected_time_status": ref.get("expected_time_status"),
            "predicted_time_status": prediction.get("time_status"), "validation_errors": errors,
            "quote_failure": quote_failure, "review_reasons": reasons,
            "reference_type": ref.get("reference_type", "precommitted_synthetic_diagnostic"),
        }
        audits.append(audit)
        if reasons:
            packets.append({**article, "prediction": result["prediction"], "raw_output": result["raw_output"],
                            "effective_selection": result["effective_selection"], "validation_errors": errors,
                            "review_reasons": reasons, "prior_reference": ref,
                            "review_status": "pending", "reference_is_human_gold": False})
    by_kind = {kind: summarize_kind([row for row in audits if row["case_kind"] == kind],
                                  [row for row in results.values() if row["case_kind"] == kind])
               for kind in ("real", "synthetic")}
    summary = {
        "schema_version": "local-relevance-evaluation-1", "pairs": len(inputs),
        "pair_integrity": {"inputs": len(inputs), "reference": len(reference), "results": len(results),
                           "missing": 0, "extra": 0, "duplicates": 0, "exact_pair_match": True,
                           "result_input_hashes_verified": True},
        "by_case_kind": by_kind, "runtime_all_pairs": runtime_summary(list(results.values())),
        "quote_failure_definition": sorted(QUOTE_ERRORS),
        "token_budget_validation_available": protocol is not None,
        "reference_is_gold": False, "human_review_completed": 0,
        "population_accuracy": None, "gnn_performance": None, "stock_prediction_accuracy": None,
        "limitations": [
            "Prior AI reviews are fallible current-event selection proxies, not human gold for economic relevance.",
            "The balanced real pilot is development data; matrix counts are not source-population accuracy, precision, or recall.",
            "Synthetic diagnostics are reported separately and cannot establish real-news performance.",
            "Valid schema and verbatim evidence do not certify company attribution, economic reasoning, event recency, or truth.",
            "Invalid results remain visible and their effective decision is abstention; they are not counted as valid abstentions.",
            "These diagnostics do not measure GNN performance or stock-return prediction, and do not authorize training or deployment.",
        ],
    }
    return summary, audits, packets


def write_jsonl(path, records):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        for row in records:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def evaluate(*, inputs, reference, results, out):
    input_paths, reference_paths = [Path(p) for p in inputs], [Path(p) for p in reference]
    results, out = Path(results), Path(out)
    if out.exists():
        raise FileExistsError("Choose a new evaluation directory; existing outputs are immutable")
    indexed_inputs = index_rows(input_paths, "input")
    indexed_reference = index_rows(reference_paths, "reference")
    indexed_results = index_rows([results], "result")
    protocol_path = results.with_name("protocol.json")
    protocol = json.loads(protocol_path.read_text(encoding="utf8")) if protocol_path.exists() else None
    attention_path = None
    if protocol is not None:
        attention_path = validate_attention_protocol(protocol)
        for name in ("max_input_tokens", "max_new_tokens"):
            number(protocol.get(name), name, integer=True)
        if protocol.get("evidence_mode") == "sentence_ids":
            import relevance_sentence_evidence
            if protocol.get("evidence_code_sha256") != sha(relevance_sentence_evidence.__file__):
                raise ValueError("Evidence resolver differs from the frozen protocol")
        if protocol.get("code_sha256") is not None and protocol["code_sha256"] != sha(runner.__file__):
            raise ValueError("Runner code changed; validator cannot reproduce the frozen result contract")
        if len(input_paths) == 1 and protocol.get("input_sha256") != sha(input_paths[0]):
            raise ValueError("Runner protocol input file hash mismatch")
        if protocol.get("rows") is not None and protocol["rows"] != len(indexed_inputs):
            raise ValueError("Runner protocol row count differs")
    summary, audits, packets = compare(indexed_inputs, indexed_reference, indexed_results, protocol)
    summary["sources"] = {
        "inputs": [{"path": str(p.resolve()), "sha256": sha(p)} for p in input_paths],
        "reference": [{"path": str(p.resolve()), "sha256": sha(p)} for p in reference_paths],
        "results": {"path": str(results.resolve()), "sha256": sha(results)},
        "protocol": {"path": str(protocol_path.resolve()), "sha256": sha(protocol_path)} if protocol else None,
        "attention_implementation": {"path": str(attention_path), "sha256": protocol["attention_code_sha256"]} if attention_path else None,
        "evaluator_sha256": sha(__file__), "prediction_validator_sha256": sha(runner.__file__),
    }
    summary["run"] = {name: protocol.get(name) for name in (
        "model_repo", "model_revision", "receipt_sha256", "prompt_sha256", "gpu", "quantization", "versions",
        "max_input_tokens", "max_new_tokens", "truncation", "do_sample", "evidence_mode",
        "evidence_code_sha256", "attn_implementation", "attention_code_sha256",
        "temperature", "top_p", "top_k", "seed_policy")} if protocol else None
    out.mkdir(parents=True, exist_ok=False)
    write_jsonl(out / "pair_audit.jsonl", audits)
    write_jsonl(out / "review_packet.jsonl", packets)
    summary["files"] = {name: {"sha256": sha(out / name), "bytes": (out / name).stat().st_size}
                        for name in ("pair_audit.jsonl", "review_packet.jsonl")}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", type=Path, required=True)
    parser.add_argument("--reference", nargs="+", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    summary = evaluate(**vars(parser.parse_args()))
    print(json.dumps({"pairs": summary["pairs"], "by_case_kind": {
        kind: {name: group[name] for name in ("pairs", "invalid_count", "quote_failure_count", "review_packet_pairs",
                                            "effective_selection_matrix")}
        for kind, group in summary["by_case_kind"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
