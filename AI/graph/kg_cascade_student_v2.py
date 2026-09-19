"""Bounded v2 hypotheses for full-article, target-aware KG pseudo-label learning.

Candidate A replaces the fixed unit-norm, linear classifier with LayerNorm and a
small MLP over the SAME full-article weighted mean/max representation. Candidate B
also separates target and content token pooling, using a target-conditioned
window attention plus global content mean/max. Global mean retains a gradient
path from every content window; attention does not select or discard sentences.
Neither change is a demonstrated accuracy improvement until held-out validation.
The default target is the separate compact classification-only teacher protocol;
legacy strict and diagnostic semantic adapters require explicit configuration.
An unusable teacher execution is omitted from loss and counted, never relabelled review.

The two candidate configurations are declared before training: seed 1709,
encoder LR 2e-5, head LR 3e-4, up to 4 epochs, unweighted 3-class cross entropy.
Minimum model-dev log loss selects the epoch. Only already validated train and
model-dev pseudo-labels are used. No calibration/test access, routing threshold
relaxation, evidence generation, corpus publication, or original-file mutation
occurs here. Teacher consistency must not be described as human-grounded truth.

This separate module retains immutable v1 validation/provenance helpers. Original
code and checkpoints remain loadable under their own original protocol. A v2
checkpoint binds the v2 code as well as every v1 helper's existing hash.
"""
from __future__ import annotations

import argparse
import kg_cascade_student as v1
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
    **v1.DEFAULT_CONFIG, "epochs": 4, "head_learning_rate": 3e-4, "candidate": "A",
    "label_policy": "kg_semantic_teacher_v1",
}
FEATURE_PROTOCOL = {
    **v1.FEATURE_PROTOCOL, "schema": "kg-cascade-finetuned-full-window-2",
    "pooling": "candidate_A_global_mean_max_or_B_global_mean_max_target_mean_attention",
    "head": "LayerNorm_Linear_GELU_Dropout_Linear",
    "target_content_masks": "tokenizer_sequence_ids_exclude_special_tokens_for_B",
    "candidate_hypotheses_not_demonstrated_improvements": True,
}
PREDECLARED_CANDIDATES = {
    "A": {**DEFAULT_CONFIG, "candidate": "A"},
    "B": {**DEFAULT_CONFIG, "candidate": "B"},
}


def model_version(config):
    return "kg-cascade-student-v2-" + config["candidate"]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(preview.canonical(value).encode("utf8")).hexdigest()


def code_hashes():
    hashes = {**v1.code_hashes(), "kg_cascade_student_v2.py": preview.file_sha256(Path(__file__))}
    for name in ("kg_cascade_semantic_labels.py", "kg_semantic_teacher.py"):
        semantic = HERE / name
        if semantic.is_file():
            hashes[name] = preview.file_sha256(semantic)
    return hashes


def configuration(config=None):
    supplied = config or {}
    require(set(supplied) <= set(DEFAULT_CONFIG), "Unknown v2 training configuration key")
    result = {**DEFAULT_CONFIG, **supplied}
    require(result["candidate"] in PREDECLARED_CANDIDATES, "Only predeclared candidates A/B are supported")
    require(result["label_policy"] in ("original_strict", "kg_semantic_pseudo_labels_v1", "kg_semantic_teacher_v1"),
            "Only replayed original or explicit semantic pseudo-label policies are supported")
    base = v1.configuration({k: value for k, value in result.items() if k not in ("candidate", "label_policy")})
    require(base["epochs"] <= 4, "The v2 candidate epoch budget is at most four")
    return {**base, "candidate": result["candidate"], "label_policy": result["label_policy"]}


def tokenize_document(tokenizer, row, config=None):
    """Cover every content token; B records exact target/content sequence masks."""
    config = configuration(config)
    base_config = {k: value for k, value in config.items() if k not in ("candidate", "label_policy")}
    windows, coverage = v1.tokenize_document(tokenizer, row, base_config)
    if not windows or config["candidate"] == "A":
        return windows, coverage
    source = preview.validate_input(row)
    backend = tokenizer.backend_tokenizer
    target = backend.encode(source["company_name"], add_special_tokens=False)
    content = backend.encode(source["title"] + "\n" + source["body"], add_special_tokens=False)
    width = config["max_tokens"] - len(target.ids) - tokenizer.num_special_tokens_to_add(pair=True)
    original = list(content.ids)
    content.truncate(width, stride=0)
    pieces = [content, *content.overflowing]
    require([token for piece in pieces for token in piece.ids] == original,
            "v2 mask construction lost or repeated content")
    require(len(pieces) == len(windows), "v2 window metadata differs")
    masked = []
    for (features, weight), piece in zip(windows, pieces):
        pair = backend.post_process(target, piece, add_special_tokens=True)
        require(pair.ids == features["input_ids"], "v2 token metadata does not match base windows")
        sequence_ids = pair.sequence_ids
        target_mask = [int(sequence == 0) for sequence in sequence_ids]
        content_mask = [int(sequence == 1) for sequence in sequence_ids]
        require(len(sequence_ids) == len(pair.ids) and sum(target_mask) == len(target.ids)
                and sum(content_mask) == weight, "Tokenizer must expose exact pair sequence coverage")
        masked.append(({**features, "target_mask": target_mask, "content_mask": content_mask}, weight))
    return masked, coverage


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


