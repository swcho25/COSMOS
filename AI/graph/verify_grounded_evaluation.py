"""Audit saved GNN results without training or selecting another checkpoint.

Recompute confusion metrics, trading-session differences, HAC errors and Holm
adjustment from immutable predictions. Reload every seed on CPU for one fixed
test article and compare the saved GPU predictions. This is numerical and
provenance validation, not another model selection pass or a fresh holdout.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import gc
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def close(actual, expected, tolerance=2e-7):
    assert math.isfinite(float(actual)) and math.isfinite(float(expected))
    assert abs(float(actual) - float(expected)) <= tolerance, (actual, expected)


def independent_priors(frame, labels, magnitude, indices):
    train = frame.split.eq("train").to_numpy()
    selections = {"global": train}
    for field in ("market", "ticker"):
        for value in frame.loc[train, field].unique():
            selections[(field, value)] = train & frame[field].eq(value).to_numpy()
    lookup = {}
    for key, mask in selections.items():
        counts = np.stack([(labels[mask] == c).sum(axis=0) for c in range(3)], axis=-1)
        lookup[key] = (counts / counts.sum(axis=-1, keepdims=True), np.median(magnitude[mask], axis=0))
    result = {}
    for name, field in (("market_prior", "market"), ("ticker_prior", "ticker")):
        chosen = [lookup.get((field, frame.iloc[i][field]),
                  lookup.get(("market", frame.iloc[i].market), lookup["global"])) for i in indices]
        result[name] = (np.array([row[0] for row in chosen], dtype=np.float32),
                        np.array([row[1] for row in chosen], dtype=np.float32), np.ones(len(indices), dtype=bool))
    global_probability, global_magnitude = lookup["global"]
    result["majority"] = (np.tile(np.eye(3)[global_probability.argmax(axis=-1)], (len(indices), 1, 1)),
                          np.tile(global_magnitude, (len(indices), 1)), np.ones(len(indices), dtype=bool))
    return result


def prediction_file(path, indices):
    with np.load(path) as saved:
        assert np.array_equal(saved["pair_indices"], indices)
        probability, magnitude, covered = (saved[k].copy() for k in ("probabilities", "magnitude", "covered"))
    assert probability.shape == (len(indices), 2, 3) and magnitude.shape == (len(indices), 2)
    assert covered.dtype == bool and covered.shape == (len(indices),)
    assert np.isfinite(probability).all() and np.isfinite(magnitude).all()
    assert ((probability >= 0) & (probability <= 1)).all() and ((magnitude >= 0) & (magnitude <= 1)).all()
    np.testing.assert_allclose(probability[covered].sum(axis=-1), 1, atol=1e-6)
    assert not probability[~covered].any() and not magnitude[~covered].any()
    return probability, magnitude, covered


def cpu_reload(dataset, output, protocol, frame):
    import torch
    from news_impact_data import graph_arrays, node_features
    from target_news_model import TargetNewsGAT

    torch.set_num_threads(4)
    documents = [json.loads(line) for line in (dataset / "articles.jsonl").open(encoding="utf-8")]
    companies = pd.read_json(dataset / "companies.json", dtype={"ticker": str})
    ticker_index = {ticker: i for i, ticker in enumerate(companies.ticker)}
    mapping = np.load(dataset / "feature_mapping.npy", mmap_mode="r")
    mentions = np.load(dataset / "mentions.npy", mmap_mode="r")
    text = np.load(dataset / "target_text.npy", mmap_mode="r")
    sentiment = np.load(dataset / "target_sentiment.npy", mmap_mode="r")
    indices = np.flatnonzero(frame.split.eq("test"))
    checked = []
    for arm in protocol["arms"]:
        graphs = read_json(dataset / ("graphs_base.json" if arm == "no_new_graph" else "graphs_candidate.json"))
        for seed in protocol["seeds"]:
            folder = output / "models" / arm / f"seed{seed}"
            probability, magnitude, covered = prediction_file(folder / "test_predictions.npz", indices)
            position = int(np.flatnonzero(covered)[0])
            pair = frame.iloc[indices[position]]
            article_index, node = int(pair.article), ticker_index[pair.ticker]
            document = documents[article_index]
            graph = graphs[str(document["graph"])]
            assert pd.Timestamp(graph["available_at"]) < pd.Timestamp(document["available_at"])
            edges, attributes = graph_arrays(graph, companies.ticker.tolist())
            feature_indices = np.asarray(mapping[article_index])
            text_input, sentiment_input = np.asarray(text[feature_indices]), np.asarray(sentiment[feature_indices])
            if arm == "no_news":
                text_input, sentiment_input = np.zeros_like(text_input), np.zeros_like(sentiment_input)
            state = torch.load(folder / "model.pt", map_location="cpu", weights_only=False)
            assert state["training_protocol_sha256"] == file_sha(output / "training_protocol.json")
            assert state["mode"] == arm and state["selection"]["seed"] == seed
            assert state["selection"]["best_epoch"] == read_json(folder / "fit_complete.json")["best_epoch"]
            model = TargetNewsGAT(**state["config"]).eval()
            model.load_state_dict(state["state_dict"], strict=True)
            with torch.inference_mode():
                logits, values = model(torch.tensor(text_input[None]), torch.tensor(sentiment_input[None]),
                    torch.tensor(np.asarray(mentions[article_index])[None]),
                    torch.tensor(node_features(companies, graph)), torch.tensor(edges), torch.tensor(attributes))
                actual_probability, actual_magnitude = logits.softmax(-1)[0, node].numpy(), values[0, node].numpy()
            np.testing.assert_allclose(actual_probability, probability[position], rtol=3e-5, atol=3e-5)
            np.testing.assert_allclose(actual_magnitude, magnitude[position], rtol=3e-5, atol=3e-5)
            checked.append({"arm": arm, "seed": seed, "article": article_index, "ticker": pair.ticker,
                            "max_probability_difference": float(abs(actual_probability - probability[position]).max()),
                            "max_magnitude_difference": float(abs(actual_magnitude - magnitude[position]).max())})
            del model, state
            gc.collect()
    return checked


def verify(dataset, output, reload_models=True, *, rank_correction_path=None):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    receipt = read_json(output / "evaluation_complete.json")
    assert receipt["complete"] is True and receipt["fits"] == 9
    protocol = read_json(output / "training_protocol.json")
    assert receipt["training_protocol_sha256"] == file_sha(output / "training_protocol.json")
    assert Path(protocol["dataset"]).resolve() == dataset
    for relative, digest in receipt["files"].items():
        path = (output / relative).resolve()
        assert path.is_relative_to(output) and file_sha(path) == digest
    for name, digest in protocol["source_hashes"].items():
        assert file_sha(dataset / name) == digest
    for name, digest in protocol["code_hashes"].items():
        assert file_sha(Path(__file__).parent / name) == digest
    frame = pd.read_parquet(dataset / "pairs.parquet")
    y = frame[["y_1d", "y_3d"]].to_numpy(dtype=np.float32)
    with np.load(dataset / "label_config.npz") as config:
        labels = np.ones(y.shape, dtype=int)
        labels[y > config["thresholds"]] = 2
        labels[y < -config["thresholds"]] = 0
        magnitude = np.minimum(abs(y) / config["scales"], 1)
    metrics = pd.read_csv(output / "metrics.csv")
    original_metrics = metrics.copy(deep=True)
    rank_corrections = []
    tests = pd.read_csv(output / "paired_hac.csv")
    expected_metrics = {(split, model, market, horizon) for split in ("val", "test", "legacy_val")
        for model in (*protocol["arms"], "majority", "market_prior", "ticker_prior")
        for market in ("KOSPI", "NASDAQ") for horizon in (1, 3)}
    metric_keys = list(metrics[["split", "model", "market", "horizon"]].itertuples(index=False, name=None))
    assert len(metric_keys) == len(expected_metrics) and set(metric_keys) == expected_metrics
    expected_tests = {(split, market, horizon, baseline, lag) for split in ("val", "test", "legacy_val")
        for market in ("KOSPI", "NASDAQ") for horizon in (1, 3)
        for baseline in ("no_news", "no_new_graph", "majority", "market_prior", "ticker_prior") for lag in (5, 20)}
    test_keys = list(tests[["split", "market", "horizon", "baseline", "lag"]].itertuples(index=False, name=None))
    assert len(test_keys) == len(expected_tests) and set(test_keys) == expected_tests
    mask = np.load(dataset / "direct_mask.npy", mmap_mode="r")
    companies = pd.read_json(dataset / "companies.json", dtype={"ticker": str})
    company_index = {ticker: i for i, ticker in enumerate(companies.ticker)}
    expected_coverage = mask[frame.article.to_numpy(), frame.ticker.map(company_index).to_numpy()]
    daily = {}
    summaries = []
    for split in ("val", "test", "legacy_val"):
        indices = np.flatnonzero(frame.split.eq(split))
        local = frame.iloc[indices]
        predictions = independent_priors(frame, labels, magnitude, indices)
        for arm in protocol["arms"]:
            outputs = []
            for seed in protocol["seeds"]:
                folder = output / "models" / arm / f"seed{seed}"
                marker = read_json(folder / "fit_complete.json")
                assert marker["training_protocol_sha256"] == receipt["training_protocol_sha256"]
                assert marker["seed"] == seed
                assert marker["model_sha256"] == file_sha(folder / "model.pt")
                history = read_json(folder / "history.json")
                best, best_epoch = math.inf, -1
                for row in history:
                    if row["val_loss"] < best - protocol["settings"]["min_delta"]:
                        best, best_epoch = row["val_loss"], row["epoch"]
                assert marker["best_epoch"] == best_epoch and marker["completed_epochs"] == len(history)
                close(marker["val_loss"], best)
                predictions_for_seed = prediction_file(folder / f"{split}_predictions.npz", indices)
                np.testing.assert_array_equal(predictions_for_seed[2], expected_coverage[indices])
                outputs.append(predictions_for_seed)
            saved = prediction_file(output / "models" / arm / f"{split}_ensemble.npz", indices)
            np.testing.assert_array_equal(saved[2], outputs[0][2])
            for other in outputs[1:]:
                np.testing.assert_array_equal(other[2], saved[2])
            np.testing.assert_allclose(saved[0], np.mean([r[0] for r in outputs], axis=0), atol=1e-7)
            np.testing.assert_allclose(saved[1], np.mean([r[1] for r in outputs], axis=0), atol=1e-7)
            predictions[arm] = saved
        for name, (probability, prediction_mag, covered) in predictions.items():
            prediction = probability.argmax(axis=-1)
            prediction[~covered] = -1
            for market in ("KOSPI", "NASDAQ"):
                select = local.market.eq(market).to_numpy()
                for horizon_index, horizon in enumerate((1, 3)):
                    correct = prediction[:, horizon_index] == labels[indices, horizon_index]
                    groups = defaultdict(list)
                    for day, value in zip(local.loc[select, "anchor_date"], correct[select]):
                        groups[day].append(int(value))
                    sessions = {day: sum(values) / len(values) for day, values in sorted(groups.items())}
                    daily[(split, name, market, horizon)] = sessions
                    confusion = np.array([[np.sum(select & (labels[indices, horizon_index] == true) &
                        (prediction[:, horizon_index] == predicted)) for predicted in (0, 1, 2, -1)] for true in (0, 1, 2)])
                    f1 = []
                    for k in range(3):
                        denominator = confusion[k, :].sum() + confusion[:, k].sum()
                        f1.append(2 * confusion[k, k] / denominator if denominator else 0.)
                    selected = metrics[(metrics.split == split) & (metrics.model == name) &
                        (metrics.market == market) & (metrics.horizon == horizon)]
                    assert len(selected) == 1
                    row = selected.iloc[0]
                    assert row.pairs == int(select.sum()) and row.covered_pairs == int((select & covered).sum())
                    assert row.abstentions == int((select & ~covered).sum()) and row.sessions == len(groups)
                    assert json.loads(row.confusion_true_rows_pred_columns) == confusion.tolist()
                    close(row.pair_accuracy, correct[select].mean())
                    close(row.session_accuracy, np.mean(list(sessions.values())))
                    close(row.macro_f1, np.mean(f1))
                    close(row.magnitude_mae_covered,
                          np.abs(prediction_mag[select & covered, horizon_index] - magnitude[indices[select & covered], horizon_index]).mean())
                    rank_groups = defaultdict(list)
                    for position in np.flatnonzero(select & covered):
                        rank_groups[local.iloc[position].news_id].append(position)
                    ranks = []
                    for positions in rank_groups.values():
                        actual_values = magnitude[indices[positions], horizon_index]
                        if len(positions) < 2 or np.ptp(actual_values) == 0:
                            continue
                        predicted_values = prediction_mag[positions, horizon_index]
                        if np.ptp(predicted_values) == 0:
                            ranks.append(0.)
                        else:
                            actual_rank = pd.Series(actual_values).rank(method="average").to_numpy()
                            predicted_rank = pd.Series(predicted_values).rank(method="average").to_numpy()
                            actual_rank -= actual_rank.mean()
                            predicted_rank -= predicted_rank.mean()
                            ranks.append(float((actual_rank @ predicted_rank) /
                                math.sqrt((actual_rank @ actual_rank) * (predicted_rank @ predicted_rank))))
                    expected_rank = float(np.mean(ranks)) if ranks else None
                    stored_rank = (float(row.within_article_magnitude_spearman)
                                   if pd.notna(row.within_article_magnitude_spearman) else None)
                    rank_matches = ((expected_rank is None and stored_rank is None) or
                        (expected_rank is not None and stored_rank is not None and
                         abs(expected_rank - stored_rank) <= 2e-7))
                    if row.rank_articles != len(ranks) or not rank_matches:
                        assert rank_correction_path is not None, (
                            "Stored rank report differs; preserve it and explicitly request a rank-only correction",
                            split, name, market, horizon, stored_rank, expected_rank)
                        rank_corrections.append({"split": split, "model": name, "market": market,
                            "horizon": horizon, "original_rank_articles": int(row.rank_articles),
                            "corrected_rank_articles": len(ranks), "original_spearman": stored_rank,
                            "corrected_spearman": expected_rank})
                        metrics.at[row.name, "rank_articles"] = len(ranks)
                        metrics.at[row.name, "within_article_magnitude_spearman"] = expected_rank
                    if split == "test":
                        summaries.append({"model": name, "market": market, "horizon": horizon,
                                          "pairs": int(row.pairs), "accuracy": float(row.pair_accuracy),
                                          "macro_f1": float(row.macro_f1)})
    for _, row in tests.iterrows():
        candidate = daily[(row.split, "candidate", row.market, row.horizon)]
        baseline = daily[(row.split, row.baseline, row.market, row.horizon)]
        assert candidate.keys() == baseline.keys()
        delta = np.array([value - baseline[day] for day, value in candidate.items()])
        mean = float(delta.mean())
        centered = delta - mean
        length = len(delta)
        assert row.sessions == length
        maximum_lag = min(int(row.lag), length - 1)
        autocovariance = np.correlate(centered, centered, mode="full")[length-1:length+maximum_lag] / length
        coefficients = np.r_[1., 2 * (1 - np.arange(1, maximum_lag+1) / (row.lag+1))]
        error = math.sqrt(max(0., float(autocovariance @ coefficients)) / length)
        pvalue = math.erfc(abs(mean) / (math.sqrt(2) * error)) if error else (1. if mean == 0 else 0.)
        close(row.accuracy_delta, mean, 1e-10)
        close(row.standard_error, error, 1e-10)
        close(row.p, pvalue, 1e-10)
        close(row.ci95_low, mean - 1.96 * error, 1e-10)
        close(row.ci95_high, mean + 1.96 * error, 1e-10)
    for lag in (5, 20):
        ordered = sorted((float(r.p), int(i)) for i, r in tests[tests.lag == lag].iterrows())
        previous = 0.
        for position, (pvalue, index) in enumerate(ordered):
            previous = min(1., max(previous, (len(ordered) - position) * pvalue))
            close(tests.loc[index, "holm_p"], previous, 1e-10)
    reloads = cpu_reload(dataset, output, protocol, frame) if reload_models else []
    corrected = None
    if rank_corrections:
        unchanged = [c for c in metrics.columns if c not in ("rank_articles", "within_article_magnitude_spearman")]
        pd.testing.assert_frame_equal(original_metrics[unchanged], metrics[unchanged])
        rank_correction_path = Path(rank_correction_path)
        assert not rank_correction_path.exists(), "Preserve earlier corrections"
        metrics.to_csv(rank_correction_path, index=False)
        corrected = {"path": str(rank_correction_path.resolve()), "sha256": file_sha(rank_correction_path),
                     "original_metrics_sha256": file_sha(output / "metrics.csv"),
                     "reason": "np.std(float32 constant vectors) can be nonzero from rounding; exact range identifies constants and average-rank correlation uses float64",
                     "policy": "constant target excluded; constant prediction scored zero; no model or classification metric change",
                     "changed_fields": ["rank_articles", "within_article_magnitude_spearman"],
                     "changes": rank_corrections}
    return {"checks_passed": True, "evaluation_manifest_sha256": file_sha(output / "evaluation_complete.json"),
            "verifier_sha256": file_sha(Path(__file__)), "metric_rows_recomputed": len(metrics),
            "paired_hac_rows_recomputed": len(tests), "cpu_seed_reloads": reloads, "test_metrics": summaries,
            "fresh_holdout": False, "semantic_label_accuracy_estimated": False, "model_selection_changed": False,
            "original_rank_report_fully_passed": not rank_corrections, "rank_report_correction": corrected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skip-model-reload", action="store_true")
    parser.add_argument("--repair-rank-report", action="store_true",
                        help="Preserve original metrics; create a separate correction of rank fields only if needed")
    args = parser.parse_args()
    assert not args.out.exists(), "Keep previous reports; choose a new output file"
    correction = args.out.with_name(args.out.stem + ".corrected_metrics.csv") if args.repair_rank_report else None
    report = verify(args.dataset, args.run, not args.skip_model_reload, rank_correction_path=correction)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ("test_metrics", "cpu_seed_reloads", "rank_report_correction")}))


if __name__ == "__main__":
    main()
