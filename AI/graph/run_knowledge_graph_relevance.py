"""Run the existing pinned local Qwen as a knowledge-graph relevance classifier.

No weight training: this changes the classification task/prompt, not the model.
Every original pair remains in the denominator. Full text, exact evidence IDs,
two answer-blind passes, raw output and immutable per-run provenance are saved.
Malformed/incomplete/overlong outputs abstain. Successful outputs are tentative
graph links, not verified facts, price labels or human gold. This program writes
only a new local artifact directory; production and HDFS are never touched.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

import local_news_relevance as base
import knowledge_graph_relevance_policy as policy
import knowledge_graph_news_preview as preview

HERE = Path(__file__).resolve().parent


def code_hashes():
    return {name: base.sha(HERE / name) for name in (
        "run_knowledge_graph_relevance.py", "knowledge_graph_relevance_policy.py",
        "knowledge_graph_news_preview.py", "relevance_sentence_evidence.py",
        "local_news_relevance.py", "local_relevance_attention.py")}


def needs_second(stage):
    return not stage["errors"] and stage["prediction"]["relation"] != "unclear"


def validate_stage(stage, row, protocol):
    count = stage["input_tokens"]
    if type(count) is not int or count < 1 or type(stage["output_tokens"]) is not int or stage["output_tokens"] < 0:
        raise ValueError("Invalid stage token counts")
    if type(stage["generation_complete"]) is not bool:
        raise ValueError("Invalid generation completion")
    if count > protocol["max_input_tokens"]:
        predicted, errors = None, ["input_too_long_full_article_not_processed"]
        if stage["raw_output"] or stage["output_tokens"] or stage["generation_complete"]:
            raise ValueError("Overlength source produced output")
    else:
        predicted, errors = policy.parse(stage["raw_output"], row)
        if not stage["generation_complete"]:
            errors.append("generation_incomplete")
    if stage["output_tokens"] > protocol["max_new_tokens"] or predicted != stage["prediction"] or errors != stage["errors"]:
        raise ValueError("Stored output differs from raw parse")


def validate_result(result, row, protocol):
    preview.validate_selection(result["selection"], row)
    selected = result["selection"]
    if selected["policy_version"] != protocol["policy_version"] or protocol["policy_version"] != policy.VERSION:
        raise ValueError("Selection policy differs from run")
    provenance = selected["model_provenance"]
    if (provenance.get("type") != "local_llm_inference" or
            any(provenance.get(key) != protocol.get(key) for key in ("model_repo", "model_revision"))):
        raise ValueError("Selection model provenance differs from run")
    stages = result["stages"]
    if set(stages) != {"screen", "verify"}:
        raise ValueError("Invalid stages")
    validate_stage(stages["screen"], row, protocol)
    if needs_second(stages["screen"]) != (stages["verify"] is not None):
        raise ValueError("Second-pass presence differs from policy")
    if stages["verify"] is not None:
        validate_stage(stages["verify"], row, protocol)
    decision, relation, evidence, reason = policy.combine(stages["screen"], stages["verify"])
    if any(selected[key] != value for key, value in zip(("decision", "relation", "evidence", "reason"),
                                                     (decision, relation, evidence, reason))):
        raise ValueError("Selection differs from source stages")


def generate(model, tokenizer, torch, row, stage, args):
    torch.cuda.empty_cache()
    started = time.perf_counter()
    prompt = tokenizer.apply_chat_template(policy.messages(row, stage), tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt", add_special_tokens=False, truncation=False)
    count = inputs["input_ids"].shape[1]
    raw, prediction, errors, produced, complete = "", None, [], 0, False
    torch.cuda.reset_peak_memory_stats()
    if count > args.max_input_tokens:
        errors = ["input_too_long_full_article_not_processed"]
    else:
        inputs = {key: value.to("cuda:0") for key, value in inputs.items()}
        with torch.inference_mode():
            output = model.generate(**inputs, do_sample=False, temperature=None, top_p=None, top_k=None,
                max_new_tokens=args.max_new_tokens, use_cache=True, pad_token_id=tokenizer.eos_token_id)
        generated = output[0, count:]
        produced = generated.numel()
        raw = tokenizer.decode(generated, skip_special_tokens=True)
        eos = model.generation_config.eos_token_id
        eos = [eos] if isinstance(eos, int) else eos
        complete = bool(produced and generated[-1].item() in eos)
        prediction, errors = policy.parse(raw, row)
        if not complete:
            errors.append("generation_incomplete")
        del output, generated, inputs
    return {"raw_output": raw, "prediction": prediction, "errors": errors,
            "input_tokens": count, "output_tokens": produced, "generation_complete": complete,
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated()}


def run(args):
    started = time.perf_counter()
    if args.out.exists():
        raise ValueError("Use a new output directory; existing runs are immutable")
    if args.max_input_tokens < 1 or args.max_new_tokens < 1:
        raise ValueError("Token limits must be positive")
    input_sha256 = base.sha(args.inputs)
    rows = preview.read_jsonl(args.inputs)
    if base.sha(args.inputs) != input_sha256:
        raise ValueError("Input changed while reading")
    keys = [base.pair_key(row) for row in rows]
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("Empty or duplicate pairs")
    for row in rows:
        base.validate_input(row)  # Retain this pilot's train-only input boundary.
        preview.canonical_input_sha256(row)
        policy.messages(row, 1)
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
    model_dir = Path(receipt["directory"]).resolve()
    for item in receipt["files"]:
        path = (model_dir / item["path"]).resolve()
        if not path.is_relative_to(model_dir) or base.sha(path) != item["sha256"]:
            raise ValueError("Pinned model bytes differ")
    attention = register_attention_backend()
    protocol = {"schema": "cosmos-kg-relevance-run-1", "policy_version": policy.VERSION,
        "model_repo": receipt["repo"], "model_revision": receipt["revision"],
        "model_receipt_sha256": base.sha(receipt_path), "input_sha256": input_sha256,
        "code_hashes": code_hashes(), "pairs": len(rows), "do_sample": False,
        "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens,
        "attn_implementation": attention, "quantization": "NF4 double quantization bf16",
        "stage2_receives_stage1_output": False, "truncation": False, "retry_policy": "none",
        "weight_training_performed": False, "human_gold": False,
        "gpu": torch.cuda.get_device_name(0),
        "versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "bitsandbytes", "accelerate")}}
    args.out.mkdir(parents=True)
    base.write_json(args.out / "protocol.json", protocol)
    provenance = {"type": "local_llm_inference", "model_repo": receipt["repo"],
                  "model_revision": receipt["revision"], "protocol_sha256": base.sha(args.out / "protocol.json")}
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False,
        device_map={"": 0}, dtype=torch.bfloat16, attn_implementation=attention,
        quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)).eval()
    results = []
    with (args.out / "results.jsonl").open("x", encoding="utf8", newline="\n") as stream:
        for row in rows:
            first = generate(model, tokenizer, torch, row, 1, args)
            second = generate(model, tokenizer, torch, row, 2, args) if needs_second(first) else None
            decision, relation, evidence, reason = policy.combine(first, second)
            selection = {"article_id": row["article_id"], "ticker": row["ticker"],
                "input_sha256": preview.canonical_input_sha256(row), "decision": decision,
                "relation": relation, "evidence": evidence, "reason": reason,
                "policy_version": policy.VERSION, "model_provenance": provenance}
            result = {"selection": selection, "stages": {"screen": first, "verify": second},
                      "finished_at": datetime.now(timezone.utc).isoformat()}
            validate_result(result, row, protocol)
            stream.write(base.canonical(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            results.append(result)
            print(base.canonical({"completed": len(results), "total": len(rows), "decision": decision,
                "errors": [error for s in (first, second) if s for error in s["errors"]]}), flush=True)
    if protocol["input_sha256"] != base.sha(args.inputs) or protocol["code_hashes"] != code_hashes():
        raise ValueError("Source or code changed during inference")
    with (args.out / "selections.jsonl").open("x", encoding="utf8", newline="\n") as stream:
        for result in results:
            stream.write(base.canonical(result["selection"]) + "\n")
    stages = [stage for result in results for stage in result["stages"].values() if stage is not None]
    summary = {"complete": True, "pairs": len(results),
        "counts": dict(Counter(x["selection"]["decision"] for x in results)),
        "invalid_pairs": sum(any(s["errors"] for s in x["stages"].values() if s) for x in results),
        "stage_count": len(stages), "stage_seconds": sum(s["elapsed_seconds"] for s in stages),
        "wall_seconds": round(time.perf_counter() - started, 4),
        "human_gold": False, "weight_training_performed": False, "service_published": False,
        "files": {name: base.sha(args.out / name) for name in ("protocol.json", "results.jsonl", "selections.jsonl")}}
    base.write_json(args.out / "summary.json", summary)
    print(base.canonical(summary), flush=True)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--max-input-tokens", type=int, default=6144)
    p.add_argument("--max-new-tokens", type=int, default=192)
    run(p.parse_args())