def resolve_training_labels(rows, labels, protocol, config):
    """Use one named, code-bound label protocol; None is unusable, not review."""
    if config["label_policy"] == "original_strict":
        return validate_teacher_labels(rows, labels, protocol)
    if config["label_policy"] == "kg_semantic_teacher_v1":
        import kg_semantic_teacher as semantic
    else:
        import kg_cascade_semantic_labels as semantic
    require(semantic.VERSION == config["label_policy"] and tuple(semantic.CLASSES) == CLASSES,
            "Semantic pseudo-label protocol/classes differ")
    values = semantic.validate_labels(rows, labels, protocol)
    require(len(values) == len(rows) and all(value is None or type(value) is int and 0 <= value < len(CLASSES)
                                           for value in values), "Invalid semantic label adapter result")
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
            hidden = encoder.config.hidden_size
            features = hidden * (2 if config["candidate"] == "A" else 4)
            self.head = torch.nn.ModuleDict({"classifier": torch.nn.Sequential(
                torch.nn.LayerNorm(features), torch.nn.Linear(features, hidden),
                torch.nn.GELU(), torch.nn.Dropout(config["head_dropout"]),
                torch.nn.Linear(hidden, len(CLASSES)))})
            if config["candidate"] == "B":
                self.head["window_attention"] = torch.nn.Sequential(
                    torch.nn.LayerNorm(hidden * 4), torch.nn.Linear(hidden * 4, hidden // 2),
                    torch.nn.Tanh(), torch.nn.Linear(hidden // 2, 1))

        def forward(self, windows, tokenizer, device):
            vectors, target_vectors, weights = [], [], []
            for start in range(0, len(windows), config["window_microbatch"]):
                group = windows[start:start + config["window_microbatch"]]
                features = [{k: value for k, value in row.items()
                             if k in ("input_ids", "token_type_ids", "attention_mask")}
                            for row, _ in group]
                inputs = tokenizer.pad(features, padding=True, return_tensors="pt").to(device)
                hidden = self.encoder(**inputs).last_hidden_state
                if config["candidate"] == "A":
                    mask = inputs["attention_mask"].unsqueeze(-1)
                    pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
                else:
                    def masked_mean(key):
                        length = hidden.shape[1]
                        masks = []
                        for row, _ in group:
                            mask = row[key]
                            padding = [0] * (length - len(mask))
                            masks.append(padding + mask if getattr(tokenizer, "padding_side", "right") == "left"
                                         else mask + padding)
                        mask = hidden.new_tensor(masks).unsqueeze(-1)
                        require(bool((mask.sum(1) > 0).all()), "Target/content sequence mask is empty")
                        return (hidden * mask).sum(1) / mask.sum(1)
                    pooled = masked_mean("content_mask")
                    target_vectors.append(masked_mean("target_mask"))
                vectors.append(pooled)
                weights.extend(n for _, n in group)
            stacked = torch.cat(vectors, dim=0)
            w = stacked.new_tensor(weights).unsqueeze(-1)
            mean = (stacked * w).sum(0) / w.sum()
            maximum = stacked.max(dim=0).values
            features = [mean, maximum]
            if config["candidate"] == "B":
                targets = torch.cat(target_vectors, dim=0)
                attention_inputs = torch.cat((stacked, targets, stacked * targets,
                                              (stacked - targets).abs()), dim=-1)
                attention_logits = self.head["window_attention"](attention_inputs).squeeze(-1)
                attention = attention_logits.softmax(dim=0).unsqueeze(-1)
                features.extend(((stacked * attention).sum(0), (targets * w).sum(0) / w.sum()))
            return self.head["classifier"](torch.cat(features)).unsqueeze(0)
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
    y = resolve_training_labels(trainrows, trainlabels, train_protocol, config)
    yd = resolve_training_labels(devrows, devlabels, dev_protocol, config)
    require({value for value in y if value is not None} == set(range(len(CLASSES))), "Training must include all three teacher decisions")
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
    train_indices = [i for i, (windows, _) in enumerate(tokenized["train"]) if windows and y[i] is not None]
    dev_indices = [i for i, (windows, _) in enumerate(tokenized["model_dev"]) if windows and yd[i] is not None]
    require(set(y[i] for i in train_indices) == set(range(len(CLASSES))), "Supported training inputs must retain all three classes")
    require(dev_indices, "No supported model-dev inputs")
    out.mkdir(parents=True, exist_ok=False)
    plan = {"schema": "kg-cascade-student-training-plan-2", "config": config,
        "feature_protocol": FEATURE_PROTOCOL, "model_version": model_version(config),
        "predeclared_candidates": PREDECLARED_CANDIDATES, "encoder": encoder_provenance,
        "code_hashes": before_code, "data_sha256": original_data,
        "source_file_sha256": source_files,
        "label_source": "validated_local_qwen_pseudo_labels_not_gold", "human_gold": False,
        "label_policy": config["label_policy"],
        "unusable_teacher_labels": {"train": [i for i, value in enumerate(y) if value is None],
                                    "model_dev": [i for i, value in enumerate(yd) if value is None]},
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
    manifest = {**plan, "schema": "kg-cascade-student-training-2", "complete": True,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(), "selected_epoch": selected_epoch,
        "trials": trials, "class_counts": {"train": dict(Counter(CLASSES[v] if v is not None else "unusable" for v in y)),
                                           "model_dev": dict(Counter(CLASSES[v] if v is not None else "unusable" for v in yd))},
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
        require(self.manifest.get("schema") == "kg-cascade-student-training-2" and self.manifest.get("complete") is True,
                "Incomplete or unknown student checkpoint")
        require(self.manifest.get("code_hashes") == code_hashes(), "Student training code changed")
        require(self.manifest.get("feature_protocol") == FEATURE_PROTOCOL, "Student feature protocol changed")
        self.config = configuration(self.manifest["config"])
        require(self.manifest.get("model_version") == model_version(self.config), "Student model version differs")
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
                    "model_version": model_version(self.config),
                    "label_policy": self.config["label_policy"],
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
