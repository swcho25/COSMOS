"""Build human-review candidates from complete real pilot decisions only.

Selection agreement between a local model and a frozen independent AI review is
not human ground truth, ensemble accuracy, or a GNN return label. This utility
does not load price/return data, edit GNN data, or publish anything to a service.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import evaluate_local_relevance as evaluation
import local_news_relevance as runner


REAL_PAIRS = 40
ROW_HASH_SCOPE = "UTF-8 JSONL line bytes excluding line terminator"


def hash_text(value):
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def read_frozen_jsonl(path):
    """Retain exact line hashes so the two independent review schemas can agree."""
    result = {}
    with Path(path).open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            raw = line.rstrip(b"\r\n")
            if not raw.strip():
                continue
            row = json.loads(raw.decode("utf-8-sig" if line_number == 1 else "utf8"))
            pair = evaluation.key(row)
            if pair in result:
                raise ValueError("Duplicate frozen original input pair")
            result[pair] = {"row": row, "line_number": line_number,
                            "raw_line_sha256": hashlib.sha256(raw).hexdigest(),
                            "canonical_sha256": hash_text(runner.canonical(row))}
    return result


def frozen_sources(freeze_path, inputs_path, reference_paths):
    freeze_path = Path(freeze_path).resolve()
    freeze = json.loads(freeze_path.read_text(encoding="utf8"))
    if (freeze.get("human_gold") is not False or freeze.get("model_outputs_seen") is not False
            or freeze.get("source_quotes_checked") is not True or freeze.get("review_pairs") != REAL_PAIRS):
        raise ValueError("Independent reference freeze must describe 40 non-gold, outcome-blind reviewed pairs")
    inventory = freeze.get("files")
    if not isinstance(inventory, dict) or "inputs.jsonl" not in inventory:
        raise ValueError("Freeze is missing original inputs.jsonl")
    paths = {}
    for name, expected_hash in inventory.items():
        path = (freeze_path.parent / name).resolve()
        if not path.is_relative_to(freeze_path.parent) or not path.is_file():
            raise ValueError("Frozen source path missing or outside its directory")
        if evaluation.sha(path) != expected_hash:
            raise ValueError("Frozen source changed: " + name)
        paths[path] = expected_hash
    for path in [inputs_path, *reference_paths]:
        if Path(path).resolve() not in paths:
            raise ValueError("Requested input/reference is not pinned in the independent review freeze")
    return freeze, paths, freeze_path.parent / "inputs.jsonl"


def validate_independent_reference(reference, article, original, original_file_sha256, protocol):
    evaluation.validate_reference(reference, article)
    if reference.get("reference_type") != "independent_ai_review" or reference.get("human_gold") is not False:
        raise ValueError("Candidate selection requires independent AI references explicitly marked non-gold")
    if any(reference.get(name) is True for name in (
            "model_predictions_consulted", "prior_evaluations_consulted", "prices_consulted", "future_outcomes_consulted")):
        raise ValueError("Reference consulted predictions, prior decisions, or price outcomes")
    if reference.get("full_input_read") is not True and reference.get("full_article_text_read") is not True:
        raise ValueError("Reference does not attest to reading the full input")
    if original["row"] != article:
        raise ValueError("Combined input differs from independently reviewed original")
    if "input_file_sha256" in reference:
        # Reviewer 01: canonical row hash plus a separate source file hash.
        if (reference["input_file_sha256"] != original_file_sha256
                or reference.get("input_sha256") != original["canonical_sha256"]
                or reference.get("input_row_number") != original["line_number"]):
            raise ValueError("Reference canonical row/file provenance mismatch")
        if (reference.get("body_sha256") != hash_text(article["body"])
                or reference.get("reviewed_body_chars") != len(article["body"])
                or reference.get("reviewed_title_chars") != len(article["title"])):
            raise ValueError("Reference reviewed-text length/hash mismatch")
    else:
        # Reviewer 02: source file hash and exact JSONL line-byte hash.
        if (reference.get("input_sha256") != original_file_sha256
                or reference.get("input_row_sha256") != original["raw_line_sha256"]
                or reference.get("input_row_hash_scope") != ROW_HASH_SCOPE
                or reference.get("source_line") != original["line_number"]):
            raise ValueError("Reference original line/file provenance mismatch")
    if reference.get("prompt_sha256") is not None and reference["prompt_sha256"] != protocol["prompt_sha256"]:
        raise ValueError("Reference uses a different decision prompt")
    time_status = reference.get("expected_time_status", reference.get("time_status"))
    if time_status not in evaluation.TIMES:
        raise ValueError("Reference time decision missing")
    if "time_status" in reference and reference["time_status"] != time_status:
        raise ValueError("Reference time fields disagree")
    evidence = reference.get("evidence_sentences")
    if not isinstance(evidence, list) or any(not isinstance(quote, str) or not quote.strip() for quote in evidence):
        raise ValueError("Reference evidence list invalid")
    sources = (runner.normalized(article["title"]), runner.normalized(article["body"]))
    if any(not any(runner.normalized(quote) in text for text in sources) for quote in evidence):
        raise ValueError("Independent reference quote is not in the reviewed article")
    if reference["expected_selection"] == "채택" and (
            reference.get("expected_label") not in {"직접관련", "간접관련"} or time_status != "신규" or not evidence):
        raise ValueError("Independent adoption lacks a current related event/evidence")


def build(*, inputs, results, reference, out, freeze=None):
    inputs, results, out = Path(inputs).resolve(), Path(results).resolve(), Path(out)
    reference_paths = [Path(path).resolve() for path in reference]
    freeze_path = Path(freeze).resolve() if freeze else inputs.parent / "independent_review_freeze.json"
    if out.exists():
        raise FileExistsError("Choose a new candidate directory; existing output is immutable")
    freeze_sha256 = evaluation.sha(freeze_path)
    frozen, pinned_sources, original_path = frozen_sources(freeze_path, inputs, reference_paths)
    all_inputs = evaluation.index_rows([inputs], "input")
    for article in all_inputs.values():
        runner.validate_input(article)
    real = {pair: row for pair, row in all_inputs.items() if row["case_kind"] == "real"}
    if len(real) != REAL_PAIRS:
        raise ValueError("Expected exactly 40 real article-company inputs")
    originals = read_frozen_jsonl(original_path)
    if set(originals) != set(real):
        raise ValueError("Frozen original input pair set differs from real combined inputs")
    references = evaluation.index_rows(reference_paths, "independent reference")
    result_hash = evaluation.sha(results)
    model_results = evaluation.index_rows([results], "model result")
    if set(model_results) - set(all_inputs):
        raise ValueError("Result contains a pair absent from combined inputs")
    real_results = {pair: model_results[pair] for pair in real if pair in model_results}
    evaluation.require_same_pairs(real, references, real_results)
    protocol_path = results.with_name("protocol.json")
    protocol_sha256 = evaluation.sha(protocol_path)
    protocol = json.loads(protocol_path.read_text(encoding="utf8"))
    if protocol.get("input_sha256") != evaluation.sha(inputs) or protocol.get("rows") != len(all_inputs):
        raise ValueError("Model protocol inputs do not match combined inputs")
    if protocol.get("code_sha256") != evaluation.sha(runner.__file__):
        raise ValueError("Model runner changed; cannot reproduce frozen validation")
    attention_path = evaluation.validate_attention_protocol(protocol)
    evidence_path = None
    if protocol.get("evidence_mode", "verbatim") == "sentence_ids":
        import relevance_sentence_evidence
        evidence_path = Path(relevance_sentence_evidence.__file__)
        if protocol.get("evidence_code_sha256") != evaluation.sha(evidence_path):
            raise ValueError("Evidence resolver differs from the frozen protocol")
    elif protocol.get("evidence_mode", "verbatim") != "verbatim":
        raise ValueError("Unknown model evidence mode")
    for name in ("max_input_tokens", "max_new_tokens"):
        evaluation.number(protocol.get(name), name, integer=True)
    original_file_sha256 = evaluation.sha(original_path)
    reference_location = {evaluation.key(row): {"path": str(path), "sha256": evaluation.sha(path)}
                          for path in reference_paths for row in evaluation.read_rows(path)}
    agreed, disputed, pairs = [], [], []
    for pair, article in real.items():
        result, ref = real_results[pair], references[pair]
        errors = evaluation.validate_result(result, article, protocol)
        validate_independent_reference(ref, article, originals[pair], original_file_sha256, protocol)
        model_adopts = result["effective_selection"] == "채택"
        bucket = ("review_agreed" if ref["expected_selection"] == "채택" else "disputed") if model_adopts else "not_model_adopted"
        audit_pair = {
            "article_id": pair[0], "ticker": pair[1], "input_sha256": originals[pair]["canonical_sha256"],
            "original_input_line_sha256": originals[pair]["raw_line_sha256"],
            "title_sha256": hash_text(article["title"]), "body_sha256": hash_text(article["body"]),
            "result_sha256": hash_text(runner.canonical(result)), "reference": reference_location[pair],
            "protocol_sha256": protocol_sha256, "model_effective_selection": result["effective_selection"],
            "independent_expected_selection": ref["expected_selection"], "validation_errors": errors,
            "bucket": bucket, "human_reviewed": False, "gnn_labels_generated": False,
        }
        pairs.append(audit_pair)
        if model_adopts:
            candidate = {
                **article, "input_sha256": originals[pair]["canonical_sha256"],
                "relevance_prediction": result["prediction"], "effective_selection": result["effective_selection"],
                "validation_errors": errors, "independent_ai_reference": ref,
                "selection_bucket": bucket, "candidate_only": True, "ai_assisted_candidate": True,
                "human_reviewed": False, "human_gold": False, "review_status": "pending_human_review",
                "gnn_labels_generated": False, "service_published": False,
                "model_protocol_sha256": protocol_sha256,
            }
            (agreed if bucket == "review_agreed" else disputed).append(candidate)
    # Refuse a concurrent replacement of the source/result contracts while read.
    # Results must not change during candidate building; only real completeness
    # is required, while any already present synthetic rows remain excluded.
    if evaluation.sha(results) != result_hash:
        raise ValueError("Model results changed during candidate building")
    if evaluation.sha(freeze_path) != freeze_sha256:
        raise ValueError("Independent freeze changed during candidate building")
    for path, expected in pinned_sources.items():
        if evaluation.sha(path) != expected:
            raise ValueError("Frozen source changed during candidate building")
    if evaluation.sha(protocol_path) != protocol_sha256:
        raise ValueError("Model protocol changed during candidate building")
    if evidence_path and evaluation.sha(evidence_path) != protocol["evidence_code_sha256"]:
        raise ValueError("Evidence resolver changed during candidate building")
    if attention_path and evaluation.sha(attention_path) != protocol["attention_code_sha256"]:
        raise ValueError("Attention implementation changed during candidate building")
    audit = {
        "schema_version": "relevance-review-candidates-1", "complete_real_pair_check": True,
        "real_pairs": len(real), "independent_reference_pairs": len(references), "real_result_pairs": len(real_results),
        "synthetic_input_pairs_excluded": len(all_inputs) - len(real),
        "synthetic_result_pairs_ignored": len(model_results) - len(real_results),
        "counts": dict(Counter(row["bucket"] for row in pairs)), "review_agreed_candidates": len(agreed),
        "disputed_candidates": len(disputed), "invalid_model_results": sum(bool(row["validation_errors"]) for row in pairs),
        "selection_procedure": "Real pairs with a validated model effective selection of 채택; split by frozen independent AI expected_selection == 채택",
        "source_hashes": {str(path): digest for path, digest in pinned_sources.items()},
        "independent_freeze": {"path": str(freeze_path), "sha256": freeze_sha256,
                               "human_gold": frozen["human_gold"], "model_outputs_seen": frozen["model_outputs_seen"]},
        "results": {"path": str(results), "sha256": result_hash},
        "model_protocol": {"path": str(protocol_path), "sha256": protocol_sha256,
                           "model_repo": protocol.get("model_repo"), "model_revision": protocol.get("model_revision"),
                           "prompt_sha256": protocol.get("prompt_sha256"), "do_sample": protocol.get("do_sample"),
                           "evidence_mode": protocol.get("evidence_mode", "verbatim"),
                           "evidence_code_sha256": protocol.get("evidence_code_sha256"),
                           "attn_implementation": protocol.get("attn_implementation"),
                           "attention_code_sha256": protocol.get("attention_code_sha256"),
                           "temperature": protocol.get("temperature"), "top_p": protocol.get("top_p"),
                           "top_k": protocol.get("top_k"), "seed_policy": protocol.get("seed_policy")},
        "code_hashes": {Path(path).name: evaluation.sha(path) for path in (__file__, evaluation.__file__, runner.__file__)},
        "evidence_resolver": {"path": str(evidence_path.resolve()), "sha256": protocol["evidence_code_sha256"]} if evidence_path else None,
        "attention_implementation": {"path": str(attention_path), "sha256": protocol["attention_code_sha256"]} if attention_path else None,
        "pairs": pairs, "human_reviewed": False, "human_gold": False, "gnn_labels_generated": False,
        "price_or_return_datasets_opened": False, "gnn_files_modified": False, "service_published": False,
        "ensemble_accuracy": None, "population_accuracy": None, "gnn_performance": None,
        "limitations": [
            "Selection combines text-only model decisions with frozen independent AI reviews; agreement is not ensemble accuracy or human gold.",
            "The builder opens only inputs, references, freeze and inference contracts/results; it does not open prices, returns, GNN labels, or prior evaluation labels.",
            "Independent references attest to full-text review without model outputs; these declarations do not establish independent human review.",
            "Every exported pair is an AI-assisted review candidate with human_reviewed=false; no training, price label creation, or service publication occurs.",
            "Invalid model outputs stay abstained and cannot enter either model-adopted candidate file.",
        ],
    }
    out.mkdir(parents=True, exist_ok=False)
    evaluation.write_jsonl(out / "review_agreed_candidates.jsonl", agreed)
    evaluation.write_jsonl(out / "disputed_candidates.jsonl", disputed)
    audit["files"] = {name: {"sha256": evaluation.sha(out / name), "bytes": (out / name).stat().st_size}
                      for name in ("review_agreed_candidates.jsonl", "disputed_candidates.jsonl")}
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf8")
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--reference", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, help="Default: independent_review_freeze.json beside inputs")
    audit = build(**vars(parser.parse_args()))
    print(json.dumps({key: audit[key] for key in ("real_pairs", "counts", "review_agreed_candidates", "disputed_candidates",
                                                "invalid_model_results")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
