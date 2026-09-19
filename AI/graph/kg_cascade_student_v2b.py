"""Versioned token-metadata repair for the predeclared student B candidate.

The frozen v2 module and its completed A model remain unchanged and loadable.
This adapter executes that exact implementation in a separate module namespace,
changing only B's token-mask construction and the provenance describing it.
Architecture, optimizer, data, labels, epoch budget and model-selection rule are
unchanged. Fixing an execution error does not establish predictive improvement.

The completed A result was already observed before this repair. CAL/TEST may
only be evaluated after the continuation plan and this implementation are fixed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import kg_cascade_pair_encoding_fix as pair_fix

HERE = Path(__file__).resolve().parent
VERSION = "kg-cascade-student-v2-B-pairfix-1"
_spec = importlib.util.spec_from_file_location("_cosmos_student_v2_pairfix_core", HERE / "kg_cascade_student_v2.py")
_core = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _core
_spec.loader.exec_module(_core)
_original_hashes = _core.code_hashes
_original_configuration = _core.configuration


def code_hashes():
    return {**_original_hashes(),
            "kg_cascade_student_v2b.py": _core.preview.file_sha256(Path(__file__)),
            "kg_cascade_pair_encoding_fix.py": _core.preview.file_sha256(HERE / "kg_cascade_pair_encoding_fix.py")}


def configuration(config=None):
    supplied = {"candidate": "B", **(config or {})}
    _core.require(supplied["candidate"] == "B", "The pair-repair adapter supports predeclared B only")
    return _original_configuration(supplied)


def tokenize_document(tokenizer, row, config=None):
    return pair_fix.tokenize_document(tokenizer, row, configuration(config))


def model_version(config):
    configuration(config)
    return VERSION


FEATURE_PROTOCOL = {**_core.FEATURE_PROTOCOL,
                    "target_content_masks": "lossless_validated_target_content_masks_pairfix1",
                    "implementation_repair": "B_pair_sequence_metadata_only_not_new_architecture"}
_core.code_hashes = code_hashes
_core.configuration = configuration
_core.tokenize_document = tokenize_document
_core.model_version = model_version
_core.FEATURE_PROTOCOL = FEATURE_PROTOCOL

CLASSES = _core.CLASSES
PREDECLARED_CANDIDATES = _core.PREDECLARED_CANDIDATES
legacy = _core.legacy
train_model = _core.train_model
StudentPredictor = _core.StudentPredictor
predict = _core.predict
resolve_training_labels = _core.resolve_training_labels


if __name__ == "__main__":
    _core.main()
