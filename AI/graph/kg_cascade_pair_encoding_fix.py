"""Recover exact pair masks without changing any frozen student token IDs.

The pinned tokenizer can merge two independently encoded sequence-zero
Encodings into overlapping ``sequence_ranges`` during ``post_process``. Both
range keys then point to the content sequence, and ``sequence_ids`` loses the
target (even for one window). Calling ``set_sequence_id`` does not clear the
old range key, so it does not reliably fix this collision.

This bounded repair uses the post-processor's insertion mask instead. It first
proves that all non-inserted token IDs, in order, exactly equal target IDs plus
the current content window IDs. Only then are those positions divided at the
known target length. Literal special-token spellings in the original input are
retained: positions are classified by insertion metadata, never by token value.
Unexpected ordering or inconsistent metadata raises instead of guessing.

Tokenization, window boundaries, input IDs, type IDs, attention masks, weights,
and unsupported-document decisions remain the immutable v1 behavior. The v2
model architecture, optimization, labels, and routing criteria are unchanged.
This module is an explicit new adapter, not a mutation of a frozen experiment.
"""
from __future__ import annotations

import kg_cascade_student as v1
import kg_cascade_student_v2 as v2

VERSION = "kg_pair_masks_verified_insertions_v1"


def verified_pair_masks(pair, target_ids, content_ids, *, expected_special_tokens=None):
    """Return exact input-sequence masks after proving complete ordered coverage."""
    ids = list(pair.ids)
    special = list(pair.special_tokens_mask)
    target_ids, content_ids = list(target_ids), list(content_ids)
    v2.require(target_ids and content_ids, "Pair masks require nonempty target and content")
    v2.require(len(ids) == len(special) and all(type(flag) is int and flag in (0, 1) for flag in special),
               "Invalid pair insertion mask")
    if expected_special_tokens is not None:
        v2.require(type(expected_special_tokens) is int and sum(special) == expected_special_tokens,
                   "Pair insertion count differs from tokenizer contract")
    positions = [index for index, flag in enumerate(special) if flag == 0]
    v2.require([ids[index] for index in positions] == target_ids + content_ids,
               "Pair insertion metadata does not preserve ordered target/content tokens")
    target_mask, content_mask = [0] * len(ids), [0] * len(ids)
    for index in positions[:len(target_ids)]:
        target_mask[index] = 1
    for index in positions[len(target_ids):]:
        content_mask[index] = 1
    v2.require(sum(target_mask) == len(target_ids) and sum(content_mask) == len(content_ids),
               "Pair mask coverage is incomplete")
    return target_mask, content_mask


def tokenize_document(tokenizer, row, config=None):
    """Use frozen v1 windows unchanged and add verified target/content masks for B."""
    config = v2.configuration(config)
    base_config = {key: value for key, value in config.items() if key not in ("candidate", "label_policy")}
    windows, coverage = v1.tokenize_document(tokenizer, row, base_config)
    if not windows or config["candidate"] == "A":
        return windows, coverage
    source = v2.preview.validate_input(row)
    backend = tokenizer.backend_tokenizer
    target = backend.encode(source["company_name"], add_special_tokens=False)
    content = backend.encode(source["title"] + "\n" + source["body"], add_special_tokens=False)
    inserted = tokenizer.num_special_tokens_to_add(pair=True)
    width = config["max_tokens"] - len(target.ids) - inserted
    original = list(content.ids)
    content.truncate(width, stride=0)
    pieces = [content, *content.overflowing]
    v2.require([token for piece in pieces for token in piece.ids] == original,
               "Pair mask repair lost or repeated content tokens")
    v2.require(len(pieces) == len(windows), "Pair mask repair changed window count")
    masked = []
    for (features, weight), piece in zip(windows, pieces):
        pair = backend.post_process(target, piece, add_special_tokens=True)
        v2.require(pair.ids == features["input_ids"] and pair.type_ids == features["token_type_ids"]
                   and len(piece.ids) == weight,
                   "Pair mask repair differs from frozen tokenization")
        target_mask, content_mask = verified_pair_masks(
            pair, target.ids, piece.ids, expected_special_tokens=inserted)
        masked.append(({**features, "target_mask": target_mask, "content_mask": content_mask}, weight))
    return masked, coverage
