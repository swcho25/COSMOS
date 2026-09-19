"""Run compact v3 current-event screening on full local news.

Keeps v1 replay/evidence validation; uses a compact lossless policy and greedy
decoding selected on synthetic development cases. Baseline sampling is reported
as a separate condition, not falsely claimed to be identical. Full source remains.

Only a valid stage-1 adoption invokes stage 2. The second prompt receives the
same full article, not the first answer; both passes still use the same fallible
model, so their agreement is not independent evidence of correctness. All skips,
format failures and overlength articles remain in the result denominator.
No human labels, prices, reference verdicts, training or service writes occur.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time

import local_news_relevance as base
import relevance_sentence_evidence as evidence
import relevance_policy_v3 as policy
import relevance_two_stage_policy as previous_policy

ROOT = Path(__file__).resolve().parent


def code_hashes():
    return {name: base.sha(path) for name, path in {
        "runner": Path(__file__), "base": Path(base.__file__),
        "evidence": Path(evidence.__file__), "attention": ROOT / "local_relevance_attention.py",
        "policy": Path(policy.__file__), "previous_policy": Path(previous_policy.__file__)}.items()}


def requires_verification(stage):
    return not stage["validation_errors"] and stage["prediction"]["decision"] == "adopt"


def validate_stage(stage, row, protocol):
    if stage.get("ignored_output_fields") != policy.ignored_fields(stage.get("raw_output")):
        raise ValueError("Ignored output field audit differs from raw JSON")
    for field in ("input_tokens", "output_tokens", "peak_cuda_allocated_bytes"):
        if type(stage.get(field)) is not int or stage[field] < 0:
            raise ValueError("Invalid integer stage metric: " + field)
    if (type(stage.get("elapsed_seconds")) not in (int, float)
            or not math.isfinite(stage["elapsed_seconds"]) or stage["elapsed_seconds"] < 0):
        raise ValueError("Invalid stage elapsed time")
    if type(stage.get("generation_complete")) is not bool:
        raise ValueError("Invalid completion flag")
    if stage["input_tokens"] > protocol["max_input_tokens"]:
        prediction, errors = None, ["input_too_long_full_article_not_processed"]
        if stage["raw_output"] or stage["output_tokens"] or stage["generation_complete"]:
            raise ValueError("Overlength article produced output")
    else:
        prediction, errors = policy.parse(stage["raw_output"], row)
        if not stage["generation_complete"]:
            errors.append("generation_incomplete")
        if stage["output_tokens"] > protocol["max_new_tokens"]:
            raise ValueError("Output exceeds token budget")
    if stage["prediction"] != prediction or stage["validation_errors"] != errors:
        raise ValueError("Stored stage differs from reproduced validation")


def combined_errors(stages):
    return [name + ":" + error for name, stage in stages.items() if stage is not None
            for error in stage["validation_errors"]]


def final_selection(stages):
    first, second = stages["screen"], stages["verify"]
    return policy.decide(first["prediction"], first["validation_errors"],
                         second["prediction"] if second else None,
                         second["validation_errors"] if second else [])


def validate_result(result, row, protocol):
    if result.get("policy") != "two_stage":
        raise ValueError("Wrong selector policy")
    if base.pair_key(result) != base.pair_key(row) or any(result.get(k) != row.get(k)
            for k in ("case_kind", "source_split")):
        raise ValueError("Result identity or cohort mismatch")
    if result.get("input_sha256") != hashlib.sha256(base.canonical(row).encode()).hexdigest():
        raise ValueError("Result input hash mismatch")
    stages = result.get("stages")
    if not isinstance(stages, dict) or set(stages) != {"screen", "verify"} or not isinstance(stages["screen"], dict):
        raise ValueError("Invalid stage container")
    validate_stage(stages["screen"], row, protocol)
    required = requires_verification(stages["screen"])
    if required != isinstance(stages["verify"], dict) or (not required and stages["verify"] is not None):
        raise ValueError("Missing required verification or unexpected second pass")
    if required:
        validate_stage(stages["verify"], row, protocol)
    if result["effective_selection"] != final_selection(stages) or result["validation_errors"] != combined_errors(stages):
        raise ValueError("Final decision differs from stage decisions")
    for field in ("elapsed_seconds", "input_tokens", "output_tokens"):
        expected = sum(stage[field] for stage in stages.values() if stage is not None)
        if type(result.get(field)) is not type(expected) or result[field] != expected:
            raise ValueError("Combined metric differs: " + field)
    if result["peak_cuda_allocated_bytes"] != max(s["peak_cuda_allocated_bytes"] for s in stages.values() if s):
        raise ValueError("Combined peak memory differs")


def generate_stage(model, tokenizer, torch, row, prompt, stage, args):
    torch.cuda.empty_cache()
    start = time.perf_counter()
    text = tokenizer.apply_chat_template(policy.messages(row, prompt, stage), tokenize=False, add_generation_prompt=True)
    encoded = tokenizer(text, return_tensors="pt", add_special_tokens=False, truncation=False)
    count = encoded["input_ids"].shape[1]
    print(base.canonical({"stage": stage, "input_tokens": count}), flush=True)
    torch.cuda.reset_peak_memory_stats()
    raw, prediction, errors, output_count, complete = "", None, [], 0, False
    if count > args.max_input_tokens:
        errors = ["input_too_long_full_article_not_processed"]
    else:
        encoded = {k: v.to("cuda:0") for k, v in encoded.items()}
        key = base.canonical(list(base.pair_key(row)))
        seed_text = "20260916:" + key if stage == 1 else "20260916:verify:" + key
        torch.manual_seed(int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16))
        with torch.inference_mode():
            generated = model.generate(**encoded, do_sample=False, temperature=None, top_p=None, top_k=None,
                max_new_tokens=args.max_new_tokens, use_cache=True, pad_token_id=tokenizer.eos_token_id)
        output = generated[0, count:]
        output_count = output.numel()
        raw = tokenizer.decode(output, skip_special_tokens=True)
        prediction, errors = policy.parse(raw, row)
        eos = model.generation_config.eos_token_id
        eos = [eos] if isinstance(eos, int) else eos
        complete = bool(output.numel() and output[-1].item() in eos)
        if not complete:
            errors.append("generation_incomplete")
        del generated, output, encoded
    return {"raw_output": raw, "prediction": prediction, "validation_errors": errors,
        "ignored_output_fields": policy.ignored_fields(raw),
        "input_tokens": count, "output_tokens": output_count, "generation_complete": complete,
        "elapsed_seconds": round(time.perf_counter() - start, 4),
        "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated()}


def run(args):
    args.out.mkdir(parents=True, exist_ok=True)
    lock = args.out / "inference.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        _run(args)
    finally:
        lock.unlink()


def _run(args):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HOME"] = str(ROOT / "local_models" / "hf_cache")
    sys.path.insert(0, str(ROOT / "local_models" / "runtime_packages"))
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from local_relevance_attention import register_attention_backend

    if not torch.cuda.is_available():
        raise RuntimeError("Local CUDA GPU required")
    torch.set_num_threads(4)
    attention_name = register_attention_backend()
    rows = base.read_rows(args.inputs)
    for row in rows:
        base.validate_input(row)
    indexed = {base.pair_key(row): row for row in rows}
    if not rows or len(indexed) != len(rows):
        raise ValueError("Empty or duplicate input pairs")
    receipt_path = ROOT / "local_models" / "relevance_model_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf8"))
    model_dir = Path(receipt["directory"]).resolve()
    for item in receipt["files"]:
        path = (model_dir / item["path"]).resolve()
        if not path.is_relative_to(model_dir) or base.sha(path) != item["sha256"]:
            raise ValueError("Pinned model file checksum mismatch")
    prompt_path = ROOT / "news_relevance_prompt.txt"
    prompt = prompt_path.read_text(encoding="utf8")
    protocol = {"schema": "relevance-two-stage-policy-v3-1", "policy": "two_stage",
        "model_repo": receipt["repo"], "model_revision": receipt["revision"],
        "receipt_sha256": base.sha(receipt_path), "prompt_sha256": base.sha(prompt_path),
        "input_sha256": base.sha(args.inputs), "code_hashes": code_hashes(), "rows": len(rows),
        "attn_implementation": attention_name, "quantization": "NF4 double quantization bf16",
        "do_sample": False, "temperature": None, "top_p": None, "top_k": None,
        "seed_policy": "sha256('20260916:' + canonical([article_id,ticker])) first 8 hex",
        "verification_seed_policy": "sha256('20260916:verify:' + canonical([article_id,ticker])) first 8 hex",
        "stage2_receives_stage1_output": False, "retry_policy": "none", "truncation": False,
        "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens,
        "gpu": torch.cuda.get_device_name(0),
        "versions": {n: importlib.metadata.version(n) for n in ("torch", "transformers", "bitsandbytes", "accelerate")}}
    base.pin_protocol(args.out, protocol)
    path = args.out / "results.jsonl"
    prior = base.read_rows(path) if path.exists() else []
    done = {base.pair_key(r) for r in prior}
    if len(done) != len(prior) or not done.issubset(indexed):
        raise ValueError("Invalid resume keys")
    for result in prior:
        validate_result(result, indexed[base.pair_key(result)], protocol)
    todo = [r for r in rows if base.pair_key(r) not in done]
    if todo:
        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
        model = AutoModelForCausalLM.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False,
            device_map={"": 0}, dtype=torch.bfloat16, attn_implementation=attention_name,
            quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16))
        model.eval()
        print(base.canonical({"remaining": len(todo), "model_footprint_bytes": model.get_memory_footprint()}), flush=True)
        with path.open("a", encoding="utf8", newline="\n") as stream:
            for row in todo:
                print(base.canonical({"starting": len(prior) + 1, "total": len(rows)}), flush=True)
                first = generate_stage(model, tokenizer, torch, row, prompt, 1, args)
                second = generate_stage(model, tokenizer, torch, row, prompt, 2, args) if requires_verification(first) else None
                stages = {"screen": first, "verify": second}
                result = {"article_id": row["article_id"], "ticker": row["ticker"], "case_kind": row["case_kind"],
                    "source_split": row.get("source_split"), "policy": "two_stage", "stages": stages,
                    "input_sha256": hashlib.sha256(base.canonical(row).encode()).hexdigest(),
                    "validation_errors": combined_errors(stages), "effective_selection": final_selection(stages),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "peak_cuda_allocated_bytes": max(s["peak_cuda_allocated_bytes"] for s in stages.values() if s)}
                for field in ("elapsed_seconds", "input_tokens", "output_tokens"):
                    result[field] = sum(s[field] for s in stages.values() if s is not None)
                validate_result(result, row, protocol)
                stream.write(base.canonical(result) + "\n"); stream.flush(); os.fsync(stream.fileno())
                prior.append(result)
                print(base.canonical({"completed": len(prior), "total": len(rows),
                    "selection": result["effective_selection"], "errors": result["validation_errors"],
                    "second_pass": second is not None, "seconds": result["elapsed_seconds"]}), flush=True)
    summary = {"complete": len(prior) == len(rows), "pairs": len(prior),
        "counts": dict(Counter(r["effective_selection"] for r in prior)),
        "invalid_pairs": sum(bool(r["validation_errors"]) for r in prior),
        "second_pass_count": sum(r["stages"]["verify"] is not None for r in prior),
        "elapsed_seconds": sum(r["elapsed_seconds"] for r in prior),
        "human_review_completed": 0, "gnn_training_performed": False}
    base.write_json(args.out / "summary.json", summary)
    print(base.canonical(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-input-tokens", type=int, default=6144)
    parser.add_argument("--max-new-tokens", type=int, default=768)
    run(parser.parse_args())
