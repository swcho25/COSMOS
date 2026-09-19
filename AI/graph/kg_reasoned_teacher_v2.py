"""Reasoned teacher with a separately versioned evidence-format correction.

The v1 economic criteria, pinned Qwen runtime, complete source input and strict
JSON/source validation remain unchanged. V1 rejected otherwise parsable answers
for citing more than three source segments, or for excluding an article because
the target lacked an economic event without quoting a segment proving absence.
Neither is a necessary format requirement for a developmental economic label.

V2 allows any number of distinct, nonblank, exact catalog IDs. Include still
requires evidence. Exclude/review may cite no segment, but every decision must
provide a nonempty rationale of at most 800 characters. Empty exclude evidence
does NOT make that label ready for graph publication. Quotes and rationales
remain model assertions; this correction does not establish semantic accuracy.

Only an isolated namespace of v1 is patched. Existing v1 files, loaded modules,
protocols, outputs and checkpoints remain immutable. A changed prompt means a
fresh run and direct comparison are required; v1 labels cannot be relabeled by
silently reparsing them as a completed v2 model run.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
VERSION = "kg_reasoned_teacher_development_v2"
SCHEMA = "cosmos-reasoned-teacher-run-2"
_spec = importlib.util.spec_from_file_location(
    "_cosmos_reasoned_teacher_evidence_v2_core", HERE / "kg_reasoned_teacher.py")
_core = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _core
_spec.loader.exec_module(_core)
_original_code_hashes = _core.code_hashes
_original_make_protocol = _core.make_protocol
preview, evidence = _core.preview, _core.evidence
CLASSES = _core.CLASSES
MAX_INPUT_TOKENS, MAX_NEW_TOKENS = _core.MAX_INPUT_TOKENS, _core.MAX_NEW_TOKENS
RAW_KEYS = _core.RAW_KEYS
EVIDENCE_POLICY = {
    "catalog_ids": "distinct_valid_nonblank_exact_source_segments",
    "maximum_ids": "number_of_available_source_segments",
    "include_requires_evidence": True,
    "exclude_and_review_allow_empty_evidence": True,
    "every_decision_requires_nonempty_rationale": True,
    "maximum_rationale_characters": 800,
    "empty_exclude_evidence_is_graph_ready": False,
}
_REPLACEMENTS = (
    ("이를 뒷받침하는 바깥 source_segments의 id를 최대 3개 고르고",
     "이를 뒷받침하는 바깥 source_segments의 id를 고르고(핵심 근거를 우선하되 필요하면 여러 개 가능하며 중복은 금지)"),
    ("economic_path는 800자 이하로 간결하게 쓴다.",
     "economic_path는 모든 판정에서 비어 있지 않은 이유를 800자 이하로 간결하게 쓴다."),
    ("include는 비어 있지 않은 경제 경로와 적어도 하나의 유효한 원문 ID가 필요하다. exclude도 단순 등장 등을 확인할 원문 ID가 적어도 하나 필요하다. review는 빈 ID 목록을 허용한다.",
     "include는 비어 있지 않은 경제 경로와 적어도 하나의 유효한 원문 ID가 필요하다. exclude와 review는 원문 ID가 없어도 된다. 대상 기업의 사건이 없다는 부재 판단을 위해 엉뚱한 ID를 지어내지 말고 빈 목록 []과 구체적인 이유를 써라. ID 개수에 임의의 상한은 없지만 원문 목록에 실제 존재하는 서로 다른 ID만 허용한다."),
)
CRITERIA = _core.CRITERIA
for _before, _after in _REPLACEMENTS:
    if CRITERIA.count(_before) != 1:
        raise ValueError("V1 evidence-format text changed; review the v2 adapter")
    CRITERIA = CRITERIA.replace(_before, _after)


def code_hashes():
    return {**_original_code_hashes(),
            "kg_reasoned_teacher_v2.py": preview.file_sha256(Path(__file__))}


def make_protocol(input_sha256, pairs, alias_resolver=None):
    return {**_original_make_protocol(input_sha256, pairs, alias_resolver),
            "evidence_policy": dict(EVIDENCE_POLICY)}


def parse_response(raw, row, *, catalog=None):
    """No guessed IDs, truncation, partial evidence, or semantic label on errors."""
    try:
        catalog = evidence.build_sentence_catalog(row) if catalog is None else catalog
        evidence.validate_catalog(row, catalog)
    except (ValueError, TypeError, KeyError):
        return None, [], ["invalid_evidence_catalog"]
    try:
        if not isinstance(raw, str):
            raise ValueError("Response must be text")
        value = json.loads(raw, object_pairs_hook=evidence._unique_object,
                           parse_constant=evidence._reject_constant)
    except (ValueError, TypeError, RecursionError):
        return None, [], ["invalid_json"]
    if not isinstance(value, dict) or set(value) != RAW_KEYS:
        return None, [], ["invalid_schema_keys"]
    if list(value) != ["economic_path", "evidence_ids", "decision"]:
        return None, [], ["invalid_schema_order"]
    errors = []
    if not isinstance(value["decision"], str) or value["decision"] not in CLASSES:
        errors.append("invalid_decision")
    path = value["economic_path"]
    if not isinstance(path, str) or len(path) > 800:
        errors.append("invalid_economic_path")
    elif not path.strip():
        errors.append("decision_requires_rationale")
    ids = value["evidence_ids"]
    available = {entry["id"]: entry for entry in catalog}
    if not isinstance(ids, list) or any(type(item) is not int for item in ids):
        errors.append("invalid_evidence_id_types")
    else:
        if len(ids) != len(set(ids)):
            errors.append("duplicate_evidence_ids")
        if any(item not in available for item in ids):
            errors.append("unknown_evidence_id")
        elif any(not available[item]["text"].strip() for item in ids):
            errors.append("blank_evidence_segment")
        if value["decision"] == "include" and not ids:
            errors.append("include_requires_evidence")
    if errors:
        return None, [], errors
    return value, [dict(available[item]) for item in ids], []


_core.VERSION = VERSION
_core.SCHEMA = SCHEMA
_core.CRITERIA = CRITERIA
_core.code_hashes = code_hashes
_core.make_protocol = make_protocol
_core.parse_response = parse_response

now, digest, resolver = _core.now, _core.digest, _core.resolver
name_aliases, alias_provenance, messages = _core.name_aliases, _core.alias_provenance, _core.messages
validate_protocol, validate_result, validate_labels = _core.validate_protocol, _core.validate_result, _core.validate_labels
ReasonedTeacher, run = _core.ReasonedTeacher, _core.run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.inputs, args.out)
