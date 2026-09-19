"""CPU fake-generation checks; no model download/GPU/semantic-accuracy claim."""
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_graph_batch_inference as batch
import knowledge_graph_relevance_policy as policy
import run_knowledge_graph_relevance as serial


def row(index):
    return {"article_id": str(index), "ticker": str(index), "company_name": "Company" + str(index),
            "title": "Company" + str(index) + " reported revenue.", "body": "Full body must survive.",
            "published_at": "2025-04-02", "source_url": None, "company_context": None}


class Tokenizer:
    eos_token_id, eos_token = 9, "<eos>"

    def __init__(self, counts=None):
        self.counts = counts or {}
        self.prompts, self.pad_calls = [], []

    def apply_chat_template(self, messages, **kwargs):
        return json.dumps(messages)

    def __call__(self, prompt, **kwargs):
        assert kwargs == {"add_special_tokens": False, "truncation": False}
        messages = json.loads(prompt)
        payload = json.loads(messages[-1]["content"])
        index = int(payload["company_name"].removeprefix("Company"))
        step = 2 if messages[0]["content"].endswith(policy.STAGES[2]) else 1
        self.prompts.append((index, step, payload))
        count = self.counts.get((index, step), self.counts.get(index, 10 + index * 3))
        return {"input_ids": [7] * (count - 1) + [1000 + step * 100 + index]}

    def pad(self, features, **kwargs):
        assert self.padding_side == "left"
        width = max(len(x["input_ids"]) for x in features)
        ids = [[self.pad_token_id] * (width - len(x["input_ids"])) + x["input_ids"] for x in features]
        masks = [[0] * (width - len(x["input_ids"])) + x["attention_mask"] for x in features]
        self.pad_calls.append((ids, masks))
        return {"input_ids": torch.tensor(ids), "attention_mask": torch.tensor(masks)}

    def decode(self, ids, **kwargs):
        return "".join(chr(x - 100) for x in ids if x not in (9, 10))


class Model:
    generation_config = SimpleNamespace(eos_token_id=[9, 10])

    def __init__(self, answers=None, oom_batch=False, oom_indices=(), error=None):
        self.answers = answers or {}
        self.oom_batch, self.oom_indices, self.error = oom_batch, set(oom_indices), error
        self.calls = []

    def generate(self, input_ids, attention_mask, **kwargs):
        assert kwargs["do_sample"] is False and kwargs["max_new_tokens"] == 192
        assert kwargs["pad_token_id"] == 9
        identities = [divmod(x - 1000, 100) for x in input_ids[:, -1].tolist()]
        self.calls.append(identities)
        if self.error:
            raise RuntimeError(self.error)
        if (self.oom_batch and len(identities) > 1) or any(i in self.oom_indices for _, i in identities):
            raise torch.cuda.OutOfMemoryError("CUDA out of memory")
        outputs = []
        for step, index in identities:
            answer = self.answers.get((index, step), {"relation": "direct", "evidence_ids": [1]})
            complete = True
            if isinstance(answer, tuple):
                answer, complete = answer
            raw = answer if isinstance(answer, str) else json.dumps(answer, separators=(",", ":"))
            tokens = [ord(c) + 100 for c in raw]
            if complete:
                tokens += [9 if index % 2 else 10]
            outputs.append(tokens)
        width = max(map(len, outputs))
        # An incomplete generation is only used as a singleton in these tests.
        continuation = torch.tensor([x + [9] * (width - len(x)) for x in outputs])
        return torch.cat((input_ids, continuation), dim=1)


def classifier(*, counts=None, answers=None, oom_batch=False, oom_indices=(), error=None,
               batch_size=2, token_budget=8192):
    model = Model(answers, oom_batch, oom_indices, error)
    tokenizer = Tokenizer(counts)
    runtime = (model, tokenizer, torch, {"model_repo": "fake-cpu-only", "model_revision": "fake"})
    return batch.LocalBatchClassifier(batch_size, token_budget, runtime=runtime, device="cpu")


