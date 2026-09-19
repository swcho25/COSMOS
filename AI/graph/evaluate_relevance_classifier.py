"""Audit a frozen relevance classifier against a raw-validated v1 LLM baseline.

Every input remains in the denominator. References are independent AI reviews,
not human gold; classifier scores are not calibrated probabilities. Evaluation
must never select regularization or thresholds using these references.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import unicodedata

import compare_relevance_policy_v2 as reference_contract
import compare_relevance_policy_v3 as baseline_contract
import local_news_relevance as base
import run_relevance_two_stage as baseline
import relevance_classifier as classifier
from lake_news_normalize import canonicalize_url

CHOICES = ("채택", "제외", "보류")
LABEL_MAPPING = {"제외": 0, "채택": 1}


def same(one, two):
    return base.canonical(one) == base.canonical(two)


def read_json(text):
    return baseline_contract.read_json(text)


def read_rows(path):
    return baseline_contract.read_rows(path)


def indexed(rows):
    return baseline_contract.indexed(rows)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite_number(value, name, minimum=None, maximum=None):
    require(type(value) in (int, float) and math.isfinite(value), "Invalid finite number: " + name)
    require(minimum is None or value >= minimum, "Below minimum: " + name)
    require(maximum is None or value <= maximum, "Above maximum: " + name)
    return value


def selected(score, exclude_threshold, adopt_threshold, errors=(), *, adopt_enabled=True, exclude_enabled=True):
    """Reproduce inclusive endpoints; errors never promote a row."""
    finite_number(exclude_threshold, "exclude_threshold", 0, 1)
    finite_number(adopt_threshold, "adopt_threshold", 0, 1)
    require(exclude_threshold < adopt_threshold, "Overlapping decision thresholds")
    require(type(adopt_enabled) is bool and type(exclude_enabled) is bool, "Enable flags must be booleans")
    if score is not None:
        finite_number(score, "score", 0, 1)
    require(isinstance(errors, (list, tuple)) and all(isinstance(e, str) and e.strip() for e in errors),
            "Invalid error list")
    if errors:
        return "보류"
    require(score is not None, "Valid prediction requires a score")
    return ("채택" if adopt_enabled and score >= adopt_threshold else
            "제외" if exclude_enabled and score <= exclude_threshold else "보류")


def measure(records, references):
    require(set(records) == set(references) and bool(references), "Every reference/input pair is required")
    counts = {truth: {prediction: 0 for prediction in CHOICES} for truth in CHOICES}
    errors = Counter()
    invalid = 0
    elapsed = 0.0
    for key, row in records.items():
        prediction, truth = row["effective_selection"], references[key]["expected_selection"]
        require(prediction in CHOICES and truth in CHOICES, "Unknown selection")
        counts[truth][prediction] += 1
        require(isinstance(row["validation_errors"], list)
                and all(isinstance(e, str) and e for e in row["validation_errors"]), "Invalid result errors")
        invalid += bool(row["validation_errors"])
        errors.update(row["validation_errors"])
        elapsed += finite_number(row["elapsed_seconds"], "elapsed_seconds", 0)
    choices = {choice: sum(counts[truth][choice] for truth in CHOICES) for choice in CHOICES}
    return {
        "pairs": len(references), "denominator_all_pairs": len(references), "selections": choices,
        "reference_by_model": counts,
        "adopted_reference_adopt": counts["채택"]["채택"],
        "adopted_reference_exclude": counts["제외"]["채택"],
        "adopted_reference_hold": counts["보류"]["채택"],
        "missed_reference_adopt": counts["채택"]["제외"] + counts["채택"]["보류"],
        "excluded_reference_adopt": counts["채택"]["제외"],
        "hold_count": choices["보류"],
        "choice_agreement_count": sum(counts[c][c] for c in CHOICES),
        "invalid_pairs": invalid, "errors": dict(errors), "inference_seconds": round(elapsed, 4),
    }


def validate_references(rows, inputs, input_path):
    indexed(rows)
    refs = reference_contract.validate_references(rows, inputs, input_path)
    for reference in refs.values():
        label = reference.get("expected_label", reference.get("label"))
        time = reference.get("expected_time_status", reference.get("time_status"))
        if reference["expected_selection"] == "채택":
            require(label in ("직접관련", "간접관련") and time == "신규", "Adopted reference is not a current related event")
        if label == "판단보류":
            require(reference["expected_selection"] == "보류", "Uncertain reference must abstain")
    return refs


def load_baseline(folder, inputs, input_path):
    return baseline_contract.load_arm(folder, inputs, input_path, baseline)


def replay_linear_scores(vectors, coefficients, intercept):
    """Recompute finite float64 linear scores from stored float32 features.

    This runs no encoder or model loading. It checks only the frozen numeric
    scorer; full-text feature coverage needs its separate per-row provenance.
    """
    import numpy as np

    require(isinstance(vectors, np.ndarray) and vectors.dtype == np.dtype("float32")
            and vectors.ndim == 2 and vectors.shape[0] > 0 and vectors.shape[1] == 768,
            "Features must be a nonempty float32 [N,768] array")
    require(bool(np.isfinite(vectors).all()), "Nonfinite stored features")
    coefficient_array = np.asarray(coefficients)
    require(coefficient_array.shape in ((768,), (1, 768))
            and coefficient_array.dtype.kind in "fi", "Expected 768 numeric coefficients")
    coefficient_array = coefficient_array.astype(np.float64).reshape(768)
    require(bool(np.isfinite(coefficient_array).all()), "Nonfinite coefficients")
    if isinstance(intercept, list):
        require(len(intercept) == 1, "Expected one intercept")
        intercept = intercept[0]
    finite_number(intercept, "intercept")
    logits = vectors.astype(np.float64) @ coefficient_array + float(intercept)
    require(bool(np.isfinite(logits).all()), "Nonfinite reproduced linear values")
    scores = np.empty(logits.shape, dtype=np.float64)
    positive = logits >= 0
    scores[positive] = 1.0 / (1.0 + np.exp(-logits[positive]))
    negative_exp = np.exp(logits[~positive])
    scores[~positive] = negative_exp / (1.0 + negative_exp)
    require(bool(np.isfinite(scores).all()), "Nonfinite reproduced scores")
    return scores


def snapshot(path, fingerprints):
    path = Path(path).resolve()
    digest = base.sha(path)
    if str(path) in fingerprints:
        require(fingerprints[str(path)] == digest, "Dependency changed: " + str(path))
    fingerprints[str(path)] = digest
    return digest


def pinned_json(path, expected, fingerprints):
    require(snapshot(path, fingerprints) == expected, "Pinned JSON bytes changed: " + str(path))
    return read_json(Path(path).read_text(encoding="utf-8-sig"))


def load_features(path, expected, count, fingerprints):
    import numpy as np

    require(snapshot(path, fingerprints) == expected, "Feature cache hash differs")
    with np.load(path, allow_pickle=False) as data:
        require(set(data.files) == {"vectors"}, "Unexpected feature cache members")
        vectors = data["vectors"]
    require(vectors.dtype == np.dtype("float32") and vectors.shape == (count, 768)
            and bool(np.isfinite(vectors).all()), "Invalid feature dtype, shape or values")
    return vectors


def validate_coverage(coverage, count):
    fields = {"total_content_tokens", "covered_content_tokens", "windows", "target_tokens", "max_window_tokens"}
    require(isinstance(coverage, list) and len(coverage) == count, "Coverage rows differ")
    for row in coverage:
        require(isinstance(row, dict) and set(row) == fields
                and all(type(v) is int and v > 0 for v in row.values()), "Invalid coverage integers")
        require(row["total_content_tokens"] == row["covered_content_tokens"], "Article token coverage is incomplete")
        require(row["target_tokens"] <= 64 and row["target_tokens"] < row["max_window_tokens"] <= 256,
                "Invalid target/window token size")
        require(row["windows"] <= row["total_content_tokens"]
                <= row["windows"] * (256 - row["target_tokens"]), "Impossible token/window counts")


def validate_selection(selection):
    require(isinstance(selection, dict) and set(selection) == {
        "C", "adopt_threshold", "exclude_threshold", "adopt_enabled", "exclude_enabled"}, "Invalid selection fields")
    require(type(selection["C"]) in (int, float) and selection["C"] in (.1, 1., 10.), "Unknown regularization C")
    selected(.5, selection["exclude_threshold"], selection["adopt_threshold"],
             adopt_enabled=selection["adopt_enabled"], exclude_enabled=selection["exclude_enabled"])


def dev_thresholds(labels, scores):
    """Independent implementation of the predeclared dev-only grid rules."""
    adopts, excludes = [], []
    for boundary in (i / 100 for i in range(50, 100, 5)):
        tp = sum(y == 1 and score >= boundary for y, score in zip(labels, scores))
        fp = sum(y == 0 and score >= boundary for y, score in zip(labels, scores))
        if tp >= 3 and fp == 0:
            adopts.append((tp, boundary))
    for boundary in (i / 100 for i in range(5, 50, 5)):
        tn = sum(y == 0 and score <= boundary for y, score in zip(labels, scores))
        fn = sum(y == 1 and score <= boundary for y, score in zip(labels, scores))
        if tn >= 3 and fn == 0:
            excludes.append((tn, -boundary))
    return {"adopt_enabled": bool(adopts), "exclude_enabled": bool(excludes),
            "adopt_threshold": max(adopts)[1] if adopts else 1.,
            "exclude_threshold": -max(excludes)[1] if excludes else 0.}


def body_hash(text):
    return hashlib.sha256(" ".join(unicodedata.normalize("NFC", text).split()).encode()).hexdigest()


def validate_seed(manifest, sources, eval_inputs, fingerprints):
    path = Path(manifest["seed_manifest_path"]).resolve()
    seed = pinned_json(path, manifest["seed_manifest_sha256"], fingerprints)
    require(seed["schema"] == "relevance-classifier-seed-data-1" and seed["status"] == "frozen"
            and seed["independent_human_gold"] is False, "Seed provenance differs")
    require(same(seed["label_mapping"], LABEL_MAPPING), "Seed label mapping differs")
    ppath = path.parent / "provenance.jsonl"
    require(snapshot(ppath, fingerprints) == seed["files"]["provenance.jsonl"]["sha256"], "Seed provenance bytes differ")
    provenance_rows = read_rows(ppath)
    provenance = {r["sample_id"]: r for r in provenance_rows}
    require(len(provenance) == len(provenance_rows) == seed["rows"], "Seed provenance coverage differs")
    for name in sources:
        require(Path(manifest["source_paths"][name]).resolve() == path.parent / (name + ".jsonl")
                and manifest["source_sha256"][name] == seed["files"][name + ".jsonl"]["sha256"], "Seed training source binding differs")
    # Source-chain bytes are checked without using AI rationales or human verdicts as features.
    for source in seed.get("source_files", {}).values():
        require(snapshot(source["path"], fingerprints) == source["sha256"], "Original seed source bytes differ")
    eval_ids = {r["article_id"] for r in eval_inputs.values()}
    eval_bodies = {body_hash(r["body"]) for r in eval_inputs.values()}
    eval_urls = {canonicalize_url(r.get("source_url")) for r in eval_inputs.values()} - {None}
    identities = {}
    all_samples = set()
    for part in ("train", "dev"):
        ids, bodies, urls, groups = set(), set(), set(), set()
        for row in sources[part + "_inputs"]:
            require(set(row) == {"sample_id", "title", "body", "company_name"}
                    and all(isinstance(v, str) and v.strip() for v in row.values()), "Seed feature whitelist differs")
            sample_id = row["sample_id"]
            require(sample_id in provenance and sample_id not in all_samples, "Seed sample mapping missing/duplicated")
            all_samples.add(sample_id)
            p = provenance[sample_id]
            require(p["split"] == part and p["review_mode"] == "ai_assisted_selection"
                    and p["independent_human_gold"] is False, "Seed split/review provenance differs")
            require(p["features_sha256"] == hashlib.sha256(base.canonical(row).encode()).hexdigest()
                    and p["normalized_body_sha256"] == body_hash(row["body"]), "Seed feature/body hash differs")
            require(isinstance(p["article_id"], str) and p["article_id"] and isinstance(p["ticker"], str) and p["ticker"], "Missing seed source identity")
            require(isinstance(p["canonical_urls"], list) and all(isinstance(u, str) and u for u in p["canonical_urls"]), "Invalid seed URLs")
            ids.add(p["article_id"]); bodies.add(p["normalized_body_sha256"]); urls.update(p["canonical_urls"]); groups.add(p["group_id"])
        require(not ids & eval_ids and not bodies & eval_bodies and not urls & eval_urls,
                "Fresh evaluation identity/body/URL occurs in classifier development")
        identities[part] = (ids, bodies, urls, groups)
    require(all_samples == set(provenance), "Unaccounted seed provenance rows")
    require(all(not one & two for one, two in zip(identities["train"], identities["dev"])), "Train/dev transitive identity overlap")
    return provenance


def validate_training(protocol, eval_inputs, fingerprints):
    import numpy as np

    manifest_path = Path(protocol["training_manifest_path"]).resolve()
    manifest = pinned_json(manifest_path, protocol["training_manifest_sha256"], fingerprints)
    require(manifest["schema"] == "relevance-classifier-training-1" and manifest["complete"] is True,
            "Training artifact is not complete")
    require(manifest["fresh_evaluation_seen"] is False and manifest["human_gold"] is False
            and manifest["label_origin"] == "ai_assisted_user_selection_not_independent_gold",
            "Training supervision/evaluation provenance differs")
    for field in ("code_hashes", "feature_protocol", "encoder", "selection", "threshold_source", "scorer_contract"):
        require(same(manifest[field], protocol[field]), "Train/predict contract differs: " + field)
    model_path = Path(protocol["model_artifact_path"]).resolve()
    require(model_path == manifest_path.parent / "model.json", "Model artifact is not in frozen training directory")
    require(manifest["model_artifact_sha256"] == protocol["model_artifact_sha256"], "Training model hash differs")
    model = pinned_json(model_path, protocol["model_artifact_sha256"], fingerprints)
    require(set(model) == {"classes", "coef", "intercept"} and same(model["classes"], [0, 1]), "Invalid binary linear model")
    source_fields = {"train_inputs", "dev_inputs", "train_labels", "dev_labels"}
    require(set(manifest["source_paths"]) == set(manifest["source_sha256"]) == source_fields, "Training sources differ")
    sources = {}
    for name in sorted(source_fields):
        path = Path(manifest["source_paths"][name])
        require(snapshot(path, fingerprints) == manifest["source_sha256"][name], "Training input/label hash differs")
        sources[name] = read_rows(path)
    validate_seed(manifest, sources, eval_inputs, fingerprints)
    total = 0
    sample_sets = []
    for part in ("train", "dev"):
        rows, labels = sources[part + "_inputs"], sources[part + "_labels"]
        require(type(manifest[part + "_rows"]) is int and len(rows) == len(labels) == manifest[part + "_rows"],
                "Training/dev count differs")
        ids = [r.get("sample_id") for r in rows]
        require(all(isinstance(x, str) and x for x in ids) and len(set(ids)) == len(ids)
                and ids == [r.get("sample_id") for r in labels], "Training input/label identity alignment differs")
        require(all(isinstance(r.get("label"), str) and r["label"] in LABEL_MAPPING for r in labels)
                and {r["label"] for r in labels} == set(LABEL_MAPPING), "Both original Korean binary choices required")
        sample_sets.append(set(ids))
        total += len(rows)
    require(not sample_sets[0] & sample_sets[1], "Train/dev identity overlap")
    vectors = load_features(manifest_path.parent / "features.npz", manifest["feature_cache_sha256"], total, fingerprints)
    validate_coverage(manifest["coverage"], total)
    replayed = replay_linear_scores(vectors, model["coef"], model["intercept"])[manifest["train_rows"]:]
    dev_path = manifest_path.parent / "dev_results.jsonl"
    snapshot(dev_path, fingerprints)
    dev_rows = read_rows(dev_path)
    require(len(dev_rows) == len(replayed), "Dev score coverage differs")
    labels = [LABEL_MAPPING[r["label"]] for r in sources["dev_labels"]]
    for row, source_row, label, score in zip(dev_rows, sources["dev_inputs"], labels, replayed):
        require(row["sample_id"] == source_row["sample_id"] and type(row["label"]) is int and row["label"] == label,
                "Stored dev score identity/label differs")
        finite_number(row["score"], "dev_score", 0, 1)
        require(abs(row["score"] - float(score)) <= 1e-12, "Stored dev score differs from linear replay")
        selection = manifest["selection"]
        require(row["effective_selection"] == selected(float(score), selection["exclude_threshold"], selection["adopt_threshold"],
                adopt_enabled=selection["adopt_enabled"], exclude_enabled=selection["exclude_enabled"]), "Stored dev decision differs")
    require(same({k: v for k, v in manifest["selection"].items() if k != "C"}, dev_thresholds(labels, replayed)),
            "Thresholds are not reproducible from development only")
    require(same(manifest["C_grid"], [.1, 1., 10.])
            and same(manifest["adopt_grid"], [i / 100 for i in range(50, 100, 5)])
            and same(manifest["exclude_grid"], [i / 100 for i in range(5, 50, 5)]), "Development selection grid changed")
    trials = manifest["trials"]
    require(isinstance(trials, list) and len(trials) == 3 and sorted(r["C"] for r in trials) == [.1, 1., 10.], "C trial coverage differs")
    for trial in trials:
        finite_number(trial["dev_balanced_log_loss"], "dev_balanced_log_loss", 0)
    chosen = min(trials, key=lambda r: (r["dev_balanced_log_loss"], r["C"]))
    require(chosen["C"] == manifest["selection"]["C"], "C was not chosen by frozen dev criterion")
    # sklearn's float64 binary log-loss clipping, independently computed.
    p = np.clip(replayed, np.finfo(np.float64).eps, 1 - np.finfo(np.float64).eps)
    y = np.asarray(labels)
    loss = -float(.5 * (np.log(p[y == 1]).mean() + np.log1p(-p[y == 0]).mean()))
    require(abs(loss - chosen["dev_balanced_log_loss"]) <= 1e-10, "Selected model's dev loss differs")
    return model, manifest, {"train_rows": manifest["train_rows"], "dev_rows": manifest["dev_rows"],
        "selected_C": chosen["C"], "replayed_dev_balanced_log_loss": loss,
        "dev_thresholds_reproduced": True, "fresh_identity_body_url_overlap": 0,
        "all_C_models_refit": False}


def load_classifier(folder, inputs, input_path, fingerprints):
    require(not (folder / "inference.lock").exists(), "Classifier inference is active")
    protocol_path = folder / "protocol.json"
    snapshot(protocol_path, fingerprints)
    protocol = read_json(protocol_path.read_text(encoding="utf-8-sig"))
    require(protocol["schema"] == "relevance-classifier-prediction-1" and protocol["complete"] is True,
            "Classifier prediction is incomplete")
    require(protocol["code_hashes"] == classifier.code_hashes()
            and same(protocol["feature_protocol"], classifier.FEATURE_PROTOCOL), "Classifier dependency or feature protocol differs")
    require(protocol["input_sha256"] == base.sha(input_path) and type(protocol["rows"]) is int
            and protocol["rows"] == len(inputs), "Classifier input hash/count differs")
    require(protocol["threshold_source"] == "development_only"
            and protocol["scorer_contract"] == "sigmoid_linear_float64_not_calibrated"
            and protocol["score_is_calibrated_probability"] is False, "Classifier score/threshold contract differs")
    validate_selection(protocol["selection"])
    model, training, training_audit = validate_training(protocol, inputs, fingerprints)
    vectors = load_features(folder / "features.npz", protocol["feature_cache_sha256"], len(inputs), fingerprints)
    reproduced = replay_linear_scores(vectors, model["coef"], model["intercept"])
    result_path = folder / "results.jsonl"
    snapshot(result_path, fingerprints)
    records = indexed(read_rows(result_path))
    require(list(records) == list(inputs), "Classifier results must preserve every input pair and feature-row order")
    fields = {"article_id", "ticker", "input_sha256", "score", "effective_selection", "validation_errors", "elapsed_seconds", "coverage"}
    selection = protocol["selection"]
    for (key, row), score in zip(records.items(), reproduced):
        require(set(row) == fields, "Unexpected classifier result fields")
        require(row["input_sha256"] == hashlib.sha256(base.canonical(inputs[key]).encode()).hexdigest(), "Classifier row input hash differs")
        require(row["validation_errors"] == [], "Classifier producer aborts on errors; complete run cannot contain partial errors")
        finite_number(row["score"], "score", 0, 1)
        finite_number(row["elapsed_seconds"], "elapsed_seconds", 0)
        require(abs(row["score"] - float(score)) <= 1e-12, "Classifier score differs from frozen features/model")
        require(row["effective_selection"] == selected(float(score), selection["exclude_threshold"], selection["adopt_threshold"],
                adopt_enabled=selection["adopt_enabled"], exclude_enabled=selection["exclude_enabled"]), "Classifier threshold decision differs")
        validate_coverage([row["coverage"]], 1)
    return records, protocol, training_audit


def evaluate(args):
    require(not args.out.exists(), "Use a new evaluation directory; previous results are immutable")
    fingerprints = {}
    dependencies = [args.inputs, *args.references, Path(__file__), Path(classifier.__file__), Path(base.__file__),
                    Path(reference_contract.__file__), Path(baseline_contract.__file__),
                    Path(__file__).resolve().parent / "lake_news_normalize.py"]
    for path in dependencies:
        snapshot(path, fingerprints)
    inputs = indexed(read_rows(args.inputs))
    for row in inputs.values():
        base.validate_input(row)
        require(set(row) == {"article_id", "ticker", "company_name", "published_at", "title", "body",
                            "company_context", "source_url", "source_split", "case_kind"}, "Evaluation input whitelist differs")
        require(row.get("company_context") is None, "This comparison requires identical null-context inputs")
    predicted, protocol, training_audit = load_classifier(args.classifier_run, inputs, args.inputs, fingerprints)
    for filename in ("protocol.json", "results.jsonl"):
        snapshot(args.baseline_run / filename, fingerprints)
    original, baseline_protocol = load_baseline(args.baseline_run, inputs, args.inputs)
    refs = validate_references([row for path in args.references for row in read_rows(path)], inputs, args.inputs)
    constants = {choice: measure({key: {"effective_selection": choice, "validation_errors": [], "elapsed_seconds": 0.0}
                                  for key in refs}, refs) for choice in CHOICES}
    summary = {"schema": "relevance-classifier-evaluation-1", "pairs": len(inputs), "denominator_all_pairs": len(inputs),
        "baseline": measure(original, refs), "classifier": measure(predicted, refs),
        "all_constant_baselines": constants, "reference_counts": {choice: sum(r["expected_selection"] == choice for r in refs.values()) for choice in CHOICES},
        "input_sha256": base.sha(args.inputs), "human_gold": False, "reference_type": "independent_ai_review",
        "training_label_origin": "ai_assisted_user_selection_not_independent_gold", "score_is_calibrated_probability": False,
        "threshold_source": "development_only", "selection": protocol["selection"], "training_audit": training_audit,
        "gnn_training_performed": False, "population_accuracy_claim": False, "actual_stock_effect_evaluated": False,
        "source_sha256": fingerprints, "baseline_protocol": baseline_protocol, "classifier_protocol": protocol,
        "limitations": ["All inputs remain in the denominator, including invalid LLM outputs and all abstentions.",
            "AI-reference agreement is not independent human accuracy; the classifier seed uses assisted user choices.",
            "C and thresholds share a small development set; dev constraints are not population precision guarantees.",
            "Frozen feature coverage metadata is checked arithmetically; this evaluator does not rerun tokenization or embeddings.",
            "Stored-score replay verifies the linear scorer, not the semantic truth of the encoder or annotations.",
            "Timing sums per-row processing; classifier encoder load/file validation and training are excluded, so this is not end-to-end wall-clock benchmarking.",
            "Classifier and LLM change architecture and inference procedure together; this is not an isolated prompting experiment."]}
    pairs = [{"article_id": key[0], "ticker": key[1], "baseline": original[key], "classifier": predicted[key],
              "reference": refs[key], "choice_changed": original[key]["effective_selection"] != predicted[key]["effective_selection"]} for key in inputs]
    summary["changed_choices"] = sum(r["choice_changed"] for r in pairs)
    for path, expected in fingerprints.items():
        require(base.sha(path) == expected, "Dependency changed while evaluating: " + path)
    require(classifier.code_hashes() == protocol["code_hashes"] and baseline.code_hashes() == baseline_protocol["code_hashes"],
            "Runtime changed during evaluation")
    require(not (args.baseline_run / "inference.lock").exists() and not (args.classifier_run / "inference.lock").exists(), "Inference restarted")
    args.out.mkdir(parents=True, exist_ok=False)
    base.write_json(args.out / "summary.json", summary)
    (args.out / "pair_comparison.jsonl").write_text("".join(base.canonical(r) + "\n" for r in pairs), encoding="utf8")
    print(base.canonical(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "classifier-run", "baseline-run", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--references", type=Path, nargs="+", required=True)
    evaluate(parser.parse_args())
