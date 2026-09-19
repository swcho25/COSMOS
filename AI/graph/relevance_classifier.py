"""Frozen multilingual pair encoder + a small regularized relevance classifier.

Only company_name, title and body enter the encoder. Every token of title/body
is covered in non-overlapping windows; the target company is the first sequence
in each window. This is a frozen sentence encoder with a trained linear head,
not a fine-tuned cross-encoder or a calibrated probability estimator. Dedicated
price labels, AI rationales, dates, ticker IDs and human choice metadata are not
features; dates/numbers within the original text remain intact. Small assisted
human labels are development supervision, not gold.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.metadata
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import local_news_relevance as base

HERE = Path(__file__).resolve().parent
BUNDLE = HERE / "artifacts/news_impact_v2_bundle"
MAX_TOKENS = 256
CS = (0.1, 1.0, 10.0)
ADOPT_GRID = tuple(round(v / 100, 2) for v in range(50, 100, 5))
EXCLUDE_GRID = tuple(round(v / 100, 2) for v in range(5, 50, 5))
FEATURE_PROTOCOL = {"name": "frozen_target_pair_full_token_mean_max_v1", "max_tokens": MAX_TOKENS,
    "full_coverage": True, "window_overlap": 0, "feature_dim": 768,
    "pooling": "masked_mean_per_pair_window; content-token-weighted_mean_and_elementwise_max; each_L2_normalized; concatenated",
    "input_text_fields": ["company_name", "title", "body"], "company_context_used": False,
    "inference_semantic_filter": False, "encoder_finetuned": False}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def dump_new(path, value):
    with Path(path).open("x", encoding="utf8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def code_hashes():
    return {"classifier": base.sha(Path(__file__)), "base": base.sha(Path(base.__file__))}


def text_only(row):
    value = {key: row.get(key) for key in FEATURE_PROTOCOL["input_text_fields"]}
    require(all(isinstance(v, str) and v.strip() for v in value.values()), "Missing nonempty input text")
    require(row.get("company_context") is None, "This pilot does not use company_context")
    return value


def windows(tokenizer, row):
    value = text_only(row)
    backend = tokenizer.backend_tokenizer
    backend.no_truncation()
    backend.no_padding()
    target = backend.encode(value["company_name"], add_special_tokens=False)
    content = backend.encode(value["title"] + "\n" + value["body"], add_special_tokens=False)
    original_ids = list(content.ids)
    require(0 < len(target.ids) <= 64 and original_ids, "Target is empty/overlong or article has no tokens")
    width = MAX_TOKENS - len(target.ids) - tokenizer.num_special_tokens_to_add(pair=True)
    require(width > 0, "No article capacity")
    # Retain every overflow chunk; never feed a truncated prefix alone.
    content.truncate(width, stride=0)
    pieces = [content, *content.overflowing]
    require([token for piece in pieces for token in piece.ids] == original_ids, "Token windows lost or duplicated content")
    result = []
    for piece in pieces:
        encoded = backend.post_process(target, piece, add_special_tokens=True)
        ids, types = encoded.ids, encoded.type_ids
        require(len(ids) == len(types) and len(ids) <= MAX_TOKENS, "Pair window shape mismatch")
        result.append(({"input_ids": ids, "token_type_ids": types, "attention_mask": [1] * len(ids)}, len(piece.ids)))
    coverage = {"total_content_tokens": len(original_ids), "covered_content_tokens": sum(n for _, n in result),
        "windows": len(result), "target_tokens": len(target.ids), "max_window_tokens": max(len(x["input_ids"]) for x, _ in result)}
    require(coverage["total_content_tokens"] == coverage["covered_content_tokens"], "Incomplete token coverage")
    return result, coverage


def pool_windows(vectors, weights):
    vectors, weights = np.asarray(vectors, dtype=np.float64), np.asarray(weights, dtype=np.float64)
    require(vectors.ndim == 2 and vectors.shape[0] == len(weights) and vectors.shape[1] == 384,
            "Expected 384 dimensional window embeddings")
    require(np.isfinite(vectors).all() and np.isfinite(weights).all() and (weights > 0).all(), "Invalid pooled inputs")
    mean = np.average(vectors, weights=weights, axis=0)
    maximum = vectors.max(axis=0)
    values = [v / max(float(np.linalg.norm(v)), 1e-12) for v in (mean, maximum)]
    return np.concatenate(values).astype(np.float32)


def verified_encoder(bundle):
    bundle = Path(bundle).resolve()
    spec = json.loads((bundle / "pretrained.json").read_text(encoding="utf8"))["embedding"]
    manifest = json.loads((bundle / "bundle_manifest.json").read_text(encoding="utf8"))
    prefix = spec["path"].replace("\\", "/").rstrip("/") + "/"
    selected = {name: metadata for name, metadata in manifest.items() if name.startswith(prefix)}
    require(selected and any(name.endswith("model.safetensors") for name in selected), "Missing pinned encoder files")
    for name, expected in selected.items():
        path = (bundle / name).resolve()
        require(path.is_relative_to(bundle) and path.stat().st_size == expected["bytes"]
                and base.sha(path) == expected["sha256"], "Encoder bytes differ: " + name)
    return bundle / spec["path"], {"model_repo": spec["repo"], "model_revision": spec["revision"],
        "model_files": {name: item["sha256"] for name, item in selected.items()},
        "pretrained_sha256": base.sha(bundle / "pretrained.json"), "bundle_manifest_sha256": base.sha(bundle / "bundle_manifest.json")}


def encode(rows, bundle=BUNDLE, device="cuda"):
    import torch
    from transformers import AutoModel, AutoTokenizer
    path, encoder = verified_encoder(bundle)
    torch.set_num_threads(4)
    require(device != "cuda" or torch.cuda.is_available(), "CUDA requested but unavailable")
    tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(str(path), local_files_only=True, trust_remote_code=False,
                                     attn_implementation="eager").to(device).eval()
    vectors, metadata, elapsed = [], [], []
    with torch.inference_mode():
        for number, row in enumerate(rows):
            if device == "cuda":
                torch.cuda.synchronize()
            started = time.perf_counter()
            segments, coverage = windows(tokenizer, row)
            embeddings, weights = [], []
            for start in range(0, len(segments), 16):
                batch = segments[start:start + 16]
                inputs = tokenizer.pad([item[0] for item in batch], padding=True, return_tensors="pt").to(device)
                hidden = model(**inputs).last_hidden_state
                mask = inputs["attention_mask"].unsqueeze(-1)
                pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
                pooled = torch.nn.functional.normalize(pooled, dim=-1)
                embeddings.extend(pooled.cpu().numpy())
                weights.extend(item[1] for item in batch)
            vectors.append(pool_windows(embeddings, weights))
            metadata.append(coverage)
            if device == "cuda":
                torch.cuda.synchronize()
            elapsed.append(time.perf_counter() - started)
            print(base.canonical({"encoded": number + 1, "total": len(rows), "windows": coverage["windows"]}), flush=True)
    del model, tokenizer
    if device == "cuda":
        torch.cuda.empty_cache()
    return np.asarray(vectors, dtype=np.float32), metadata, elapsed, encoder


def scores(vectors, model):
    x = np.asarray(vectors, dtype=np.float64)
    coef = np.asarray(model["coef"], dtype=np.float64)
    intercept = float(model["intercept"])
    require(model.get("classes") == [0, 1] and coef.shape == (768,) and x.ndim == 2 and x.shape[1] == 768,
            "Linear model shape/classes differ")
    require(np.isfinite(x).all() and np.isfinite(coef).all() and np.isfinite(intercept), "Nonfinite model/features")
    z = x @ coef + intercept
    # Stable sigmoid with no score clipping or threshold-dependent rounding.
    values = np.empty_like(z)
    positive = z >= 0
    values[positive] = 1 / (1 + np.exp(-z[positive]))
    ez = np.exp(z[~positive])
    values[~positive] = ez / (1 + ez)
    return values


def choose_thresholds(y, score):
    y, score = np.asarray(y), np.asarray(score)
    require(set(y.tolist()) == {0, 1} and np.isfinite(score).all(), "Development requires both binary labels and finite scores")
    adopts, excludes = [], []
    for threshold in ADOPT_GRID:
        mask = score >= threshold
        tp, fp = int(((y == 1) & mask).sum()), int(((y == 0) & mask).sum())
        if tp >= 3 and fp == 0:
            adopts.append((tp, threshold))
    for threshold in EXCLUDE_GRID:
        mask = score <= threshold
        tn, fn = int(((y == 0) & mask).sum()), int(((y == 1) & mask).sum())
        if tn >= 3 and fn == 0:
            excludes.append((tn, -threshold))
    return {"adopt_enabled": bool(adopts), "exclude_enabled": bool(excludes),
            "adopt_threshold": float(max(adopts)[1]) if adopts else 1.0,
            "exclude_threshold": float(-max(excludes)[1]) if excludes else 0.0}


def decision(score, selection, errors=None):
    require(np.isfinite(score) and 0 <= score <= 1, "Invalid score")
    require(0 <= selection["exclude_threshold"] < selection["adopt_threshold"] <= 1, "Thresholds overlap")
    require(type(selection["adopt_enabled"]) is bool and type(selection["exclude_enabled"]) is bool, "Invalid enabled flags")
    if errors:
        return "보류"
    if selection["adopt_enabled"] and score >= selection["adopt_threshold"]:
        return "채택"
    if selection["exclude_enabled"] and score <= selection["exclude_threshold"]:
        return "제외"
    return "보류"


def train(seed, out, bundle=BUNDLE, device="cuda"):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import log_loss
    seed, out = Path(seed).resolve(), Path(out).resolve()
    require(not out.exists(), "Training output must be new")
    seed_manifest_path = seed / "manifest.json"
    seed_manifest_sha = base.sha(seed_manifest_path)
    seed_manifest = json.loads(seed_manifest_path.read_text(encoding="utf8"))
    require(seed_manifest.get("schema") == "relevance-classifier-seed-data-1"
            and seed_manifest.get("status") == "frozen"
            and seed_manifest.get("labels_and_inputs_separate") is True
            and seed_manifest.get("independent_human_gold") is False
            and set(seed_manifest.get("leakage_check", {}).values()) == {"PASS"}, "Unverified seed provenance")
    for filename, item in seed_manifest["files"].items():
        source = (seed / filename).resolve()
        require(source.is_relative_to(seed) and base.sha(source) == item["sha256"], "Seed manifest file mismatch")
    paths = {name: seed / (name + ".jsonl") for name in ("train_inputs", "dev_inputs", "train_labels", "dev_labels")}
    hashes = {name: base.sha(path) for name, path in paths.items()}
    inputs = {name: base.read_rows(paths[name + "_inputs"]) for name in ("train", "dev")}
    labels = {name: base.read_rows(paths[name + "_labels"]) for name in ("train", "dev")}
    label_mapping = {"제외": 0, "채택": 1}
    require(seed_manifest.get("label_mapping") == label_mapping, "Seed binary mapping differs")
    for part in ("train", "dev"):
        require([r["sample_id"] for r in inputs[part]] == [r["sample_id"] for r in labels[part]], "Input/label alignment differs")
        require(all(r["label"] in label_mapping for r in labels[part]), "Only explicit adopt/exclude seed labels allowed")
        labels[part] = [dict(r, label=label_mapping[r["label"]]) for r in labels[part]]
        require(set(r["label"] for r in labels[part]) == {0, 1}, "Binary labels with both classes required")
        require(all(type(r["label"]) is int for r in labels[part]), "Labels must be binary integers")
        require(len({r["sample_id"] for r in inputs[part]}) == len(inputs[part]), "Duplicate sample IDs")
    require(not {r["sample_id"] for r in inputs["train"]} & {r["sample_id"] for r in inputs["dev"]}, "Train/dev overlap")
    rows = inputs["train"] + inputs["dev"]
    before_code = code_hashes()
    vectors, coverage, elapsed, encoder = encode(rows, bundle, device)
    n = len(inputs["train"])
    y = np.asarray([r["label"] for r in labels["train"]], dtype=np.int64)
    yd = np.asarray([r["label"] for r in labels["dev"]], dtype=np.int64)
    require(np.isfinite(vectors).all(), "Nonfinite encoder features")
    models, trials = {}, []
    weights = np.asarray([len(yd) / (2 * int((yd == label).sum())) for label in yd])
    for c in CS:
        fit = LogisticRegression(C=c, class_weight="balanced", max_iter=3000, solver="lbfgs", random_state=1709)
        fit.fit(vectors[:n].astype(np.float64), y)
        model = {"classes": fit.classes_.tolist(), "coef": fit.coef_[0].tolist(), "intercept": float(fit.intercept_[0])}
        score = scores(vectors[n:], model)
        trials.append({"C": c, "dev_balanced_log_loss": float(log_loss(yd, score, sample_weight=weights, labels=[0, 1]))})
        models[c] = model
    chosen = min(trials, key=lambda trial: (trial["dev_balanced_log_loss"], trial["C"]))
    model = models[chosen["C"]]
    dev_scores = scores(vectors[n:], model)
    selection = {"C": chosen["C"], **choose_thresholds(yd, dev_scores)}
    require(hashes == {name: base.sha(path) for name, path in paths.items()} and before_code == code_hashes(), "Training dependencies changed")
    require(base.sha(seed_manifest_path) == seed_manifest_sha, "Seed manifest changed during training")
    out.mkdir(parents=True, exist_ok=False)
    dump_new(out / "model.json", model)
    np.savez(out / "features.npz", vectors=vectors)
    dev_rows = [{"sample_id": row["sample_id"], "label": int(label), "score": float(score),
        "effective_selection": decision(float(score), selection)} for row, label, score in zip(inputs["dev"], yd, dev_scores)]
    with (out / "dev_results.jsonl").open("x", encoding="utf8") as stream:
        stream.write("".join(base.canonical(row) + "\n" for row in dev_rows))
    manifest = {"schema": "relevance-classifier-training-1", "complete": True,
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(), "code_hashes": before_code,
        "feature_protocol": FEATURE_PROTOCOL, "encoder": encoder, "selection": selection,
        "threshold_source": "development_only", "scorer_contract": "sigmoid_linear_float64_not_calibrated",
        "label_origin": "ai_assisted_user_selection_not_independent_gold", "train_rows": n, "dev_rows": len(yd),
        "source_paths": {name: str(path) for name, path in paths.items()}, "source_sha256": hashes,
        "seed_manifest_path": str(seed_manifest_path), "seed_manifest_sha256": seed_manifest_sha,
        "C_grid": list(CS), "C_selection": "minimum_dev_class_balanced_log_loss_then_lower_C", "trials": trials,
        "threshold_rule": "adopt grid .50:.05:.95, >=3 dev true-positive and 0 false-positive, max TP then higher threshold; exclude .05:.05:.45, >=3 dev true-negative and 0 false-negative, max TN then lower threshold; otherwise disabled",
        "adopt_grid": list(ADOPT_GRID), "exclude_grid": list(EXCLUDE_GRID),
        "model_artifact_sha256": base.sha(out / "model.json"), "feature_cache_sha256": base.sha(out / "features.npz"),
        "coverage": coverage, "encoding_seconds": sum(elapsed),
        "versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "numpy", "scikit-learn")},
        "fresh_evaluation_seen": False, "human_gold": False, "gnn_training_performed": False,
        "limitations": ["Only a small assisted-label seed; thresholds and C share a small dev set.",
            "Frozen pair encoder pooling may lose token-level roles; long text is covered, not guaranteed understood.",
            "Score is not a calibrated correctness probability and developer labels are not price effects.",
            "Current company names can be incomplete historical identities; no unsupported alias supplementation."]}
    dump_new(out / "training_manifest.json", manifest)
    print(base.canonical({"complete": True, "train": n, "dev": len(yd), "selection": selection, "trials": trials}), flush=True)
    return manifest


def predict(inputs, model_dir, out, bundle=BUNDLE, device="cuda"):
    inputs, model_dir, out = Path(inputs).resolve(), Path(model_dir).resolve(), Path(out).resolve()
    require(not out.exists(), "Prediction output must be new")
    rows = base.read_rows(inputs)
    require(rows and len({base.pair_key(r) for r in rows}) == len(rows), "Empty/duplicate prediction pairs")
    for row in rows:
        base.validate_input(row)
        text_only(row)
    manifest_path = model_dir / "training_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    manifest_sha, input_sha = base.sha(manifest_path), base.sha(inputs)
    require(manifest["complete"] and manifest["code_hashes"] == code_hashes()
            and manifest["feature_protocol"] == FEATURE_PROTOCOL, "Training protocol changed")
    model_path = model_dir / "model.json"
    require(base.sha(model_path) == manifest["model_artifact_sha256"], "Trained model bytes changed")
    model = json.loads(model_path.read_text(encoding="utf8"))
    vectors, coverage, elapsed, encoder = encode(rows, bundle, device)
    require(encoder == manifest["encoder"], "Inference encoder differs from training")
    score = scores(vectors, model)
    results = [{"article_id": row["article_id"], "ticker": row["ticker"],
        "input_sha256": hashlib.sha256(base.canonical(row).encode()).hexdigest(),
        "score": float(value), "effective_selection": decision(float(value), manifest["selection"]),
        "validation_errors": [], "elapsed_seconds": seconds, "coverage": cover}
        for row, value, seconds, cover in zip(rows, score, elapsed, coverage)]
    require(input_sha == base.sha(inputs) and manifest_sha == base.sha(manifest_path)
            and manifest["code_hashes"] == code_hashes() and base.sha(model_path) == manifest["model_artifact_sha256"],
            "Prediction dependencies changed")
    out.mkdir(parents=True, exist_ok=False)
    np.savez(out / "features.npz", vectors=vectors)
    with (out / "results.jsonl").open("x", encoding="utf8") as stream:
        stream.write("".join(base.canonical(row) + "\n" for row in results))
    protocol = {"schema": "relevance-classifier-prediction-1", "complete": True, "input_sha256": input_sha,
        "rows": len(rows), "code_hashes": code_hashes(), "feature_protocol": FEATURE_PROTOCOL,
        "encoder": encoder, "training_manifest_path": str(manifest_path), "training_manifest_sha256": manifest_sha,
        "model_artifact_path": str(model_path), "model_artifact_sha256": base.sha(model_path),
        "feature_cache_sha256": base.sha(out / "features.npz"), "threshold_source": "development_only",
        "selection": manifest["selection"], "scorer_contract": manifest["scorer_contract"],
        "score_is_calibrated_probability": False}
    dump_new(out / "protocol.json", protocol)
    print(base.canonical({"complete": True, "pairs": len(rows), "counts": dict(Counter(r["effective_selection"] for r in results)), "encoding_seconds": sum(elapsed)}), flush=True)
    return protocol


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("train", "predict"))
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, default=BUNDLE)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    if args.mode == "train":
        require(args.seed is not None, "Training requires --seed")
        train(args.seed, args.out, args.bundle, args.device)
    else:
        require(args.inputs is not None and args.model_dir is not None, "Prediction requires --inputs and --model-dir")
        predict(args.inputs, args.model_dir, args.out, args.bundle, args.device)
