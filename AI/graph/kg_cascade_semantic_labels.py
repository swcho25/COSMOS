"""Derive decision-only pseudo-labels from immutable, replay-validated Qwen runs.

The original KG policy remains unchanged. Its review outcome mixes semantic
uncertainty, direct/indirect subtype disagreement, and citation/runtime failure.
This versioned target separates those concerns for a NEW student experiment.
Two complete raw passes that agree on relevance can label include/exclude even
when a citation was omitted; that does not create or validate their evidence.
Missing passes and runtime/JSON failures are unusable labels, never negatives.
The sole one-pass semantic label is a valid unclear screen, for which the frozen
teacher deliberately skipped verification. No quote, graph edge, price-effect
label, human gold, or production approval is created here. Qwen can be wrong.

Callers must retain every source pair in accounting, mask unusable training
labels, and route unsupported evaluation/production inputs to the teacher.
This changes the reference task, so agreement is not old-policy parity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import knowledge_graph_batch_inference as batch
import knowledge_graph_news_preview as preview
import knowledge_graph_relevance_policy as policy
import run_knowledge_graph_relevance as teacher

VERSION = "kg_semantic_pseudo_labels_v1"
CLASSES = ("include", "exclude", "review")
MODEL_REPO = "Qwen/Qwen3-4B-Instruct-2507"
MODEL_REVISION = "cdbee75f17c01a7cc42f958dc650907174af0554"
MODEL_RECEIPT_SHA256 = "876024d033031fbd9fe61d0dfb57df022b5b718634ccae58ede4370312175ddf"
CITATION_ONLY_ERRORS = frozenset(("missing_evidence",))


def _digest(value):
    return hashlib.sha256(preview.canonical(value).encode("utf8")).hexdigest()


def validate_protocol(protocol, protocol_sha256=None):
    """Check existing model/prompt/code pins, returning its canonical file hash.

    Worker protocol files are canonical JSON plus a newline. An explicit digest
    must match those bytes; provenance cannot be rebound to arbitrary contents.
    """
    expected = hashlib.sha256((preview.canonical(protocol) + "\n").encode("utf8")).hexdigest()
    if protocol_sha256 is not None and protocol_sha256 != expected:
        raise ValueError("Protocol digest differs from canonical frozen file")
    if (protocol.get("policy_version") != policy.VERSION
            or protocol.get("model_repo") != MODEL_REPO
            or protocol.get("model_revision") != MODEL_REVISION
            or protocol.get("model_receipt_sha256") != MODEL_RECEIPT_SHA256
            or protocol.get("prompt_sha256") != batch.prompt_sha256()
            or protocol.get("max_input_tokens") != batch.MAX_INPUT_TOKENS
            or protocol.get("max_new_tokens") != batch.MAX_NEW_TOKENS):
        raise ValueError("Teacher protocol/model/prompt pin mismatch")
    pins = protocol.get("code_hashes", {})
    if any(pins.get(name) != digest for name, digest in batch.code_hashes().items()):
        raise ValueError("Teacher code pins differ")
    # Worker runs include this additional source file beyond the batch API.
    if "knowledge_graph_reselection_worker.py" in pins:
        path = Path(__file__).with_name("knowledge_graph_reselection_worker.py")
        if pins[path.name] != preview.file_sha256(path):
            raise ValueError("Teacher worker code pin differs")
    return expected


def _stage_relation(stage):
    if stage is None:
        return None, "missing_stage"
    if not stage["generation_complete"]:
        return None, "incomplete_generation"
    if set(stage["errors"]) - CITATION_ONLY_ERRORS:
        return None, "non_citation_execution_or_parse_error"
    # Strict original replay has already checked errors against the raw bytes.
    try:
        raw = json.loads(stage["raw_output"], object_pairs_hook=policy.unique_object,
                         parse_constant=policy.reject_constant)
    except (TypeError, ValueError, RecursionError):
        return None, "invalid_json"
    if (not isinstance(raw, dict) or set(raw) != policy.KEYS
            or not isinstance(raw["relation"], str) or raw["relation"] not in policy.RELATIONS
            or not isinstance(raw["evidence_ids"], list)
            or any(type(x) is not int for x in raw["evidence_ids"])):
        return None, "invalid_semantic_schema"
    return raw["relation"], None


def _derive(row, result, protocol, protocol_sha256):
    teacher.validate_result(result, row, protocol)
    required = {"type": "local_llm_inference", "model_repo": MODEL_REPO,
                "model_revision": MODEL_REVISION, "protocol_sha256": protocol_sha256}
    if result["selection"]["model_provenance"] != required:
        raise ValueError("Teacher result not bound to frozen protocol")
    first, second = result["stages"]["screen"], result["stages"]["verify"]
    a, a_error = _stage_relation(first)
    b, b_error = _stage_relation(second)
    label = None
    if a_error:
        reason = "unusable_screen_" + a_error
    elif a == "unclear":
        label, reason = "review", "semantic_unclear_screen"
    elif b_error:
        reason = "unusable_verify_" + b_error
    elif b == "unclear":
        label, reason = "review", "semantic_unclear_verify"
    elif a in ("direct", "indirect") and b in ("direct", "indirect"):
        label, reason = "include", "two_pass_relevance_agreement"
    elif a == b == "incidental":
        label, reason = "exclude", "two_pass_incidental_agreement"
    else:
        label, reason = "review", "semantic_include_exclude_disagreement"
    citation_errors = {name: list(stage["errors"]) if stage else []
                       for name, stage in (("screen", first), ("verify", second))}
    return {"schema": VERSION, "article_id": row["article_id"], "ticker": row["ticker"],
            "input_sha256": preview.canonical_input_sha256(row),
            "label": label, "usable_for_training": label is not None,
            "source_decision": result["selection"]["decision"], "derivation_reason": reason,
            "stage_relations": {"screen": a, "verify": b}, "stage_errors": citation_errors,
            "evidence_valid": bool(first and second and not first["errors"] and not second["errors"]
                                   and first["prediction"]["evidence"] and second["prediction"]["evidence"]),
            "source_result_sha256": _digest(result), "source_protocol_sha256": protocol_sha256,
            "source_policy_version": policy.VERSION, "evidence": None,
            "human_gold": False, "graph_materialization_ready": False,
            "production_approved": False, "old_policy_parity": False}


def derive_label(row, result, protocol, *, protocol_sha256=None):
    """Replay one source pair and return a decision-only, provenance-bound label."""
    protocol_sha256 = validate_protocol(protocol, protocol_sha256)
    return _derive(row, result, protocol, protocol_sha256)


def derive_labels(rows, raw_results, protocol, *, protocol_sha256=None):
    """Return one record per original pair, including explicit unusable records."""
    rows, raw_results = list(rows), list(raw_results)
    if not rows or len(rows) != len(raw_results):
        raise ValueError("Empty or mismatched teacher label coverage")
    inputs = [preview.pair_key(row) for row in rows]
    indexed = {preview.pair_key(result["selection"]): result for result in raw_results}
    if (len(set(inputs)) != len(inputs) or len(indexed) != len(raw_results)
            or set(inputs) != set(indexed)):
        raise ValueError("Duplicate or mismatched teacher pairs")
    protocol_sha256 = validate_protocol(protocol, protocol_sha256)
    return [_derive(row, indexed[preview.pair_key(row)], protocol, protocol_sha256) for row in rows]


def validate_labels(rows, raw_results, protocol, *, protocol_sha256=None):
    """Return class indices or None; callers MUST mask None, never relabel it."""
    return [CLASSES.index(record["label"]) if record["usable_for_training"] else None
            for record in derive_labels(rows, raw_results, protocol, protocol_sha256=protocol_sha256)]
