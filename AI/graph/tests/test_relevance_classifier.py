"""Coverage, isolation, thresholds and serialized model replay; no real reviews."""
from pathlib import Path
import copy
import json
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relevance_classifier as c


class Encoding:
    def __init__(self, ids, types=None):
        self.ids = ids
        self.type_ids = types
        self.overflowing = []
    def truncate(self, width, stride=0):
        assert stride == 0
        self.overflowing = [Encoding(self.ids[i:i + width]) for i in range(width, len(self.ids), width)]
        self.ids = self.ids[:width]


class CharacterTokenizer:
    @property
    def backend_tokenizer(self):
        return self
    def no_truncation(self):
        pass
    def no_padding(self):
        pass
    def encode(self, value, add_special_tokens=False):
        return Encoding([ord(x) for x in value])
    def num_special_tokens_to_add(self, pair=True):
        return 3
    def post_process(self, first, second, add_special_tokens=True):
        first, second = first.ids, second.ids
        return Encoding([-1, *first, -2, *second, -3], [0] * (len(first) + 2) + [1] * (len(second) + 1))


class ClassifierTests(unittest.TestCase):
    def test_full_late_body_coverage_and_target_in_every_window(self):
        row = {"company_name": "가상기업", "title": "제목", "body": "앞" * 700 + "후반의 중요한 사건"}
        chunks, coverage = c.windows(CharacterTokenizer(), row)
        company = [ord(x) for x in row["company_name"]]
        recovered = []
        for item, n in chunks:
            self.assertEqual(item["input_ids"][1:1 + len(company)], company)
            body = item["input_ids"][len(company) + 2:-1]
            self.assertEqual(len(body), n)
            recovered.extend(body)
            self.assertLessEqual(len(item["input_ids"]), 256)
        self.assertEqual(recovered, [ord(x) for x in row["title"] + "\n" + row["body"]])
        self.assertEqual(coverage["covered_content_tokens"], len(recovered))
        self.assertGreater(coverage["windows"], 1)

    def test_labels_dates_ids_rationales_do_not_change_model_input(self):
        row = {"company_name": "회사", "title": "새 기사", "body": "현재 사건"}
        changed = dict(row, article_id="different", ticker="123456", published_at="2099-01-01",
                       ai_reason="채택", human_selection="제외", label=0, future_return=999)
        self.assertEqual(c.windows(CharacterTokenizer(), row), c.windows(CharacterTokenizer(), changed))
        self.assertNotEqual(c.windows(CharacterTokenizer(), row), c.windows(CharacterTokenizer(), dict(row, company_name="다른 회사")))
        with self.assertRaises(ValueError):
            c.windows(CharacterTokenizer(), dict(row, company_context={"business": "future"}))

    def test_pool_includes_late_window_and_its_token_weight(self):
        vectors = np.zeros((2, 384)); vectors[0, 0] = 1; vectors[1, 1] = 1
        result = c.pool_windows(vectors, [3, 1])
        expected = np.asarray([3., 1.]); expected /= np.linalg.norm(expected)
        np.testing.assert_allclose(result[:2], expected, rtol=1e-6)
        self.assertGreater(result[1], 0)
        self.assertEqual(result.dtype, np.float32)
        self.assertEqual(result.shape, (768,))
        with self.assertRaises(ValueError):
            c.pool_windows(vectors, [3, 0])

    def test_sigmoid_replay_extremes_and_nonfinite_rejection(self):
        model = {"classes": [0, 1], "coef": [1.] + [0.] * 767, "intercept": .2}
        vectors = np.zeros((3, 768)); vectors[:, 0] = [-1000, 0, 1000]
        values = c.scores(vectors, model)
        self.assertTrue(np.isfinite(values).all())
        self.assertEqual(values[0], 0); self.assertEqual(values[2], 1)
        self.assertAlmostEqual(values[1], 1 / (1 + np.exp(-.2)))
        with self.assertRaises(ValueError):
            c.scores(vectors, dict(model, intercept=float("nan")))

    def test_dev_thresholds_support_error_constraint_and_disabled_paths(self):
        y, score = np.asarray([1, 1, 1, 0, 0, 0]), np.asarray([.9, .8, .7, .3, .2, .1])
        selected = c.choose_thresholds(y, score)
        self.assertEqual(selected, {"adopt_enabled": True, "exclude_enabled": True,
                                   "adopt_threshold": .7, "exclude_threshold": .3})
        self.assertEqual(c.decision(.7, selected), "채택")
        self.assertEqual(c.decision(.3, selected), "제외")
        self.assertEqual(c.decision(.5, selected), "보류")
        self.assertEqual(c.decision(.9, selected, ["broken"]), "보류")
        disabled = c.choose_thresholds(y, np.full(6, .5))
        self.assertFalse(disabled["adopt_enabled"]); self.assertFalse(disabled["exclude_enabled"])
        self.assertEqual(c.decision(1., disabled), "보류"); self.assertEqual(c.decision(0., disabled), "보류")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "artifacts" / ("classifier_test_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
    def tearDown(self):
        target = self.root.resolve()
        assert target.parent == (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        shutil.rmtree(target)
    def write_rows(self, path, rows):
        path.write_text("".join(c.base.canonical(row) + "\n" for row in rows), encoding="utf8")
    @staticmethod
    def fake_encode(rows, *args):
        vectors = np.zeros((len(rows), 768), dtype=np.float32)
        vectors[:, 0] = [1 if row["body"] == "positive" else -1 for row in rows]
        coverage = [{"total_content_tokens": 3, "covered_content_tokens": 3, "windows": 1,
                     "target_tokens": 1, "max_window_tokens": 7} for row in rows]
        return vectors, coverage, [0.01] * len(rows), {"fixture": True}
    def make_seed(self):
        seed = self.root / "seed"; seed.mkdir()
        for part in ("train", "dev"):
            rows = [{"sample_id": f"{part}-{i}", "company_name": "Fixture", "title": "Title",
                     "body": "positive" if i < 3 else "negative"} for i in range(6)]
            labels = [{"sample_id": row["sample_id"], "label": "채택" if i < 3 else "제외"} for i, row in enumerate(rows)]
            self.write_rows(seed / (part + "_inputs.jsonl"), rows)
            self.write_rows(seed / (part + "_labels.jsonl"), labels)
        manifest = {"schema": "relevance-classifier-seed-data-1", "status": "frozen",
                    "label_mapping": {"제외": 0, "채택": 1},
                    "labels_and_inputs_separate": True, "independent_human_gold": False,
                    "leakage_check": {"fixture": "PASS"},
                    "files": {p.name: {"sha256": c.base.sha(p)} for p in seed.glob("*.jsonl")}}
        c.dump_new(seed / "manifest.json", manifest)
        return seed
    def test_train_reload_predict_and_corrupted_artifact_refusal(self):
        seed = self.make_seed(); trained = self.root / "trained"
        with patch.object(c, "encode", side_effect=self.fake_encode):
            manifest = c.train(seed, trained, device="cpu")
            inputs = self.root / "new.jsonl"
            rows = [{"article_id": "new-a", "ticker": "F", "company_name": "Fixture", "title": "Title", "body": "positive",
                     "published_at": "2021-01-01", "company_context": None, "case_kind": "synthetic", "source_split": "train"}]
            self.write_rows(inputs, rows)
            c.predict(inputs, trained, self.root / "prediction", device="cpu")
            result = c.base.read_rows(self.root / "prediction/results.jsonl")[0]
            model = json.loads((trained / "model.json").read_text(encoding="utf8"))
            expected = 1 / (1 + np.exp(-(model["coef"][0] + model["intercept"])))
            self.assertAlmostEqual(result["score"], expected, places=14)
            self.assertEqual(manifest["threshold_source"], "development_only")
            with (trained / "model.json").open("a", encoding="utf8") as stream:
                stream.write(" ")
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                c.predict(inputs, trained, self.root / "must_fail", device="cpu")
            self.assertFalse((self.root / "must_fail").exists())
    def test_changing_training_labels_mid_extraction_refuses_publish(self):
        seed = self.make_seed()
        def changed(rows, *args):
            result = self.fake_encode(rows)
            with (seed / "train_labels.jsonl").open("a", encoding="utf8") as stream:
                stream.write("\n")
            return result
        with patch.object(c, "encode", side_effect=changed), self.assertRaisesRegex(ValueError, "dependencies changed"):
            c.train(seed, self.root / "must_fail", device="cpu")
        self.assertFalse((self.root / "must_fail").exists())


if __name__ == "__main__":
    unittest.main()
