"""Full-article B student for a separately versioned, reasoned relevance teacher.

This is a new experiment, not a repair or continuation of the frozen A/B models.
The document encoder, target/content pooling, window attention and classifier
are the existing B architecture. All source tokens are retained by the verified
pair-mask adapter. The default four-epoch run uses encoder LR 1e-5, head LR 3e-4,
head dropout 0.2 and seed 1709. These are preregistered mitigation hypotheses for
the earlier small-data overfitting, not demonstrated improvements.

Labels come only from kg_reasoned_teacher_v3.validate_labels. An unusable teacher
execution returns None and is excluded from loss; semantic uncertainty remains
the explicit review class. Minimum supported DEV loss selects the checkpoint.
The legacy training loop, source checks and inference fallback are unchanged.
No routing thresholds, calibration, test access or production approval occurs
in this module. Agreement with a local LLM does not establish human accuracy.

The immutable v2 implementation is loaded into its own namespace. Original A/B
modules and their checkpoints retain their original settings and provenance.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys

import kg_cascade_pair_encoding_fix as pair_fix
import kg_target_aliases as aliases

HERE = Path(__file__).resolve().parent
VERSION = "kg-reasoned-student-B-v1"
LABEL_POLICY = "kg_reasoned_teacher_development_v3"
_spec = importlib.util.spec_from_file_location(
    "_cosmos_reasoned_student_core", HERE / "kg_cascade_student_v2.py")
_core = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _core
_spec.loader.exec_module(_core)
_original_hashes = _core.code_hashes
_original_configuration = _core.configuration
CLASSES = _core.CLASSES

DEFAULT_CONFIG = {**_core.DEFAULT_CONFIG, "candidate": "B",
                  "label_policy": LABEL_POLICY, "learning_rate": 1e-5,
                  "head_learning_rate": 3e-4, "head_dropout": 0.2,
                  "epochs": 4, "seed": 1709}
PREDECLARED_CANDIDATES = {"B": dict(DEFAULT_CONFIG)}


def _teacher_module():
    teacher = importlib.import_module("kg_reasoned_teacher_v3")
    _core.require(teacher.VERSION == LABEL_POLICY and tuple(teacher.CLASSES) == CLASSES,
                  "Reasoned teacher protocol/classes differ")
    return teacher


def code_hashes():
    """Bind the original engine, token repair and complete new teacher sources."""
    teacher = _teacher_module()
    return {**_original_hashes(), **teacher.code_hashes(),
            "kg_reasoned_student.py": _core.preview.file_sha256(Path(__file__)),
            "kg_reasoned_teacher.py": _core.preview.file_sha256(HERE / "kg_reasoned_teacher.py"),
            "kg_reasoned_teacher_v3.py": _core.preview.file_sha256(HERE / "kg_reasoned_teacher_v3.py"),
            "kg_target_aliases.py": _core.preview.file_sha256(HERE / "kg_target_aliases.py"),
            "../ner/data/aliases.csv": _core.preview.file_sha256(aliases.ALIAS_FILE),
            "kg_cascade_pair_encoding_fix.py": _core.preview.file_sha256(HERE / "kg_cascade_pair_encoding_fix.py")}


def configuration(config=None):
    supplied = {**DEFAULT_CONFIG, **(config or {})}
    _core.require(supplied["candidate"] == "B", "Reasoned student supports B architecture only")
    _core.require(supplied["label_policy"] == LABEL_POLICY, "Reasoned student requires reasoned teacher labels")
    # Delegate all unknown-key, numeric, window and epoch-budget checks to the
    # immutable validator. The substitute name is only an internal compatibility
    # value; the returned/persisted configuration always names the new teacher.
    validated = _original_configuration({**supplied, "label_policy": "kg_semantic_teacher_v1"})
    return {**validated, "label_policy": LABEL_POLICY}


def target_surface(row):
    """Canonical name plus at most three distinct names actually in the source.

    These are lexical spelling hints, not evidence of relevance, a relationship
    or historical identity. Names are not truncated to satisfy a token budget.
    """
    source = _core.preview.validate_input(row)
    matched = aliases.matched_aliases(source)
    _core.require(isinstance(matched, list)
                  and all(isinstance(value, str) and value.strip() for value in matched),
                  "Alias matcher must return nonempty name strings")
    names, seen = [source["company_name"]], {aliases.spelling_key(source["company_name"])}
    for value in matched:
        name_key = aliases.spelling_key(value)
        if name_key not in seen:
            names.append(value)
            seen.add(name_key)
            if len(names) == 4:
                break
    return " | ".join(names)


def tokenize_document(tokenizer, row, config=None):
    config = configuration(config)
    # The frozen pair helper validates its own historical policy whitelist.
    # Label names do not enter encoding; all numerical settings remain identical.
    # Only this copy gets the target-name hint. The original input, its company
    # field and its SHA stored by StudentPredictor remain unchanged.
    hinted = {**row, "company_name": target_surface(row)}
    return pair_fix.tokenize_document(tokenizer, hinted,
        {**config, "label_policy": "kg_semantic_teacher_v1"})


def resolve_training_labels(rows, labels, protocol, config):
    configuration(config)
    values = _teacher_module().validate_labels(rows, labels, protocol)
    _core.require(len(values) == len(rows)
                  and all(value is None or type(value) is int and 0 <= value < len(CLASSES)
                          for value in values), "Invalid reasoned teacher label adapter result")
    return values


def model_version(config):
    configuration(config)
    return VERSION


FEATURE_PROTOCOL = {**_core.FEATURE_PROTOCOL,
    "schema": "kg-reasoned-finetuned-full-window-1",
    "pooling": "B_global_mean_max_target_mean_attention",
    "target_content_masks": "lossless_validated_target_content_masks_pairfix1",
    "label_policy": LABEL_POLICY,
    "target_lexical_hints": {
        "canonical_name_first": True, "maximum_additional_distinct_aliases": 3,
        "match_must_occur_in_article": True, "separator": " | ",
        "body_title_unchanged": True, "input_sha256_uses_original_row": True,
        "target_over_token_limit": "fallback_without_truncation",
        "historical_identity_or_economic_relevance_implied": False,
        "provenance": aliases.provenance()},
    "unusable_teacher_execution": "excluded_from_loss_not_review",
    "review_semantics": "teacher_semantic_uncertainty_not_execution_failure",
    "candidate_hypotheses_not_demonstrated_improvements": True}

_core.code_hashes = code_hashes
_core.configuration = configuration
_core.tokenize_document = tokenize_document
_core.resolve_training_labels = resolve_training_labels
_core.model_version = model_version
_core.FEATURE_PROTOCOL = FEATURE_PROTOCOL
_core.PREDECLARED_CANDIDATES = PREDECLARED_CANDIDATES

legacy = _core.legacy
train_model = _core.train_model
StudentPredictor = _core.StudentPredictor
predict = _core.predict


if __name__ == "__main__":
    _core.main()
