"""CPU evidence for v2 full coverage, target distinction, learning and integrity."""
from pathlib import Path
import copy
import json
import shutil
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kg_cascade_student_v2 as student
import test_kg_cascade_student as fixtures
from test_kg_cascade_student import CharacterTokenizer, PROTOCOL, source, label


class SequenceTokenizer(CharacterTokenizer):
    padding_side = "right"
    def post_process(self, first, second, add_special_tokens=True):
        encoded = super().post_process(first, second, add_special_tokens)
        encoded.sequence_ids = [None] + [0] * len(first.ids) + [None] + [1] * len(second.ids) + [None]
        return encoded


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "artifacts" / ("student_v2_cpu_test_" + uuid.uuid4().hex)
        self.root.mkdir()
    def tearDown(self):
        target = self.root.resolve()
        assert target.parent == (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        shutil.rmtree(target)
    @staticmethod
    def loader(path):
        encoder, _ = fixtures.FineTuningTests.loader(path)
        return encoder, SequenceTokenizer()
    @staticmethod
    def dataset():
        train = [source("train-" + str(i), body=chr(65 + i) * 30) for i in range(3)]
        dev = [source("dev-" + str(i), body=chr(70 + i) * 30) for i in range(3)]
        relations = ("direct", "incidental", "unclear")
        return train, [label(r, k) for r, k in zip(train, relations)], dev, [label(r, k) for r, k in zip(dev, relations)]

    def test_only_two_candidates_and_bounded_epochs(self):
        self.assertEqual(set(student.PREDECLARED_CANDIDATES), {"A", "B"})
        for candidate in "AB":
            config = student.configuration({"candidate": candidate})
            self.assertEqual(config["head_learning_rate"], 3e-4)
            self.assertEqual(config["epochs"], 4)
            self.assertEqual(config["learning_rate"], 2e-5)
        for config in ({"candidate": "C"}, {"epochs": 5}, {"class_weights": [1, 2, 1]}):
            with self.assertRaises(ValueError):
                student.configuration(config)

    def test_masks_keep_all_content_and_exclude_special_tokens(self):
        row = source(body="a" * 700 + "last company earnings")
        windows, coverage = student.tokenize_document(SequenceTokenizer(), row, {"candidate": "B"})
        rebuilt = []
        for features, count in windows:
            ids, target, content = features["input_ids"], features["target_mask"], features["content_mask"]
            self.assertEqual([value for value, mask in zip(ids, target) if mask], list(map(ord, row["company_name"])))
            rebuilt.extend(value for value, mask in zip(ids, content) if mask)
            self.assertEqual(sum(content), count)
            self.assertTrue(all(not (left and right) for left, right in zip(target, content)))
            self.assertEqual(sum(target) + sum(content), len(ids) - 3)
        self.assertEqual(rebuilt, list(map(ord, row["title"] + "\n" + row["body"])))
        self.assertTrue(coverage["full_coverage"])

    def test_over_limit_stays_explicit_fallback(self):
        for candidate in "AB":
            windows, coverage = student.tokenize_document(SequenceTokenizer(), source(body="x" * 1000),
                {"candidate": candidate, "max_windows": 1})
            self.assertEqual(windows, [])
            self.assertFalse(coverage["full_coverage"])
            self.assertEqual(coverage["covered_content_tokens"], 0)

    def test_all_encoder_gradients_and_late_window_survive(self):
        import torch
        for candidate in "AB":
            torch.manual_seed(123)
            encoder, tokenizer = self.loader(self.root)
            config = student.configuration({"candidate": candidate, "max_tokens": 32, "head_dropout": 0.})
            model = student._make_model(encoder, config, torch)
            windows, _ = student.tokenize_document(tokenizer, source(body="a" * 100 + "Z"), config)
            self.assertGreater(len(windows), 1)
            model(windows, tokenizer, "cpu").square().sum().backward()
            for name, parameter in encoder.named_parameters():
                self.assertIsNotNone(parameter.grad, name)
                self.assertTrue(bool(torch.isfinite(parameter.grad).all()), name)
            self.assertGreater(encoder.embedding.weight.grad[ord("Z") % 32].abs().sum().item(), 0.)
            if candidate == "B":
                self.assertTrue(any(parameter.grad is not None and parameter.grad.abs().sum() > 0
                                    for parameter in model.head["window_attention"].parameters()))

    def test_target_is_a_real_feature_not_metadata(self):
        import torch
        torch.manual_seed(43)
        encoder, tokenizer = self.loader(self.root)
        config = student.configuration({"candidate": "B", "head_dropout": 0.})
        model = student._make_model(encoder, config, torch).eval()
        one, _ = student.tokenize_document(tokenizer, source(body="a" * 400), config)
        two, _ = student.tokenize_document(tokenizer, source(body="a" * 400, company_name="Different"), config)
        self.assertFalse(torch.allclose(model(one, tokenizer, "cpu"), model(two, tokenizer, "cpu")))

    def test_metadata_future_values_do_not_change_features(self):
        row = source()
        for candidate in "AB":
            config = {"candidate": candidate}
            changed = dict(row, future_price=1e10, human_decision="exclude", ticker="other", article_id="different")
            self.assertEqual(student.tokenize_document(SequenceTokenizer(), row, config),
                             student.tokenize_document(SequenceTokenizer(), changed, config))

    def test_train_reload_exact_and_manifest_binds_candidate(self):
        import torch
        from safetensors.torch import load_file
        for candidate in "AB":
            out = self.root / candidate
            with patch.object(student.legacy, "verified_encoder", return_value=(self.root / "parent", {"fixture": True})), \
                 patch.object(student, "_load_encoder", side_effect=self.loader):
                manifest = student.train_model(*self.dataset(), out, train_protocol=PROTOCOL,
                    dev_protocol=PROTOCOL, device="cpu", config={"candidate": candidate, "label_policy": "original_strict", "epochs": 2,
                        "learning_rate": .02, "head_dropout": 0., "max_tokens": 32, "gradient_accumulation_docs": 2})
                original = self.loader(self.root)[0].state_dict()
                trained = load_file(str(out / "checkpoint/encoder/model.safetensors"))
                self.assertTrue(any(not torch.equal(original[k], trained[k]) for k in original))
                self.assertEqual(manifest["model_version"], "kg-cascade-student-v2-" + candidate)
                self.assertFalse(manifest["calibration_or_test_labels_used"])
                self.assertIsNone(manifest["routing_thresholds"])
                self.assertFalse(manifest["graph_materialization_ready"])
                rows = [source("new", body="novel news")]
                first, second = student.predict(rows, out, device="cpu"), student.predict(rows, out, device="cpu")
                self.assertEqual(first[0]["probabilities"], second[0]["probabilities"])
                self.assertEqual(set(first[0]["probabilities"]), {"include", "exclude", "review"})
                self.assertAlmostEqual(sum(first[0]["probabilities"].values()), 1., places=6)
                self.assertEqual(first[0]["model_version"], manifest["model_version"])
                self.assertTrue(first[0]["coverage"]["encoding_completed"])
                self.assertIsNone(first[0]["evidence"])
                with (out / "checkpoint/encoder/config.json").open("a", encoding="utf8") as stream:
                    stream.write(" ")
                with self.assertRaisesRegex(ValueError, "checkpoint bytes changed"):
                    student.predict(rows, out, device="cpu")

    def test_unusable_semantic_execution_is_omitted_not_review(self):
        train, labels, dev, devlabels = self.dataset()
        train.append(source("train-unusable")); dev.append(source("dev-unusable"))
        labels.append({"raw_unusable_fixture": True}); devlabels.append({"raw_unusable_fixture": True})
        adapter = SimpleNamespace(VERSION="kg_semantic_teacher_v1", CLASSES=student.CLASSES,
                                  validate_labels=lambda rows, raw, protocol: [0, 1, 2, None])
        with patch.dict(sys.modules, {"kg_semantic_teacher": adapter}), \
             patch.object(student.legacy, "verified_encoder", return_value=(self.root / "parent", {"fixture": True})), \
             patch.object(student, "_load_encoder", side_effect=self.loader):
            manifest = student.train_model(train, labels, dev, devlabels, self.root / "masked",
                train_protocol={"fixture": "compact"}, dev_protocol={"fixture": "compact"}, device="cpu",
                config={"candidate": "B", "label_policy": "kg_semantic_teacher_v1", "epochs": 1, "max_tokens": 32})
        self.assertEqual(manifest["unusable_teacher_labels"], {"train": [3], "model_dev": [3]})
        self.assertEqual(manifest["class_counts"]["train"], {"include": 1, "exclude": 1, "review": 1, "unusable": 1})
        self.assertEqual(manifest["trials"][0]["train_pairs_used"], 3)
        self.assertEqual(manifest["trials"][0]["model_dev_pairs_used"], 3)

    def test_teacher_replay_and_article_split_isolation_remain_required(self):
        train, labels, dev, devlabels = self.dataset()
        tampered = copy.deepcopy(labels)
        tampered[0]["selection"]["decision"] = "review"
        with self.assertRaisesRegex(ValueError, "differs from source stages"):
            student.train_model(train, tampered, dev, devlabels, self.root / "bad", train_protocol=PROTOCOL, dev_protocol=PROTOCOL, device="cpu", config={"label_policy": "original_strict"})
        with self.assertRaisesRegex(ValueError, "leakage"):
            student.validate_split_isolation(train, [dict(train[0], ticker="other")])


if __name__ == "__main__":
    unittest.main()