class BatchInference(unittest.TestCase):
    def test_left_padding_full_body_per_row_eos_and_original_order(self):
        c = classifier(counts={1: 12, 2: 20}, answers={
            (1, 1): {"relation": "incidental", "evidence_ids": [1]},
            (1, 2): {"relation": "incidental", "evidence_ids": [1]}})
        rows = [row(2), row(1)]
        results = c.classify(rows)
        self.assertEqual([x["selection"]["article_id"] for x in results], ["2", "1"])
        self.assertEqual([x["selection"]["decision"] for x in results], ["include", "exclude"])
        for value, original in zip(results, rows):
            serial.validate_result(value, original, c.protocol)
            for stage in value["stages"].values():
                self.assertTrue(stage["generation_complete"])
                self.assertEqual(stage["output_tokens"], len(stage["raw_output"]) + 1)
                self.assertEqual(stage["input_tokens"], {1: 12, 2: 20}[int(original["ticker"])])
        ids, masks = c.tokenizer.pad_calls[0]
        self.assertEqual(masks[0], [0] * 8 + [1] * 12)
        self.assertEqual(ids[0][:8], [9] * 8)
        for _, _, payload in c.tokenizer.prompts:
            self.assertEqual("".join(x[2] for x in payload["evidence_catalog"] if x[1] == "body"),
                             "Full body must survive.")

    def test_masked_token_budget_and_full_pair_coverage(self):
        items = [{"index": i, "input_tokens": n} for i, n in enumerate((6000, 100, 4000, 150))]
        groups = list(batch.schedule(items, 2, 8192))
        self.assertEqual([[x["index"] for x in group] for group in groups], [[1, 3], [2], [0]])
        self.assertEqual(sorted(x["index"] for group in groups for x in group), list(range(4)))
        for group in groups:
            self.assertLessEqual((max(x["input_tokens"] for x in group) + 192) * len(group), 8192)

    def test_limits_reject_bigger_batches_and_tiny_budget(self):
        for size, budget in ((3, 8192), (True, 8192), (2, 6000)):
            with self.assertRaises(ValueError):
                list(batch.schedule([], size, budget))

    def test_no_truncation_overlength_is_review_without_generation(self):
        c = classifier(counts={1: 6145})
        result = c.classify([row(1)])[0]
        self.assertEqual(result["selection"]["decision"], "review")
        self.assertEqual(result["stages"]["screen"]["errors"], ["input_too_long_full_article_not_processed"])
        self.assertIsNone(result["stages"]["verify"])
        self.assertEqual(c.model.calls, [])

    def test_verify_uses_its_own_full_token_count(self):
        c = classifier(counts={(1, 1): 10, (1, 2): 6145})
        result = c.classify([row(1)])[0]
        self.assertEqual(result["selection"]["decision"], "review")
        self.assertEqual(len(c.model.calls), 1)
        self.assertEqual(result["stages"]["verify"]["input_tokens"], 6145)

    def test_bad_json_unclear_and_incomplete_screens_skip_second(self):
        answers = {(1, 1): "oops", (2, 1): {"relation": "unclear", "evidence_ids": []},
                   (3, 1): ({"relation": "direct", "evidence_ids": [1]}, False)}
        c = classifier(answers=answers, batch_size=1)
        results = c.classify([row(1), row(2), row(3)])
        self.assertTrue(all(x["selection"]["decision"] == "review" for x in results))
        self.assertTrue(all(x["stages"]["verify"] is None for x in results))
        self.assertIn("generation_incomplete", results[2]["stages"]["screen"]["errors"])
        self.assertEqual(c.last_call_summary["verify_stages"], 0)

    def test_only_valid_definitive_screens_reach_second_pass(self):
        c = classifier(answers={(1, 1): "invalid", (2, 2): {"relation": "incidental", "evidence_ids": [1]}})
        results = c.classify([row(1), row(2)])
        self.assertEqual([x["selection"]["decision"] for x in results], ["review", "review"])
        self.assertEqual([i for call in c.model.calls for stage, i in call if stage == 2], [2])
        for _, _, payload in c.tokenizer.prompts:
            self.assertEqual(set(payload), {"published_at", "evidence_catalog", "company_name", "task"})

    def test_oom_batch_splits_without_dropping_or_duplicating_pairs(self):
        c = classifier(oom_batch=True)
        results = c.classify([row(1), row(2)])
        self.assertEqual([x["selection"]["decision"] for x in results], ["include", "include"])
        self.assertEqual(c.last_call_summary["oom_attempts"], 2)
        self.assertEqual(len(c.batch_events), 6)
        self.assertTrue(all(stage["oom_fallback"] for result in results for stage in result["stages"].values()))
        self.assertEqual(c.last_call_summary["batch_attempt_seconds"], sum(x["elapsed_seconds"] for x in c.batch_events))

    def test_singleton_oom_becomes_explicit_review_and_skips_verify(self):
        c = classifier(oom_indices=(1,))
        result = c.classify([row(1)])[0]
        self.assertEqual(result["selection"]["decision"], "review")
        stage = result["stages"]["screen"]
        self.assertEqual(stage["operational_error"], "cuda_out_of_memory")
        self.assertEqual(stage["errors"], ["invalid_json", "generation_incomplete"])
        serial.validate_result(result, row(1), c.protocol)
        self.assertIsNone(result["stages"]["verify"])

    def test_non_oom_errors_fail_instead_of_inventing_a_judgment(self):
        with self.assertRaisesRegex(RuntimeError, "bad kernel"):
            classifier(error="bad kernel").classify([row(1)])

    def test_elapsed_is_counted_once_per_batch_not_once_per_row(self):
        c = classifier()
        results = c.classify([row(1), row(2)])
        attributed = sum(s["elapsed_seconds"] for r in results for s in r["stages"].values())
        self.assertAlmostEqual(attributed, c.last_call_summary["batch_attempt_seconds"])
        self.assertEqual(c.last_call_summary["batch_attempts"], 2)

    def test_eos_first_occurrence_multiple_eos_and_no_eos(self):
        self.assertEqual(batch.stop_at_eos([111, 10, 9, 9], {9, 10}), ([111, 10], True))
        self.assertEqual(batch.stop_at_eos([111, 112], {9, 10}), ([111, 112], False))

    def test_duplicate_input_and_non_null_context_rejected_before_inference(self):
        c = classifier()
        with self.assertRaises(ValueError):
            c.classify([row(1), row(1)])
        value = row(1)
        value["company_context"] = {"unverified": True}
        with self.assertRaises(ValueError):
            c.classify([value])
        self.assertEqual(c.model.calls, [])

    def test_provenance_mismatch_rejected_before_inference(self):
        c = classifier()
        with self.assertRaises(ValueError):
            c.classify([row(1)], {"type": "local_llm_inference", "model_repo": "other",
                "model_revision": "fake", "protocol_sha256": "a" * 64})
        self.assertEqual(c.model.calls, [])

    def test_portable_model_directory_preserves_receipt_and_checks_every_file(self):
        artifact_root = (batch.HERE / "artifacts").resolve()
        directory = artifact_root / ("kg_portable_test_" + uuid.uuid4().hex)
        if not directory.resolve().is_relative_to(artifact_root):
            raise ValueError("Test cleanup outside artifacts")
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        weights = directory / "weights.bin"
        weights.write_bytes(b"synthetic CPU fixture, not model weights")
        receipt = {"directory": "Z:/original-computer/model", "files": [
            {"path": "weights.bin", "sha256": batch.base.sha(weights)}]}
        original = batch.base.canonical(receipt)
        with mock.patch.dict(os.environ, {"COSMOS_RELEVANCE_MODEL_DIR": str(directory)}):
            self.assertEqual(batch.pinned_model_directory(receipt), directory.resolve())
            self.assertEqual(batch.base.canonical(receipt), original)
            weights.write_bytes(b"wrong model")
            with self.assertRaisesRegex(ValueError, "Pinned model bytes"):
                batch.pinned_model_directory(receipt)
            receipt["files"][0]["path"] = "../outside.bin"
            with self.assertRaisesRegex(ValueError, "Pinned model bytes"):
                batch.pinned_model_directory(receipt)


if __name__ == "__main__":
    unittest.main()
