"""Fine-tune a target-aware, full-article MiniLM student for the KG cascade.

This is a new experiment, not the earlier frozen-encoder logistic regression.
Every encoder parameter and a three-class document head are trained. The existing
receipt-verified multilingual MiniLM is loaded locally and never modified. Each
window pairs the target company with a nonoverlapping part of title + body; all
content tokens must be covered. Differentiable weighted mean and maximum pooling
aggregate windows. A resource limit rejects a whole document rather than silently
classifying a prefix. Such inputs receive no probabilities and require fallback.

Training consumes separately supplied train/model-dev inputs and raw, replayable
Qwen KG results. Teacher decisions are pseudo-labels, never independent gold.
Review is a third class, not a negative. Only model-dev log loss selects an epoch;
this module neither sees calibration/test labels nor chooses routing thresholds.
Probabilities are uncalibrated model scores. It never invents evidence, relation
subtypes, or graph-ready selections. A separately validated router must decide
which outputs may bypass the teacher and obtain evidence when required.
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
import random
import time

import knowledge_graph_news_preview as preview
import knowledge_graph_relevance_policy as policy
import relevance_classifier as legacy
import run_knowledge_graph_relevance as teacher

HERE = Path(__file__).resolve().parent
CLASSES = ("include", "exclude", "review")
DEFAULT_CONFIG = {
    "seed": 1709, "epochs": 3, "learning_rate": 2e-5, "head_learning_rate": 1e-3, "weight_decay": 0.01,
    "gradient_accumulation_docs": 8, "window_microbatch": 4,
    "max_tokens": 256, "max_windows": 64, "max_target_tokens": 64,
    "gradient_clip_norm": 1.0, "head_dropout": 0.1,
}
FEATURE_PROTOCOL = {
    "schema": "kg-cascade-finetuned-full-window-1", "classes": list(CLASSES),
    "encoder_finetuned": True, "input_text_fields": ["company_name", "title", "body"],
    "unused_bert_pooler_loaded": False,
    "pooling": "masked_token_mean_then_content_weighted_window_mean_and_window_max",
    "window_overlap": 0, "full_token_coverage_required": True,
    "unsupported_input_behavior": "no_probabilities_require_fallback",
    "probabilities_calibrated": False, "evidence_extraction": False,
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(preview.canonical(value).encode("utf8")).hexdigest()


def code_hashes():
    return {name: preview.file_sha256(HERE / name) for name in (
        "kg_cascade_student.py", "relevance_classifier.py", "knowledge_graph_news_preview.py",
        "knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py",
        "run_knowledge_graph_relevance.py", "local_news_relevance.py")}


def configuration(config=None):
    supplied = config or {}
    require(set(supplied) <= set(DEFAULT_CONFIG), "Unknown training configuration key")
    result = {**DEFAULT_CONFIG, **supplied}
    for key in ("seed", "epochs", "gradient_accumulation_docs", "window_microbatch", "max_tokens", "max_windows", "max_target_tokens"):
        require(type(result[key]) is int and result[key] > 0, "Expected positive integer: " + key)
    require(result["max_tokens"] <= 512, "Encoder position limit is 512")
    for key in ("learning_rate", "head_learning_rate", "weight_decay", "gradient_clip_norm", "head_dropout"):
        require(type(result[key]) in (int, float) and math.isfinite(result[key]) and result[key] >= 0,
                "Expected nonnegative finite number: " + key)
    require(result["learning_rate"] > 0 and result["head_learning_rate"] > 0
            and result["gradient_clip_norm"] > 0 and result["head_dropout"] < 1,
            "Invalid learning rate, clipping, or dropout")
    return result


def tokenize_document(tokenizer, row, config=None):
    """Return losslessly covered pair windows, or an explicit whole-input fallback."""
    config = configuration(config)
    source = preview.validate_input(row)
    require(row.get("company_context") is None, "External company context is not a student feature")
    backend = tokenizer.backend_tokenizer
    backend.no_truncation()
    backend.no_padding()
    target = backend.encode(source["company_name"], add_special_tokens=False)
    content = backend.encode(source["title"] + "\n" + source["body"], add_special_tokens=False)
    original = list(content.ids)
    width = config["max_tokens"] - len(target.ids) - tokenizer.num_special_tokens_to_add(pair=True)
    coverage = {"total_content_tokens": len(original), "covered_content_tokens": 0,
        "target_tokens": len(target.ids), "windows_required": math.ceil(len(original) / width) if width > 0 else None,
        "windows_processed": 0, "full_coverage": False, "truncated": False,
        "max_window_tokens": 0, "fallback_reasons": []}
    if not target.ids or len(target.ids) > config["max_target_tokens"] or width <= 0:
        coverage["fallback_reasons"].append("unsupported_target_length")
    if not original:
        coverage["fallback_reasons"].append("no_content_tokens")
    if coverage["windows_required"] is not None and coverage["windows_required"] > config["max_windows"]:
        coverage["fallback_reasons"].append("unsupported_document_window_count")
    if coverage["fallback_reasons"]:
        return [], coverage
    content.truncate(width, stride=0)
    pieces = [content, *content.overflowing]
    require([token for piece in pieces for token in piece.ids] == original, "Token windows lost or repeated article content")
    windows = []
    for piece in pieces:
        pair = backend.post_process(target, piece, add_special_tokens=True)
        require(len(pair.ids) == len(pair.type_ids) <= config["max_tokens"], "Invalid pair token shape")
        windows.append(({"input_ids": pair.ids, "token_type_ids": pair.type_ids,
                         "attention_mask": [1] * len(pair.ids)}, len(piece.ids)))
    coverage.update(covered_content_tokens=sum(n for _, n in windows), windows_processed=len(windows),
                    full_coverage=True, max_window_tokens=max(len(item["input_ids"]) for item, _ in windows))
    require(coverage["covered_content_tokens"] == len(original), "Incomplete article coverage")
    return windows, coverage


def validate_teacher_labels(rows, labels, protocol):
    """Replay strict raw stages; loose decisions or an unrelated teacher cannot train."""
    require(rows and len(rows) == len(labels), "Teacher input/label counts differ or are empty")
    require(protocol.get("policy_version") == policy.VERSION, "Teacher economic-relevance policy differs")
    require(protocol.get("model_repo") == "Qwen/Qwen3-4B-Instruct-2507", "Unexpected teacher model")
    require(protocol.get("model_revision") == "cdbee75f17c01a7cc42f958dc650907174af0554", "Unexpected teacher revision")
    indexed = {}
    for result in labels:
        require(isinstance(result, dict) and isinstance(result.get("selection"), dict), "Raw teacher result required")
        key = preview.pair_key(result["selection"])
        require(key not in indexed, "Duplicate teacher label pair")
        indexed[key] = result
    require(len({preview.pair_key(row) for row in rows}) == len(rows), "Duplicate student input pair")
    require(set(indexed) == {preview.pair_key(row) for row in rows}, "Teacher label coverage differs")
    values = []
    for row in rows:
        result = indexed[preview.pair_key(row)]
        teacher.validate_result(result, row, protocol)
        values.append(CLASSES.index(result["selection"]["decision"]))
    return values


def validate_split_isolation(trainrows, devrows):
    """A different target company cannot disguise a duplicate held-out article."""
    def identities(rows):
        found = {"article_id": set(), "url": set(), "content": set()}
        for raw in rows:
            row = preview.validate_input(raw)
            found["article_id"].add(row["article_id"])
            if row["source_url"] and row["source_url"].strip():
                found["url"].add(row["source_url"].strip().lower().rstrip("/"))
            # Prefer over-grouping duplicate source content to optimistic validation.
            normalized = " ".join((row["title"] + "\n" + row["body"]).split()).casefold()
            found["content"].add(hashlib.sha256(normalized.encode("utf8")).hexdigest())
        return found
    a, b = identities(trainrows), identities(devrows)
    for kind in a:
        require(not a[kind] & b[kind], "Train/model-dev leakage: " + kind)


def _runtime(device):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    import torch
    require(not str(device).startswith("cuda") or torch.cuda.is_available(), "CUDA requested but unavailable")
    torch.set_num_threads(4)
    return torch


def _load_encoder(path):
    from transformers import AutoModel, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
    encoder = AutoModel.from_pretrained(str(path), local_files_only=True, trust_remote_code=False,
                                       attn_implementation="eager", add_pooling_layer=False)
    return encoder, tokenizer


def _make_model(encoder, config, torch):
    class DocumentClassifier(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = encoder
            self.head = torch.nn.Sequential(torch.nn.Dropout(config["head_dropout"]),
                torch.nn.Linear(encoder.config.hidden_size * 2, len(CLASSES)))

        def forward(self, windows, tokenizer, device):
            vectors, weights = [], []
            for start in range(0, len(windows), config["window_microbatch"]):
                group = windows[start:start + config["window_microbatch"]]
                inputs = tokenizer.pad([x for x, _ in group], padding=True, return_tensors="pt").to(device)
                hidden = self.encoder(**inputs).last_hidden_state
                mask = inputs["attention_mask"].unsqueeze(-1)
                pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
                vectors.append(pooled)
                weights.extend(n for _, n in group)
            stacked = torch.cat(vectors, dim=0)
            w = stacked.new_tensor(weights).unsqueeze(-1)
            mean = (stacked * w).sum(0) / w.sum()
            maximum = stacked.max(dim=0).values
            # Both paths retain gradients into every encoded window.
            features = torch.cat((torch.nn.functional.normalize(mean, dim=0),
                                  torch.nn.functional.normalize(maximum, dim=0)))
            return self.head(features).unsqueeze(0)
    return DocumentClassifier()


def _save_model(model, tokenizer, directory):
    from safetensors.torch import save_file
    directory.mkdir(parents=True, exist_ok=True)
    model.encoder.save_pretrained(directory / "encoder", safe_serialization=True)
    tokenizer.save_pretrained(directory / "encoder")
    save_file({key: value.detach().cpu().contiguous() for key, value in model.head.state_dict().items()},
              str(directory / "head.safetensors"))


def _artifact_hashes(directory):
    return {path.relative_to(directory).as_posix(): {"bytes": path.stat().st_size,
        "sha256": preview.file_sha256(path)} for path in sorted(directory.rglob("*")) if path.is_file()}


def _dump(path, value):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        stream.write(preview.canonical(value) + "\n")


def _sync(torch, device):
    if str(device).startswith("cuda"):
        torch.cuda.synchronize()


def train_model(trainrows, trainlabels, devrows, devlabels, out, *, train_protocol,
                dev_protocol, encoder_bundle=legacy.BUNDLE, config=None, device="cuda", source_files=None):
    """Return a complete manifest; incomplete directories are never loadable models."""
    started = time.perf_counter()
    config, out = configuration(config), Path(out).resolve()
    require(not out.exists(), "Training output must be new")
    trainrows, devrows, trainlabels, devlabels = map(list, (trainrows, devrows, trainlabels, devlabels))
    validate_split_isolation(trainrows, devrows)
    y = validate_teacher_labels(trainrows, trainlabels, train_protocol)
    yd = validate_teacher_labels(devrows, devlabels, dev_protocol)
    require(set(y) == set(range(len(CLASSES))), "Training must include all three teacher decisions")
    before_code = code_hashes()
    original_data = digest([trainrows, trainlabels, devrows, devlabels, train_protocol, dev_protocol])
    source_files = dict(source_files or {})
    require(all(preview.file_sha256(path) == expected for path, expected in source_files.items()),
            "Training source file hash differs")
    encoder_path, encoder_provenance = legacy.verified_encoder(encoder_bundle)
    torch = _runtime(device)
    random.seed(config["seed"])
    torch.manual_seed(config["seed"])
    if str(device).startswith("cuda"):
        torch.cuda.manual_seed_all(config["seed"])
    encoder, tokenizer = _load_encoder(encoder_path)
    encoder.config.use_cache = False
    encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model = _make_model(encoder, config, torch).to(device)
    require(all(p.requires_grad for p in model.encoder.parameters()), "All encoder parameters must be fine-tuned")
    tokenized = {}
    unsupported = []
    for split, rows in (("train", trainrows), ("model_dev", devrows)):
        tokenized[split] = []
        for index, row in enumerate(rows):
            windows, coverage = tokenize_document(tokenizer, row, config)
            tokenized[split].append((windows, coverage))
            if not windows:
                unsupported.append({"split": split, "index": index, "article_id": row["article_id"],
                    "ticker": row["ticker"], "coverage": coverage})
    train_indices = [i for i, (windows, _) in enumerate(tokenized["train"]) if windows]
    dev_indices = [i for i, (windows, _) in enumerate(tokenized["model_dev"]) if windows]
    require(set(y[i] for i in train_indices) == set(range(len(CLASSES))), "Supported training inputs must retain all three classes")
    require(dev_indices, "No supported model-dev inputs")
    out.mkdir(parents=True, exist_ok=False)
    plan = {"schema": "kg-cascade-student-training-plan-1", "config": config,
        "feature_protocol": FEATURE_PROTOCOL, "encoder": encoder_provenance,
        "code_hashes": before_code, "data_sha256": original_data,
        "source_file_sha256": source_files,
        "label_source": "validated_local_qwen_pseudo_labels_not_gold", "human_gold": False,
        "calibration_or_test_labels_used": False, "epochs_selection": "minimum_supported_model_dev_log_loss_then_earlier_epoch",
        "unsupported_inputs": unsupported, "train_pairs": len(trainrows), "model_dev_pairs": len(devrows)}
    _dump(out / "plan.json", plan)
    optimizer = torch.optim.AdamW([
        {"params": model.encoder.parameters(), "lr": config["learning_rate"]},
        {"params": model.head.parameters(), "lr": config["head_learning_rate"]},
    ], weight_decay=config["weight_decay"])
    loss_fn = torch.nn.CrossEntropyLoss()
    trials, best = [], float("inf")
    rng = random.Random(config["seed"])
    for epoch in range(1, config["epochs"] + 1):
        epoch_started = time.perf_counter()
        model.train()
        order, losses = list(train_indices), []
        rng.shuffle(order)
        for offset in range(0, len(order), config["gradient_accumulation_docs"]):
            group = order[offset:offset + config["gradient_accumulation_docs"]]
            optimizer.zero_grad(set_to_none=True)
            for index in group:
                logits = model(tokenized["train"][index][0], tokenizer, device)
                loss = loss_fn(logits, torch.tensor([y[index]], device=device))
                require(bool(torch.isfinite(loss).item()), "Nonfinite training loss")
                losses.append(float(loss.detach().cpu()))
                (loss / len(group)).backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), config["gradient_clip_norm"], error_if_nonfinite=True)
            optimizer.step()
        model.eval()
        dev_losses = []
        with torch.inference_mode():
            for index in dev_indices:
                logits = model(tokenized["model_dev"][index][0], tokenizer, device)
                loss = loss_fn(logits, torch.tensor([yd[index]], device=device))
                require(bool(torch.isfinite(loss).item()), "Nonfinite model-dev loss")
                dev_losses.append(float(loss.cpu()))
        _sync(torch, device)
        trial = {"epoch": epoch, "train_log_loss": sum(losses) / len(losses),
            "model_dev_log_loss": sum(dev_losses) / len(dev_losses),
            "train_pairs_used": len(train_indices), "model_dev_pairs_used": len(dev_indices),
            "seconds": time.perf_counter() - epoch_started}
        if trial["model_dev_log_loss"] < best:
            best = trial["model_dev_log_loss"]
            selected_epoch = epoch
            _save_model(model, tokenizer, out / "checkpoint")
        trials.append(trial)
        with (out / "epochs.jsonl").open("a", encoding="utf8", newline="\n") as stream:
            stream.write(preview.canonical(trial) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(preview.canonical(trial), flush=True)
    require(before_code == code_hashes(), "Training code changed")
    require(original_data == digest([trainrows, trainlabels, devrows, devlabels, train_protocol, dev_protocol]), "Training source changed")
    require(encoder_provenance == legacy.verified_encoder(encoder_bundle)[1], "Parent encoder changed")
    require(all(preview.file_sha256(path) == expected for path, expected in source_files.items()),
            "Training source files changed")
    manifest = {**plan, "schema": "kg-cascade-student-training-1", "complete": True,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(), "selected_epoch": selected_epoch,
        "trials": trials, "class_counts": {"train": dict(Counter(CLASSES[v] for v in y)),
                                           "model_dev": dict(Counter(CLASSES[v] for v in yd))},
        "checkpoint_files": _artifact_hashes(out / "checkpoint"), "wall_seconds": time.perf_counter() - started,
        "versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "safetensors")},
        "production_approved": False, "routing_thresholds": None, "evidence_extraction": False,
        "graph_materialization_ready": False}
    _dump(out / "training_manifest.json", manifest)
    del optimizer, model, encoder, tokenizer
    if str(device).startswith("cuda"):
        torch.cuda.empty_cache()
    return manifest


class StudentPredictor:
    """Load once for repeated bounded candidate chunks; close before Qwen loads."""
    def __init__(self, checkpoint, *, device="cuda"):
        self.root = Path(checkpoint).resolve()
        self.manifest_path = self.root / "training_manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf8"))
        self.manifest_sha256 = preview.file_sha256(self.manifest_path)
        require(self.manifest.get("schema") == "kg-cascade-student-training-1" and self.manifest.get("complete") is True,
                "Incomplete or unknown student checkpoint")
        require(self.manifest.get("code_hashes") == code_hashes(), "Student training code changed")
        require(self.manifest.get("feature_protocol") == FEATURE_PROTOCOL, "Student feature protocol changed")
        self.config = configuration(self.manifest["config"])
        self._check_files()
        self.device, self.torch = device, _runtime(device)
        encoder, self.tokenizer = _load_encoder(self.root / "checkpoint/encoder")
        self.model = _make_model(encoder, self.config, self.torch).to(device)
        from safetensors.torch import load_file
        self.model.head.load_state_dict(load_file(str(self.root / "checkpoint/head.safetensors")))
        self.model.eval()

    def _check_files(self):
        require(self.manifest_sha256 == preview.file_sha256(self.manifest_path), "Student manifest changed")
        require(_artifact_hashes(self.root / "checkpoint") == self.manifest["checkpoint_files"], "Student checkpoint bytes changed")

    def predict(self, rows):
        rows = list(rows)
        require(len({preview.pair_key(row) for row in rows}) == len(rows), "Duplicate student prediction pair")
        require(self.manifest["code_hashes"] == code_hashes(), "Student code changed after load")
        outputs = []
        with self.torch.inference_mode():
            for row in rows:
                _sync(self.torch, self.device)
                started = time.perf_counter()
                windows, coverage = tokenize_document(self.tokenizer, row, self.config)
                probabilities = None
                if windows:
                    try:
                        logits = self.model(windows, self.tokenizer, self.device)
                        require(bool(self.torch.isfinite(logits).all()), "Nonfinite student logits")
                        values = logits.softmax(dim=-1)[0].cpu().tolist()
                        probabilities = dict(zip(CLASSES, values))
                    except self.torch.cuda.OutOfMemoryError:
                        coverage["fallback_reasons"].append("student_cuda_out_of_memory")
                    if probabilities is None and str(self.device).startswith("cuda"):
                        # The exception scope is gone, so failed activations are releasable.
                        self.torch.cuda.empty_cache()
                coverage["encoding_completed"] = probabilities is not None
                _sync(self.torch, self.device)
                outputs.append({"article_id": row["article_id"], "ticker": row["ticker"],
                    "input_sha256": preview.canonical_input_sha256(row), "probabilities": probabilities,
                    "predicted_class": max(probabilities, key=probabilities.get) if probabilities else None,
                    "requires_fallback": probabilities is None, "fallback_reasons": coverage["fallback_reasons"],
                    "coverage": coverage, "elapsed_seconds": time.perf_counter() - started,
                    "student_manifest_sha256": self.manifest_sha256,
                    "probabilities_calibrated": False, "human_gold": False, "evidence": None,
                    "graph_materialization_ready": False})
        return outputs

    def close(self):
        self._check_files()
        del self.model, self.tokenizer
        if str(self.device).startswith("cuda"):
            self.torch.cuda.empty_cache()


def predict(rows, checkpoint, *, device="cuda"):
    predictor = StudentPredictor(checkpoint, device=device)
    try:
        return predictor.predict(rows)
    finally:
        predictor.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("train", "predict"))
    p.add_argument("--train-inputs", type=Path)
    p.add_argument("--train-results", type=Path)
    p.add_argument("--train-protocol", type=Path)
    p.add_argument("--dev-inputs", type=Path)
    p.add_argument("--dev-results", type=Path)
    p.add_argument("--dev-protocol", type=Path)
    p.add_argument("--encoder-bundle", type=Path, default=legacy.BUNDLE)
    p.add_argument("--config", type=Path)
    p.add_argument("--inputs", type=Path)
    p.add_argument("--checkpoint", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = p.parse_args()
    if args.mode == "train":
        paths = [args.train_inputs, args.train_results, args.train_protocol,
                 args.dev_inputs, args.dev_results, args.dev_protocol]
        require(all(paths), "Train/model-dev inputs, raw results and protocols are required")
        if args.config:
            paths.append(args.config)
        hashes = {str(path): preview.file_sha256(path) for path in paths}
        train_model(preview.read_jsonl(args.train_inputs), preview.read_jsonl(args.train_results),
            preview.read_jsonl(args.dev_inputs), preview.read_jsonl(args.dev_results), args.out,
            train_protocol=json.loads(args.train_protocol.read_text(encoding="utf8")),
            dev_protocol=json.loads(args.dev_protocol.read_text(encoding="utf8")),
            encoder_bundle=args.encoder_bundle,
            config=json.loads(args.config.read_text(encoding="utf8")) if args.config else None,
            device=args.device, source_files=hashes)
    else:
        require(args.inputs and args.checkpoint, "Prediction requires --inputs and --checkpoint")
        require(not args.out.exists(), "Prediction output must be new")
        input_sha = preview.file_sha256(args.inputs)
        result = predict(preview.read_jsonl(args.inputs), args.checkpoint, device=args.device)
        require(input_sha == preview.file_sha256(args.inputs), "Prediction inputs changed")
        args.out.mkdir(parents=True, exist_ok=False)
        with (args.out / "results.jsonl").open("x", encoding="utf8", newline="\n") as stream:
            for row in result:
                stream.write(preview.canonical(row) + "\n")
        _dump(args.out / "summary.json", {"complete": True, "pairs": len(result), "input_sha256": input_sha,
            "requires_fallback": sum(row["requires_fallback"] for row in result),
            "inference_seconds": sum(row["elapsed_seconds"] for row in result), "production_approved": False})


if __name__ == "__main__":
    main()
