"""Fictional CPU evaluator regressions; no model or evaluation-article access."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_relevance_classifier as evaluator
import local_news_relevance as base


class ClassifierEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.directory = self.parent / (".classifier-evaluator-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.inputs = [{"article_id": "fiction-" + str(i), "ticker": "FICT", "company_name": "가상회사",
                        "published_at": "2021-05-20", "title": "가상회사 계약 발표",
                        "body": "가상회사는 오늘 계약을 발표했다. 이것은 가상 평가 자료다.",
                        "company_context": None, "case_kind": "synthetic", "source_split": "diagnostic",
                        "source_url": "fiction://" + str(i)} for i in range(5)]
        self.input_path = self.directory / "inputs.jsonl"
        self.write_rows(self.input_path, self.inputs)
        self.index = evaluator.indexed(self.inputs)
        self.refs = [{"article_id": row["article_id"], "ticker": row["ticker"],
                      "input_sha256": self.digest(row), "human_gold": False,
                      "reference_type": "independent_ai_review", "model_outputs_seen": False,
                      "full_article_text_read": True, "expected_selection": choice,
                      "expected_label": "직접관련", "expected_time_status": "신규",
                      "evidence_sentences": [row["body"]], "reason": "가상 참고판정"}
                     for row, choice in zip(self.inputs, ("채택", "채택", "제외", "보류", "채택"))]

    def tearDown(self):
        if self.directory.resolve().parent != self.parent or not self.directory.name.startswith(".classifier-evaluator-"):
            raise RuntimeError("Cleanup escaped test workspace")
        shutil.rmtree(self.directory)

    @staticmethod
    def write_rows(path, rows):
        path.write_text("".join(base.canonical(row) + "\n" for row in rows), encoding="utf8")

    @staticmethod
    def digest(row):
        return hashlib.sha256(base.canonical(row).encode()).hexdigest()

    def baseline_fixture(self):
        folder = self.directory / "baseline"
        folder.mkdir(exist_ok=True)
        runner = evaluator.baseline
        protocol = {"schema": "relevance-two-stage-1", "policy": "two_stage", "code_hashes": runner.code_hashes(),
                    "input_sha256": base.sha(self.input_path), "rows": len(self.inputs),
                    "model_repo": "fiction/model", "model_revision": "a" * 40, "receipt_sha256": "b" * 64,
                    "prompt_sha256": "c" * 64, "attn_implementation": "sdpa_explicit_kv",
                    "quantization": "NF4 double quantization bf16", "seed_policy": evaluator.baseline_contract.SEED,
                    "verification_seed_policy": evaluator.baseline_contract.VERIFY_SEED,
                    "stage2_receives_stage1_output": False, "retry_policy": "none", "truncation": False,
                    "max_input_tokens": 6144, "max_new_tokens": 768, "gpu": "fiction GPU",
                    "versions": {key: "fiction-version" for key in ("torch", "transformers", "bitsandbytes", "accelerate")},
                    "do_sample": True, "temperature": .7, "top_p": .8, "top_k": 20}
        base.write_json(folder / "protocol.json", protocol)
        records = []
        for i, row in enumerate(self.inputs):
            raw = "{" if i == 4 else base.canonical({"decision": "hold", "evidence_ids": []})
            prediction, errors = runner.policy.parse(raw, row)
            stage = {"raw_output": raw, "prediction": prediction, "validation_errors": errors,
                     "generation_complete": True, "input_tokens": 100, "output_tokens": 20,
                     "elapsed_seconds": 1.0, "peak_cuda_allocated_bytes": 1024}
            stages = {"screen": stage, "verify": None}
            record = {"article_id": row["article_id"], "ticker": row["ticker"], "case_kind": row["case_kind"],
                      "source_split": row["source_split"], "policy": "two_stage", "input_sha256": self.digest(row),
                      "stages": stages, "validation_errors": runner.combined_errors(stages),
                      "effective_selection": runner.final_selection(stages), "peak_cuda_allocated_bytes": 1024,
                      "elapsed_seconds": 1.0, "input_tokens": 100, "output_tokens": 20}
            records.append(record)
        self.write_rows(folder / "results.jsonl", records)
        return folder, protocol, records

    def classifier_fixture(self):
        import math
        import numpy as np

        seed, trained, run = (self.directory / name for name in ("seed", "trained", "classifier"))
        for folder in (seed, trained, run):
            folder.mkdir(exist_ok=True)
        source = {}
        for part, count in (("train", 8), ("dev", 6)):
            source[part + "_inputs"] = [{"sample_id": part + str(i), "title": "가상 개발 계약",
                                         "body": "개발 전용 독립 본문 " + part + str(i), "company_name": "가상회사"}
                                        for i in range(count)]
            source[part + "_labels"] = [{"sample_id": part + str(i), "label": "채택" if i >= count // 2 else "제외"} for i in range(count)]
        paths = {name: seed / (name + ".jsonl") for name in source}
        for name, values in source.items():
            self.write_rows(paths[name], values)
        provenance = [{"sample_id": r["sample_id"], "article_id": r["sample_id"], "ticker": "FICT",
                       "split": part, "features_sha256": self.digest(r), "normalized_body_sha256": evaluator.body_hash(r["body"]),
                       "canonical_urls": ["fiction://seed/" + r["sample_id"]], "group_id": r["sample_id"],
                       "review_mode": "ai_assisted_selection", "independent_human_gold": False}
                      for part in ("train", "dev") for r in source[part + "_inputs"]]
        self.write_rows(seed / "provenance.jsonl", provenance)
        seed_manifest = {"schema": "relevance-classifier-seed-data-1", "status": "frozen", "independent_human_gold": False,
                         "rows": 14, "label_mapping": {"제외": 0, "채택": 1},
                         "files": {p.name: {"sha256": base.sha(p)} for p in [*paths.values(), seed / "provenance.jsonl"]}}
        base.write_json(seed / "manifest.json", seed_manifest)
        model = {"classes": [0, 1], "coef": [1.0] + [0.0] * 767, "intercept": 0.0}
        base.write_json(trained / "model.json", model)
        vectors = np.zeros((14, 768), dtype=np.float32)
        vectors[8:, 0] = [math.log(p / (1 - p)) for p in (.1, .15, .2, .8, .85, .9)]
        np.savez(trained / "features.npz", vectors=vectors)
        scores = evaluator.replay_linear_scores(vectors, model["coef"], model["intercept"])[8:]
        yd = [evaluator.LABEL_MAPPING[r["label"]] for r in source["dev_labels"]]
        selection = {"C": 1.0, **evaluator.dev_thresholds(yd, scores)}
        dev = [{"sample_id": row["sample_id"], "label": label, "score": float(score),
                "effective_selection": evaluator.selected(float(score), selection["exclude_threshold"], selection["adopt_threshold"],
                    adopt_enabled=selection["adopt_enabled"], exclude_enabled=selection["exclude_enabled"])}
               for row, label, score in zip(source["dev_inputs"], yd, scores)]
        self.write_rows(trained / "dev_results.jsonl", dev)
        loss = -.5 * (float(np.log(scores[3:]).mean()) + float(np.log1p(-scores[:3]).mean()))
        cover = {"total_content_tokens": 100, "covered_content_tokens": 100, "windows": 1,
                 "target_tokens": 4, "max_window_tokens": 107}
        encoder = {"model_repo": "fiction/encoder", "model_revision": "e" * 40, "model_files": {"model.safetensors": "f" * 64},
                   "pretrained_sha256": "d" * 64, "bundle_manifest_sha256": "c" * 64}
        manifest = {"schema": "relevance-classifier-training-1", "complete": True,
                    "fresh_evaluation_seen": False, "human_gold": False,
                    "label_origin": "ai_assisted_user_selection_not_independent_gold",
                    "code_hashes": evaluator.classifier.code_hashes(), "feature_protocol": evaluator.classifier.FEATURE_PROTOCOL,
                    "encoder": encoder, "selection": selection, "threshold_source": "development_only",
                    "scorer_contract": "sigmoid_linear_float64_not_calibrated", "train_rows": 8, "dev_rows": 6,
                    "source_paths": {name: str(path) for name, path in paths.items()},
                    "source_sha256": {name: base.sha(path) for name, path in paths.items()},
                    "seed_manifest_path": str(seed / "manifest.json"), "seed_manifest_sha256": base.sha(seed / "manifest.json"),
                    "model_artifact_sha256": base.sha(trained / "model.json"), "feature_cache_sha256": base.sha(trained / "features.npz"),
                    "coverage": [cover.copy() for _ in range(14)], "C_grid": [.1, 1., 10.],
                    "adopt_grid": [i / 100 for i in range(50, 100, 5)], "exclude_grid": [i / 100 for i in range(5, 50, 5)],
                    "trials": [{"C": .1, "dev_balanced_log_loss": loss + .2}, {"C": 1., "dev_balanced_log_loss": loss},
                               {"C": 10., "dev_balanced_log_loss": loss + .1}]}
        base.write_json(trained / "training_manifest.json", manifest)
        evaluation_vectors = np.zeros((5, 768), dtype=np.float32)
        evaluation_vectors[:, 0] = [math.log(p / (1 - p)) for p in (.9, .1, .8, .9, .5)]
        np.savez(run / "features.npz", vectors=evaluation_vectors)
        evaluation_scores = evaluator.replay_linear_scores(evaluation_vectors, model["coef"], model["intercept"])
        records = [{"article_id": row["article_id"], "ticker": row["ticker"], "input_sha256": self.digest(row),
                    "score": float(score), "effective_selection": evaluator.selected(float(score), selection["exclude_threshold"], selection["adopt_threshold"],
                        adopt_enabled=selection["adopt_enabled"], exclude_enabled=selection["exclude_enabled"]),
                    "validation_errors": [], "elapsed_seconds": .1, "coverage": cover.copy()}
                   for row, score in zip(self.inputs, evaluation_scores)]
        self.write_rows(run / "results.jsonl", records)
        protocol = {"schema": "relevance-classifier-prediction-1", "complete": True, "input_sha256": base.sha(self.input_path),
                    "rows": 5, "code_hashes": manifest["code_hashes"], "feature_protocol": manifest["feature_protocol"],
                    "encoder": encoder, "training_manifest_path": str(trained / "training_manifest.json"),
                    "training_manifest_sha256": base.sha(trained / "training_manifest.json"),
                    "model_artifact_path": str(trained / "model.json"), "model_artifact_sha256": manifest["model_artifact_sha256"],
                    "feature_cache_sha256": base.sha(run / "features.npz"), "threshold_source": "development_only",
                    "selection": selection, "scorer_contract": manifest["scorer_contract"], "score_is_calibrated_probability": False}
        base.write_json(run / "protocol.json", protocol)
        return run, protocol, records, manifest

    def complete_args(self):
        before, _, _ = self.baseline_fixture()
        after, _, _, _ = self.classifier_fixture()
        refs = self.directory / "refs.jsonl"
        self.write_rows(refs, self.refs)
        return SimpleNamespace(inputs=self.input_path, baseline_run=before, classifier_run=after,
                               references=[refs], out=self.directory / "output")

    def execute(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return evaluator.evaluate(args)

    def test_threshold_endpoints_disabled_extremes_and_error_hold(self):
        self.assertEqual(evaluator.selected(.8, .2, .8), "채택")
        self.assertEqual(evaluator.selected(.2, .2, .8), "제외")
        self.assertEqual(evaluator.selected(.5, .2, .8), "보류")
        self.assertEqual(evaluator.selected(1, .2, 1, adopt_enabled=False), "보류")
        self.assertEqual(evaluator.selected(0, 0, .8, exclude_enabled=False), "보류")
        self.assertEqual(evaluator.selected(.99, .2, .8, ["embedding_failed"]), "보류")
        self.assertEqual(evaluator.selected(None, .2, .8, ["embedding_failed"]), "보류")

    def test_invalid_numeric_values_and_thresholds_fail_closed(self):
        for score in (True, False, float("nan"), float("inf"), -.1, 1.01, "0.8", None):
            with self.subTest(score=score), self.assertRaises(ValueError):
                evaluator.selected(score, .2, .8)
        for lo, hi in ((.8, .8), (.9, .8), (-.1, .8), (.2, 1.1), (False, .8)):
            with self.subTest(thresholds=(lo, hi)), self.assertRaises(ValueError):
                evaluator.selected(.5, lo, hi)
        with self.assertRaises(ValueError):
            evaluator.selected(.5, .2, .8, adopt_enabled=1)

    def test_full_denominator_distinguishes_misses_exclusions_and_holds(self):
        refs = evaluator.validate_references(self.refs, self.index, self.input_path)
        records = {key: {"effective_selection": choice, "validation_errors": [], "elapsed_seconds": .1}
                   for key, choice in zip(self.index, ("채택", "제외", "채택", "채택", "보류"))}
        records[list(records)[-1]]["validation_errors"] = ["embedding_failed"]
        value = evaluator.measure(records, refs)
        self.assertEqual(value["denominator_all_pairs"], 5)
        self.assertEqual(value["adopted_reference_adopt"], 1)
        self.assertEqual(value["adopted_reference_exclude"], 1)
        self.assertEqual(value["adopted_reference_hold"], 1)
        self.assertEqual(value["missed_reference_adopt"], 2)
        self.assertEqual(value["excluded_reference_adopt"], 1)
        self.assertEqual(value["hold_count"], 1)
        self.assertEqual(value["invalid_pairs"], 1)
        self.assertEqual(value["inference_seconds"], .5)
        records.pop(next(iter(records)))
        with self.assertRaises(ValueError):
            evaluator.measure(records, refs)

    def test_reference_hash_quotes_provenance_label_and_time_are_checked(self):
        for change in ({"human_gold": True}, {"reference_type": "human_review"}, {"model_outputs_seen": True},
                       {"full_article_text_read": False}, {"input_sha256": "0" * 64},
                       {"evidence_sentences": ["This never appears in the source."]}, {"evidence_sentences": []},
                       {"expected_time_status": "과거"}, {"expected_label": "판단보류"},
                       {"expected_label": "positive"}, {"input_file_sha256": "0" * 64}):
            refs = copy.deepcopy(self.refs)
            refs[0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluator.validate_references(refs, self.index, self.input_path)
        for refs in (self.refs[:-1], self.refs + self.refs[:1]):
            with self.assertRaises(ValueError):
                evaluator.validate_references(refs, self.index, self.input_path)

    def test_strict_json_rejects_nonfinite_duplicate_keys_and_pair_coercion(self):
        for text in ('{"score":NaN}', '{"score":1,"score":0}', '{"score":Infinity}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                evaluator.read_json(text)
        rows = copy.deepcopy(self.inputs)
        rows[0]["ticker"] = 12
        with self.assertRaises(ValueError):
            evaluator.indexed(rows)

    def test_linear_score_replay_uses_full_float32_rows_and_float64_sigmoid(self):
        import numpy as np

        vectors = np.zeros((3, 768), dtype=np.float32)
        vectors[:, 0] = (-1000, 0, 1000)
        coefficients = [1.0] + [0.0] * 767
        scores = evaluator.replay_linear_scores(vectors, coefficients, [0.0])
        self.assertEqual(scores.dtype, np.dtype("float64"))
        self.assertEqual(scores.tolist(), [0.0, .5, 1.0])
        for broken in (vectors.astype(np.float64), vectors[:, :767], np.full((3, 768), np.nan, dtype=np.float32)):
            with self.assertRaises(ValueError):
                evaluator.replay_linear_scores(broken, coefficients, 0.0)
        for broken in ([True] * 768, coefficients[:-1], [float("inf")] + coefficients[1:]):
            with self.assertRaises(ValueError):
                evaluator.replay_linear_scores(vectors, broken, 0.0)

    def test_baseline_replays_raw_and_rejects_missing_duplicate_or_tampered_results(self):
        folder, protocol, records = self.baseline_fixture()
        got, _ = evaluator.load_baseline(folder, self.index, self.input_path)
        self.assertEqual(len(got), 5)
        self.assertEqual(sum(bool(r["validation_errors"]) for r in got.values()), 1)
        for broken in (records[:-1], records + records[:1]):
            self.write_rows(folder / "results.jsonl", broken)
            with self.assertRaises(ValueError):
                evaluator.load_baseline(folder, self.index, self.input_path)
        for change in ({"effective_selection": "채택"}, {"validation_errors": []}, {"elapsed_seconds": 2.0}):
            broken = copy.deepcopy(records)
            broken[-1].update(change)
            self.write_rows(folder / "results.jsonl", broken)
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluator.load_baseline(folder, self.index, self.input_path)
        self.write_rows(folder / "results.jsonl", records)
        for change in ({"temperature": .6}, {"input_sha256": "0" * 64}, {"truncation": True}):
            base.write_json(folder / "protocol.json", {**protocol, **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluator.load_baseline(folder, self.index, self.input_path)
        base.write_json(folder / "protocol.json", protocol)
        (folder / "inference.lock").write_text("123", encoding="utf8")
        with self.assertRaises(ValueError):
            evaluator.load_baseline(folder, self.index, self.input_path)

    def test_complete_evaluation_replays_scores_thresholds_and_keeps_all_pairs(self):
        args = self.complete_args()
        summary = self.execute(args)
        self.assertEqual(summary["pairs"], 5)
        self.assertEqual(summary["baseline"]["invalid_pairs"], 1)
        self.assertEqual(summary["classifier"]["denominator_all_pairs"], 5)
        self.assertEqual(summary["classifier"]["adopted_reference_adopt"], 1)
        self.assertEqual(summary["classifier"]["adopted_reference_exclude"], 1)
        self.assertEqual(summary["classifier"]["adopted_reference_hold"], 1)
        self.assertEqual(summary["classifier"]["excluded_reference_adopt"], 1)
        self.assertEqual(summary["classifier"]["missed_reference_adopt"], 2)
        self.assertTrue(summary["training_audit"]["dev_thresholds_reproduced"])
        self.assertEqual(summary["all_constant_baselines"]["채택"]["adopted_reference_adopt"], 3)
        self.assertFalse(summary["human_gold"])
        self.assertFalse(summary["score_is_calibrated_probability"])
        for path, digest in summary["source_sha256"].items():
            self.assertEqual(base.sha(path), digest)
        self.assertEqual(len(evaluator.read_rows(args.out / "pair_comparison.jsonl")), 5)
        with self.assertRaises(ValueError):
            self.execute(args)

    def test_classifier_score_row_order_coverage_and_hash_tampering_rejected(self):
        run, protocol, records, _ = self.classifier_fixture()
        for changed in (records[:-1], records + records[:1], list(reversed(records))):
            self.write_rows(run / "results.jsonl", changed)
            with self.assertRaises(ValueError):
                evaluator.load_classifier(run, self.index, self.input_path, {})
        for change in ({"score": .123}, {"effective_selection": "제외"}, {"input_sha256": "0" * 64},
                       {"elapsed_seconds": True}, {"validation_errors": ["ignored_failure"]},
                       {"coverage": {**records[0]["coverage"], "covered_content_tokens": 99}}):
            changed = copy.deepcopy(records)
            changed[0].update(change)
            self.write_rows(run / "results.jsonl", changed)
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluator.load_classifier(run, self.index, self.input_path, {})
        self.write_rows(run / "results.jsonl", records)
        for change in ({"feature_cache_sha256": "0" * 64}, {"input_sha256": "0" * 64},
                       {"training_manifest_sha256": "0" * 64}, {"score_is_calibrated_probability": True},
                       {"threshold_source": "fresh_references"}, {"complete": False}):
            base.write_json(run / "protocol.json", {**protocol, **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluator.load_classifier(run, self.index, self.input_path, {})

    def test_threshold_and_regularization_changes_cannot_be_hidden_by_rehashing(self):
        run, protocol, _, manifest = self.classifier_fixture()
        trained = Path(protocol["training_manifest_path"]).parent
        for change in ({"adopt_threshold": .95}, {"exclude_threshold": .05}, {"C": 10.}, {"adopt_enabled": False}):
            changed = {**manifest, "selection": {**manifest["selection"], **change}}
            base.write_json(trained / "training_manifest.json", changed)
            base.write_json(run / "protocol.json", {**protocol, "selection": changed["selection"],
                "training_manifest_sha256": base.sha(trained / "training_manifest.json")})
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluator.load_classifier(run, self.index, self.input_path, {})

    def test_development_labels_and_features_are_pinned_and_fresh_keys_excluded(self):
        run, protocol, _, manifest = self.classifier_fixture()
        devpath = Path(manifest["source_paths"]["dev_labels"])
        devpath.write_text(devpath.read_text(encoding="utf8") + "\n", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "input/label hash"):
            evaluator.load_classifier(run, self.index, self.input_path, {})
        run, protocol, _, manifest = self.classifier_fixture()
        overlap = copy.deepcopy(self.inputs)
        overlap[0]["article_id"] = "dev0"
        with self.assertRaisesRegex(ValueError, "Fresh evaluation identity"):
            evaluator.validate_training(protocol, evaluator.indexed(overlap), {})


if __name__ == "__main__":
    unittest.main()
