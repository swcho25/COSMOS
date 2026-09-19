"""Bounded local batching of the unchanged economic-relevance policy.

Load the receipt-pinned Qwen once; screen all pairs, then verify only definitive
valid screens. Full source prompts are never truncated. Left padding is masked,
and each row stops at its own first EOS (batch padding is not generated text).
An RTX 4050 6 GB starts at at most two rows and 8,192 padded input+output tokens;
OOM attempts split to singles. A singleton OOM is an explicit review result.

Batching may change floating-point results. A known-input benchmark compares
decisions with an earlier run, not accuracy. No training, HDFS, or service writes.
The caller owns streaming, resume, and publication; classify() is bounded-memory.
CPU tests inject a fake runtime and do not claim semantic model performance.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

import knowledge_graph_news_preview as preview
import knowledge_graph_relevance_policy as policy
import local_news_relevance as base
import run_knowledge_graph_relevance as serial

HERE = Path(__file__).resolve().parent
MAX_INPUT_TOKENS = 6144
MAX_NEW_TOKENS = 192


def code_hashes():
    return {**serial.code_hashes(), __file__.split(os.sep)[-1]: base.sha(Path(__file__))}


def prompt_sha256():
    return hashlib.sha256(base.canonical({"criteria": policy.CRITERIA,
        "examples": policy.EXAMPLES, "stages": policy.STAGES}).encode("utf8")).hexdigest()


def schedule(items, batch_size, token_budget):
    """Stable length buckets; budget includes padding and reserved output tokens."""
    if type(batch_size) is not int or batch_size not in (1, 2):
        raise ValueError("This 6 GB policy permits batch size 1 or 2 only")
    if type(token_budget) is not int or token_budget < MAX_INPUT_TOKENS + MAX_NEW_TOKENS:
        raise ValueError("Token budget must accommodate one full permitted prompt")
    current = []
    for item in sorted(items, key=lambda x: (x["input_tokens"], x["index"])):
        if not 0 < item["input_tokens"] <= MAX_INPUT_TOKENS:
            raise ValueError("Scheduler expects valid full-prompt token counts")
        padded = (item["input_tokens"] + MAX_NEW_TOKENS) * (len(current) + 1)
        if current and (len(current) == batch_size or padded > token_budget):
            yield current
            current = []
        current.append(item)
    if current:
        yield current


def stop_at_eos(tokens, eos_ids):
    """Return real row tokens including EOS; discard later batch padding only."""
    for index, token in enumerate(tokens):
        if token in eos_ids:
            return tokens[:index + 1], True
    return tokens, False


def pinned_model_directory(receipt):
    """Allow a different local directory while preserving every receipt digest."""
    override = os.environ.get("COSMOS_RELEVANCE_MODEL_DIR")
    model_dir = Path(override or receipt["directory"]).expanduser().resolve()
    for item in receipt["files"]:
        path = (model_dir / item["path"]).resolve()
        if not path.is_relative_to(model_dir) or base.sha(path) != item["sha256"]:
            raise ValueError("Pinned model bytes differ")
    return model_dir


def _load_runtime():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HOME"] = str(HERE / "local_models/hf_cache")
    sys.path.insert(0, str(HERE / "local_models/runtime_packages"))
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from local_relevance_attention import register_attention_backend
    if not torch.cuda.is_available():
        raise RuntimeError("Local CUDA required")
    torch.set_num_threads(4)
    receipt_path = HERE / "local_models/relevance_model_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf8"))
    # A second PC keeps the original receipt bytes/provenance. Only its local
    # directory can differ; every referenced file still has to match the receipt.
    override = os.environ.get("COSMOS_RELEVANCE_MODEL_DIR")
    model_dir = pinned_model_directory(receipt)
    attention = register_attention_backend()
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True,
                                              trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(str(model_dir), local_files_only=True,
        trust_remote_code=False, device_map={"": 0}, dtype=torch.bfloat16,
        attn_implementation=attention,
        quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)).eval()
    metadata = {"model_repo": receipt["repo"], "model_revision": receipt["revision"],
        "model_receipt_sha256": base.sha(receipt_path), "attn_implementation": attention,
        "resolved_model_directory": str(model_dir),
        "model_directory_source": "COSMOS_RELEVANCE_MODEL_DIR" if override else "receipt",
        "quantization": "NF4 double quantization bf16", "gpu": torch.cuda.get_device_name(0),
        "versions": {name: importlib.metadata.version(name) for name in
                     ("torch", "transformers", "bitsandbytes", "accelerate")}}
    return model, tokenizer, torch, metadata


class LocalBatchClassifier:
    """One loaded runtime. classify returns original-order serial-schema results.

    Optional runtime is (model, tokenizer, torch_module, metadata), for CPU tests.
    Production callers omit it. Their saved protocol should incorporate .protocol
    and pass its actual file digest via provenance['protocol_sha256'].
    """

    def __init__(self, batch_size=2, max_batch_tokens=8192, *, runtime=None, device="cuda:0"):
        list(schedule([], batch_size, max_batch_tokens))
        self.batch_size, self.max_batch_tokens = batch_size, max_batch_tokens
        self.model, self.tokenizer, self.torch, metadata = runtime or _load_runtime()
        self.device = device
        self.tokenizer.padding_side = "left"
        if self.tokenizer.eos_token_id is None:
            raise ValueError("Tokenizer EOS required for padding")
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
        eos = self.model.generation_config.eos_token_id
        eos = eos if isinstance(eos, (list, tuple)) else [eos]
        if not eos or any(type(x) is not int for x in eos):
            raise ValueError("Pinned model must specify generation EOS tokens")
        self.eos_ids = set(eos)
        self.protocol = {"schema": "cosmos-kg-batched-run-1", **metadata,
            "policy_version": policy.VERSION, "code_hashes": code_hashes(),
            "prompt_sha256": prompt_sha256(), "max_input_tokens": MAX_INPUT_TOKENS,
            "max_new_tokens": MAX_NEW_TOKENS, "batch_size": batch_size,
            "max_batch_tokens": max_batch_tokens, "padding_side": "left",
            "token_budget_includes": "batch_size * (padded_input_width + max_new_tokens)",
            "do_sample": False, "truncation": False, "stage2_receives_stage1_output": False,
            "retry_policy": "CUDA OOM splits batch to single; single OOM becomes review",
            "stage_elapsed_accounting": "successful batch elapsed divided equally across its rows",
            "total_inference_accounting": "sum of unique batch attempt elapsed, including OOM attempts",
            "weight_training_performed": False, "human_gold": False}
        self.batch_events = []
        self.last_call_summary = None

    def _stage(self, item, raw="", tokens=0, complete=False, elapsed=0., peak=0, **extras):
        if item["input_tokens"] > MAX_INPUT_TOKENS:
            prediction, errors = None, ["input_too_long_full_article_not_processed"]
        else:
            prediction, errors = policy.parse(raw, item["row"])
            if not complete:
                errors.append("generation_incomplete")
        stage = {"raw_output": raw, "prediction": prediction, "errors": errors,
            "input_tokens": item["input_tokens"], "output_tokens": tokens,
            "generation_complete": complete, "elapsed_seconds": elapsed,
            "peak_cuda_allocated_bytes": peak, "prompt_sha256": item["prompt_sha256"], **extras}
        serial.validate_stage(stage, item["row"], self.protocol)
        return stage

    def _prepare(self, rows, step):
        prepared = []
        for index, row in rows:
            prompt = self.tokenizer.apply_chat_template(policy.messages(row, step),
                tokenize=False, add_generation_prompt=True)
            encoded = self.tokenizer(prompt, add_special_tokens=False, truncation=False)
            ids = encoded["input_ids"]
            if not isinstance(ids, list) or not ids or any(type(x) is not int for x in ids):
                raise ValueError("Tokenizer must return a nonempty unbatched token list")
            prepared.append({"index": index, "row": row, "step": step,
                "input_tokens": len(ids), "features": {"input_ids": ids, "attention_mask": [1] * len(ids)},
                "prompt_sha256": hashlib.sha256(prompt.encode("utf8")).hexdigest()})
        return prepared

    def _cuda(self, operation):
        if self.device.startswith("cuda"):
            return getattr(self.torch.cuda, operation)()
        return 0

    def _attempt(self, batch):
        inputs = output = None
        self._cuda("synchronize")
        self._cuda("reset_peak_memory_stats")
        started = time.perf_counter()
        try:
            inputs = self.tokenizer.pad([item["features"] for item in batch],
                padding=True, return_tensors="pt")
            width = inputs["input_ids"].shape[1]
            masks = inputs["attention_mask"].tolist()
            for item, mask in zip(batch, masks):
                if mask != [0] * (width - item["input_tokens"]) + [1] * item["input_tokens"]:
                    raise ValueError("Tokenizer padding/mask differs from left-padding contract")
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with self.torch.inference_mode():
                output = self.model.generate(**inputs, do_sample=False, temperature=None,
                    top_p=None, top_k=None, max_new_tokens=MAX_NEW_TOKENS, use_cache=True,
                    pad_token_id=self.tokenizer.eos_token_id)
            if output.shape[0] != len(batch) or output.shape[1] < width:
                raise ValueError("Model output batch shape differs")
            generated = output[:, width:].tolist()
            self._cuda("synchronize")
            elapsed = time.perf_counter() - started
            peak = self._cuda("max_memory_allocated")
            return generated, elapsed, peak
        finally:
            del inputs, output

    def _generate(self, batch, *, fallback=False):
        started = time.perf_counter()
        event = {"batch_id": len(self.batch_events), "step": batch[0]["step"],
            "pair_indices": [item["index"] for item in batch], "size": len(batch),
            "padded_input_tokens": max(x["input_tokens"] for x in batch),
            "oom_fallback": fallback}
        try:
            generated, elapsed, peak = self._attempt(batch)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower() and not isinstance(exc,
                    getattr(self.torch.cuda, "OutOfMemoryError", ())):
                raise
            # Leave the exception scope before retrying so the traceback cannot
            # retain failed GPU allocations. Preserve the operational failure.
            failed = True
            event.update(status="cuda_out_of_memory", elapsed_seconds=time.perf_counter() - started)
        else:
            failed = False
            event.update(status="ok", elapsed_seconds=elapsed, peak_cuda_allocated_bytes=peak)
        self.batch_events.append(event)
        if failed:
            self._cuda("empty_cache")
            if len(batch) > 1:
                result = {}
                for item in batch:
                    result.update(self._generate([item], fallback=True))
                return result
            item = batch[0]
            return {item["index"]: self._stage(item, batch_id=event["batch_id"],
                operational_error="cuda_out_of_memory", oom_fallback=fallback)}
        results = {}
        for item, raw_ids in zip(batch, generated):
            ids, complete = stop_at_eos(raw_ids, self.eos_ids)
            raw = self.tokenizer.decode(ids, skip_special_tokens=True)
            results[item["index"]] = self._stage(item, raw, len(ids), complete,
                elapsed / len(batch), peak, batch_id=event["batch_id"], oom_fallback=fallback)
        return results

    def _pass(self, rows, step):
        prepared = self._prepare(rows, step)
        result = {item["index"]: self._stage(item) for item in prepared
                  if item["input_tokens"] > MAX_INPUT_TOKENS}
        eligible = [item for item in prepared if item["input_tokens"] <= MAX_INPUT_TOKENS]
        for batch in schedule(eligible, self.batch_size, self.max_batch_tokens):
            result.update(self._generate(batch))
        return result

    def classify(self, rows, provenance=None):
        started, first_event = time.perf_counter(), len(self.batch_events)
        rows = list(rows)
        if not rows or len({preview.pair_key(row) for row in rows}) != len(rows):
            raise ValueError("Expected nonempty unique article/company pairs")
        for row in rows:
            preview.validate_input(row)  # Full-history compatible: no training-year restriction.
            policy.messages(row, 1)
        if self.protocol["code_hashes"] != code_hashes() or self.protocol["prompt_sha256"] != prompt_sha256():
            raise ValueError("Inference code or policy changed after runtime load")
        provenance = provenance or {"type": "local_llm_inference",
            "model_repo": self.protocol["model_repo"], "model_revision": self.protocol["model_revision"],
            "protocol_sha256": hashlib.sha256(base.canonical(self.protocol).encode("utf8")).hexdigest(),
            "protocol_digest_encoding": "canonical_json_utf8"}
        if not isinstance(provenance.get("protocol_sha256"), str) or len(provenance["protocol_sha256"]) != 64:
            raise ValueError("Protocol SHA-256 provenance required")
        if provenance.get("type") != "local_llm_inference" or any(
                provenance.get(key) != self.protocol[key] for key in ("model_repo", "model_revision")):
            raise ValueError("Model provenance differs from loaded runtime")
        first = self._pass(list(enumerate(rows)), 1)
        second = self._pass([(i, row) for i, row in enumerate(rows) if serial.needs_second(first[i])], 2)
        results = []
        for index, row in enumerate(rows):
            stages = {"screen": first[index], "verify": second.get(index)}
            decision, relation, evidence, reason = policy.combine(stages["screen"], stages["verify"])
            selection = {"article_id": row["article_id"], "ticker": row["ticker"],
                "input_sha256": preview.canonical_input_sha256(row), "decision": decision,
                "relation": relation, "evidence": evidence, "reason": reason,
                "policy_version": policy.VERSION, "model_provenance": dict(provenance)}
            result = {"selection": selection, "stages": stages,
                      "finished_at": datetime.now(timezone.utc).isoformat()}
            serial.validate_result(result, row, self.protocol)
            results.append(result)
        if self.protocol["code_hashes"] != code_hashes():
            raise ValueError("Inference code changed while classifying")
        events = self.batch_events[first_event:]
        self.last_call_summary = {"pairs": len(rows), "wall_seconds": time.perf_counter() - started,
            "batch_attempt_seconds": sum(x["elapsed_seconds"] for x in events),
            "batch_attempts": len(events), "oom_attempts": sum(x["status"] != "ok" for x in events),
            "screen_stages": len(first), "verify_stages": len(second),
            "counts": dict(Counter(x["selection"]["decision"] for x in results))}
        return results


def compare_previous(rows, results, previous_path):
    previous = preview.read_jsonl(previous_path)
    prior_protocol_path = previous_path.parent / "protocol.json"
    prior_protocol = json.loads(prior_protocol_path.read_text(encoding="utf8"))
    if prior_protocol.get("schema") != "cosmos-kg-relevance-run-1":
        raise ValueError("Benchmark baseline must be the original serial protocol")
    if prior_protocol.get("code_hashes") != serial.code_hashes():
        raise ValueError("Benchmark baseline policy/execution code changed")
    indexed = {preview.pair_key(x["selection"]): x for x in previous}
    if len(indexed) != len(previous) or set(indexed) != {preview.pair_key(row) for row in rows}:
        raise ValueError("Previous benchmark must cover exactly the same pairs")
    changed = []
    for row, result in zip(rows, results):
        old_result = indexed[preview.pair_key(row)]
        serial.validate_result(old_result, row, prior_protocol)
        old = old_result["selection"]
        preview.validate_selection(old, row)
        if old["model_provenance"].get("protocol_sha256") != base.sha(prior_protocol_path):
            raise ValueError("Previous result protocol provenance differs")
        for key in ("policy_version",):
            if old[key] != result["selection"][key]:
                raise ValueError("Previous benchmark policy differs")
        for key in ("model_repo", "model_revision"):
            if old["model_provenance"].get(key) != result["selection"]["model_provenance"].get(key):
                raise ValueError("Previous benchmark model differs")
        new = result["selection"]
        if (old["decision"], old["relation"]) != (new["decision"], new["relation"]):
            changed.append({"article_id": row["article_id"], "ticker": row["ticker"],
                "old": {k: old[k] for k in ("decision", "relation")},
                "new": {k: new[k] for k in ("decision", "relation")}})
    return {"interpretation": "same known inputs, execution/decision comparison; not held-out accuracy",
            "pairs": len(rows), "changed_decision_or_relation": len(changed), "changes": changed,
            "previous_serial_stage_seconds": sum(stage["elapsed_seconds"] for result in previous
                for stage in result["stages"].values() if stage is not None),
            "previous_results_sha256": base.sha(previous_path)}


def run(args):
    started = time.perf_counter()
    if args.out.exists():
        raise ValueError("Use a new immutable output directory")
    source_sha = base.sha(args.inputs)
    rows = preview.read_jsonl(args.inputs)
    if base.sha(args.inputs) != source_sha:
        raise ValueError("Input changed while reading")
    classifier = LocalBatchClassifier(args.batch_size, args.max_batch_tokens)
    protocol = {**classifier.protocol, "input_sha256": source_sha, "pairs": len(rows),
                "purpose": "local batch execution benchmark; no accuracy claim"}
    args.out.mkdir(parents=True, exist_ok=False)
    base.write_json(args.out / "protocol.json", protocol)
    provenance = {"type": "local_llm_inference", "model_repo": protocol["model_repo"],
        "model_revision": protocol["model_revision"], "protocol_sha256": base.sha(args.out / "protocol.json")}
    results = classifier.classify(rows, provenance)
    if base.sha(args.inputs) != source_sha:
        raise ValueError("Input changed while classifying")
    for filename, payloads in (("results.jsonl", results), ("selections.jsonl", [x["selection"] for x in results]),
                               ("batch_events.jsonl", classifier.batch_events)):
        with (args.out / filename).open("x", encoding="utf8", newline="\n") as stream:
            for payload in payloads:
                stream.write(base.canonical(payload) + "\n")
    summary = {**classifier.last_call_summary, "complete": True,
        "total_wall_seconds_including_model_load": time.perf_counter() - started,
        "weight_training_performed": False, "human_gold": False, "service_published": False,
        "files": {name: base.sha(args.out / name) for name in
                  ("protocol.json", "results.jsonl", "selections.jsonl", "batch_events.jsonl")}}
    if args.previous_results:
        comparison = compare_previous(rows, results, args.previous_results)
        comparison["batch_attempt_seconds"] = classifier.last_call_summary["batch_attempt_seconds"]
        comparison["serial_stage_seconds_over_batch_attempt_seconds"] = (
            comparison["previous_serial_stage_seconds"] / comparison["batch_attempt_seconds"]
            if comparison["batch_attempt_seconds"] else None)
        comparison["speed_caveat"] = "Stages may differ if predictions differ; model loading excluded in both quantities."
        base.write_json(args.out / "comparison.json", comparison)
        summary["files"]["comparison.json"] = base.sha(args.out / "comparison.json")
    base.write_json(args.out / "summary.json", summary)
    print(base.canonical(summary), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, choices=(1, 2), default=2)
    parser.add_argument("--max-batch-tokens", type=int, default=8192)
    parser.add_argument("--previous-results", type=Path)
    run(parser.parse_args())
