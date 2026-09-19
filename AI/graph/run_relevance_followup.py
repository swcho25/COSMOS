"""One frozen follow-up comparison, without modifying the original pilot runner.

baseline uses the exact prior prompt/validator/sampling; grounded asks for source
facts and derives selection with code. Original v4 remains replayable. Inputs are
full articles; references and prices are never loaded here. Each output row and
all runtime code hashes are retained, with separate immutable output directories.
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

ROOT = Path(__file__).resolve().parent


def engine(policy):
    if policy == "baseline":
        return lambda row, prompt: base.messages_for(row, prompt, "sentence_ids"), \
            lambda raw, row: base.validate_prediction(raw, row, "sentence_ids"), None
    if policy == "grounded":
        import relevance_grounded_policy as grounded
        return grounded.messages, grounded.validate_prediction, grounded
    raise ValueError("Unknown policy")


def code_hashes(policy):
    paths = {"runner": Path(__file__), "base": Path(base.__file__),
             "evidence": Path(evidence.__file__), "attention": ROOT / "local_relevance_attention.py"}
    if policy == "grounded":
        paths["policy"] = ROOT / "relevance_grounded_policy.py"
    return {name: base.sha(path) for name, path in paths.items()}


def validate_result(result, row, protocol):
    _, validator, module = engine(protocol["policy"])
    for field in ("input_tokens", "output_tokens", "peak_cuda_allocated_bytes"):
        if type(result.get(field)) is not int or result[field] < 0:
            raise ValueError("Invalid nonnegative integer metric: " + field)
    if (type(result.get("elapsed_seconds")) not in (int, float)
            or not math.isfinite(result["elapsed_seconds"]) or result["elapsed_seconds"] < 0):
        raise ValueError("Invalid elapsed time metric")
    if type(result.get("generation_complete")) is not bool:
        raise ValueError("Invalid generation completion flag")
    if result.get("input_sha256") != hashlib.sha256(base.canonical(row).encode()).hexdigest():
        raise ValueError("Input hash differs")
    if base.pair_key(result) != base.pair_key(row) or any(
        result.get(key) != row.get(key) for key in ("case_kind", "source_split")
    ):
        raise ValueError("Result provenance differs")
    if result["input_tokens"] > protocol["max_input_tokens"]:
        prediction, errors, facts = None, ["input_too_long_full_article_not_processed"], None
        if result["raw_output"] or result["output_tokens"] or result["generation_complete"]:
            raise ValueError("Overlength input unexpectedly generated output")
    else:
        prediction, errors = validator(result["raw_output"], row)
        facts = module.parse_audit(result["raw_output"], row)[0] if module else None
        if not result["generation_complete"]:
            errors.append("generation_incomplete")
        if not 0 <= result["output_tokens"] <= protocol["max_new_tokens"]:
            raise ValueError("Invalid output token count")
    selection = "보류" if errors else prediction["selection"]
    if any(result.get(key) != expected for key, expected in (
        ("prediction", prediction), ("validation_errors", errors),
        ("fact_audit", facts), ("effective_selection", selection), ("policy", protocol["policy"])
    )):
        raise ValueError("Stored result differs from reproduced validation")


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
    torch.manual_seed(20260916)
    attention_name = register_attention_backend()
    rows = base.read_rows(args.inputs)
    for row in rows:
        base.validate_input(row)
    indexed = {base.pair_key(row): row for row in rows}
    if not rows or len(indexed) != len(rows):
        raise ValueError("Empty or duplicate input pairs")
    message_fn, validator, module = engine(args.policy)
    receipt_path = ROOT / "local_models" / "relevance_model_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf8"))
    model_dir = Path(receipt["directory"]).resolve()
    for item in receipt["files"]:
        path = (model_dir / item["path"]).resolve()
        if not path.is_relative_to(model_dir) or base.sha(path) != item["sha256"]:
            raise ValueError("Pinned model file checksum mismatch")
    prompt_path = ROOT / "news_relevance_prompt.txt"
    prompt = prompt_path.read_text(encoding="utf8")
    protocol = {
        "schema": "relevance-followup-1", "policy": args.policy,
        "model_repo": receipt["repo"], "model_revision": receipt["revision"],
        "receipt_sha256": base.sha(receipt_path), "prompt_sha256": base.sha(prompt_path),
        "input_sha256": base.sha(args.inputs), "code_hashes": code_hashes(args.policy),
        "rows": len(rows), "attn_implementation": attention_name,
        "quantization": "NF4 double quantization bf16", "do_sample": True,
        "temperature": 0.7, "top_p": 0.8, "top_k": 20,
        "seed_policy": "sha256('20260916:' + canonical([article_id,ticker])) first 8 hex",
        "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens,
        "truncation": False, "gpu": torch.cuda.get_device_name(0),
        "versions": {name: importlib.metadata.version(name) for name in
                     ("torch", "transformers", "bitsandbytes", "accelerate")},
    }
    base.pin_protocol(args.out, protocol)
    result_path = args.out / "results.jsonl"
    prior = base.read_rows(result_path) if result_path.exists() else []
    done = {base.pair_key(row) for row in prior}
    if len(done) != len(prior) or not done.issubset(indexed):
        raise ValueError("Invalid resume keys")
    for result in prior:
        validate_result(result, indexed[base.pair_key(result)], protocol)
    todo = [row for row in rows if base.pair_key(row) not in done]
    if todo:
        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
        model = AutoModelForCausalLM.from_pretrained(
            str(model_dir), local_files_only=True, trust_remote_code=False, device_map={"": 0},
            dtype=torch.bfloat16, attn_implementation=attention_name,
            quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16))
        model.eval()
        print(base.canonical({"policy": args.policy, "remaining": len(todo),
                              "model_footprint_bytes": model.get_memory_footprint()}), flush=True)
        with result_path.open("a", encoding="utf8", newline="\n") as stream:
            for row in todo:
                torch.cuda.empty_cache()
                start = time.perf_counter()
                text = tokenizer.apply_chat_template(message_fn(row, prompt), tokenize=False, add_generation_prompt=True)
                encoded = tokenizer(text, return_tensors="pt", add_special_tokens=False, truncation=False)
                count = encoded["input_ids"].shape[1]
                print(base.canonical({"starting": len(prior) + 1, "total": len(rows), "tokens": count}), flush=True)
                raw, prediction, errors, facts, output_count, complete = "", None, [], None, 0, False
                torch.cuda.reset_peak_memory_stats()
                if count > args.max_input_tokens:
                    errors = ["input_too_long_full_article_not_processed"]
                else:
                    encoded = {key: value.to("cuda:0") for key, value in encoded.items()}
                    seed = int(hashlib.sha256(("20260916:" + base.canonical(list(base.pair_key(row)))).encode()).hexdigest()[:8], 16)
                    torch.manual_seed(seed)
                    with torch.inference_mode():
                        generated = model.generate(**encoded, do_sample=True, temperature=0.7, top_p=0.8, top_k=20,
                            max_new_tokens=args.max_new_tokens, use_cache=True, pad_token_id=tokenizer.eos_token_id)
                    output = generated[0, count:]
                    output_count = output.numel()
                    raw = tokenizer.decode(output, skip_special_tokens=True)
                    prediction, errors = validator(raw, row)
                    facts = module.parse_audit(raw, row)[0] if module else None
                    eos = model.generation_config.eos_token_id
                    eos = [eos] if isinstance(eos, int) else eos
                    complete = bool(output.numel() and output[-1].item() in eos)
                    if not complete:
                        errors.append("generation_incomplete")
                    del generated, output, encoded
                result = {
                    "article_id": row["article_id"], "ticker": row["ticker"],
                    "case_kind": row["case_kind"], "source_split": row.get("source_split"),
                    "input_sha256": hashlib.sha256(base.canonical(row).encode()).hexdigest(),
                    "policy": args.policy, "prediction": prediction, "fact_audit": facts,
                    "raw_output": raw, "validation_errors": errors,
                    "effective_selection": "보류" if errors else prediction["selection"],
                    "input_tokens": count, "output_tokens": output_count, "generation_complete": complete,
                    "elapsed_seconds": round(time.perf_counter() - start, 4),
                    "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                validate_result(result, row, protocol)
                stream.write(base.canonical(result) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                prior.append(result)
                print(base.canonical({"completed": len(prior), "total": len(rows),
                    "selection": result["effective_selection"], "errors": errors,
                    "seconds": result["elapsed_seconds"]}), flush=True)
    summary = {"complete": len(prior) == len(rows), "pairs": len(prior), "policy": args.policy,
        "counts": {kind: dict(Counter(r["effective_selection"] for r in prior if r["case_kind"] == kind))
                   for kind in ("real", "synthetic")},
        "errors": dict(Counter(e for r in prior for e in r["validation_errors"])),
        "elapsed_seconds": sum(r["elapsed_seconds"] for r in prior),
        "human_review_completed": 0, "gnn_performance": None}
    base.write_json(args.out / "summary.json", summary)
    print(base.canonical(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--policy", choices=("baseline", "grounded"), required=True)
    parser.add_argument("--max-input-tokens", type=int, default=6144)
    parser.add_argument("--max-new-tokens", type=int, default=768)
    run(parser.parse_args())
