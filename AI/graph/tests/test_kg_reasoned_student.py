"""CPU-only adapter tests: source fidelity, failed labels, learning and integrity."""
from pathlib import Path
import copy
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kg_cascade_student_v2 as original
import kg_cascade_student_v2b as repaired
import kg_reasoned_student as student
import kg_reasoned_teacher_v3 as teacher
import test_kg_cascade_student as fixtures


class InsertionTokenizer(fixtures.CharacterTokenizer):
    padding_side = "right"

    def post_process(self, first, second, add_special_tokens=True):
        pair = super().post_process(first, second, add_special_tokens)
        pair.special_tokens_mask = ([1] + [0] * len(first.ids) + [1]
                                    + [0] * len(second.ids) + [1])
        return pair


class ReasonedAdapterTests(unittest.TestCase):
    def test_namespace_isolation_architecture_and_new_defaults(self):
        self.assertIsNot(student._core, original)
        self.assertIsNot(student._core, repaired._core)
        self.assertEqual(original.model_version({"candidate": "A"}), "kg-cascade-student-v2-A")
        self.assertEqual(original.configuration()["label_policy"], "kg_semantic_teacher_v1")
        self.assertEqual(repaired.configuration()["head_dropout"], 0.1)
        self.assertEqual(student._core._make_model.__code__.co_code, original._make_model.__code__.co_code)
        self.assertEqual(student.train_model.__code__.co_code, original.train_model.__code__.co_code)
        self.assertEqual(student.StudentPredictor.__module__, "_cosmos_reasoned_student_core")
        config = student.configuration()
        self.assertEqual((config["candidate"], config["epochs"], config["seed"]), ("B", 4, 1709))
        self.assertEqual((config["learning_rate"], config["head_learning_rate"], config["head_dropout"]),
                         (1e-5, 3e-4, 0.2))
        self.assertEqual(config["label_policy"], teacher.VERSION)

    def test_old_policy_unknown_options_and_invalid_budget_are_rejected(self):
        for config in ({"candidate": "A"}, {"label_policy": "kg_semantic_teacher_v1"},
                       {"epochs": 5}, {"unregistered_option": True},
                       {"learning_rate": float("nan")}, {"max_target_tokens": 0}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                student.configuration(config)

    def test_hashes_bind_teacher_alias_dictionary_matcher_and_original_engine(self):
        hashes = student.code_hashes()
        for name, value in original.code_hashes().items():
            self.assertEqual(hashes[name], value)
        for name, value in teacher.code_hashes().items():
            self.assertEqual(hashes[name], value)
        for name in ("kg_reasoned_student.py", "kg_reasoned_teacher.py", "kg_reasoned_teacher_v3.py", "kg_target_aliases.py",
                     "kg_cascade_pair_encoding_fix.py", "../ner/data/aliases.csv", "kg_semantic_teacher.py"):
            self.assertEqual(hashes[name], original.preview.file_sha256(student.HERE / name))
        self.assertNotIn("kg_reasoned_student.py", original.code_hashes())
        self.assertNotIn("kg_reasoned_student.py", repaired.code_hashes())
        self.assertEqual(student.FEATURE_PROTOCOL["target_lexical_hints"]["provenance"],
                         student.aliases.provenance())

    def test_alias_hints_preserve_original_row_and_all_content_tokens(self):
        row = fixtures.source(body="Company CO Acme Epsilon\n" + "a" * 700 + "late event")
        saved = copy.deepcopy(row)
        with patch.object(student.aliases, "matched_aliases",
                          return_value=["company", "CO", "co", "Acme", "Epsilon", "ignored fourth hint"]):
            self.assertEqual(student.target_surface(row), "Company | CO | Acme | Epsilon")
            windows, coverage = student.tokenize_document(InsertionTokenizer(), row)
        self.assertEqual(row, saved)
        restored = []
        for feature, weight in windows:
            restored.extend(token for token, keep in zip(feature["input_ids"], feature["content_mask"]) if keep)
            target = [token for token, keep in zip(feature["input_ids"], feature["target_mask"]) if keep]
            self.assertEqual(target, list(map(ord, "Company | CO | Acme | Epsilon")))
            self.assertEqual(sum(feature["content_mask"]), weight)
        self.assertEqual(restored, list(map(ord, saved["title"] + "\n" + saved["body"])))
        self.assertTrue(coverage["full_coverage"])
        self.assertGreater(len(windows), 1)

    def test_target_alias_overflow_requires_fallback_without_truncation(self):
        row = fixtures.source(body="A" * 90 + " Company event")
        with patch.object(student.aliases, "matched_aliases", return_value=["A" * 90]):
            windows, coverage = student.tokenize_document(InsertionTokenizer(), row)
        self.assertEqual(windows, [])
        self.assertFalse(coverage["full_coverage"])
        self.assertGreater(coverage["target_tokens"], student.configuration()["max_target_tokens"])
        self.assertEqual(coverage["covered_content_tokens"], 0)

    def test_teacher_is_replayed_none_preserved_and_invalid_indices_refused(self):
        rows = [fixtures.source("r-" + str(i)) for i in range(4)]
        labels, protocol = [{"fixture": i} for i in range(4)], {"fixture": True}
        with patch.object(teacher, "validate_labels", return_value=[0, 1, 2, None]) as replay:
            self.assertEqual(student.resolve_training_labels(rows, labels, protocol,
                             student.configuration()), [0, 1, 2, None])
            replay.assert_called_once_with(rows, labels, protocol)
        for invalid in ([0, 1, 2], [0, 1, 2, True], [0, 1, 2, -1], [0, 1, 2, 3]):
            with patch.object(teacher, "validate_labels", return_value=invalid), \
                 self.assertRaisesRegex(ValueError, "Invalid reasoned teacher"):
                student.resolve_training_labels(rows, labels, protocol, student.configuration())
        with patch.object(teacher, "validate_labels", side_effect=ValueError("source hash changed")), \
             self.assertRaisesRegex(ValueError, "source hash changed"):
            student.resolve_training_labels(rows, labels, protocol, student.configuration())


class ReasonedLearningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "artifacts" / ("reasoned_student_cpu_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)

    def tearDown(self):
        resolved = self.root.resolve()
        assert resolved.parent == (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        shutil.rmtree(resolved)

    @staticmethod
    def loader(path):
        encoder, _ = fixtures.FineTuningTests.loader(path)
        return encoder, InsertionTokenizer()

    def test_train_mask_failed_teacher_save_reload_input_hash_and_tamper_refusal(self):
        import torch
        from safetensors.torch import load_file
        train = [fixtures.source("train-" + str(i), body=chr(65 + i) * 30) for i in range(4)]
        dev = [fixtures.source("dev-" + str(i), body=chr(75 + i) * 30) for i in range(3)]
        train_labels = [{"class_index": value} for value in (0, 1, 2, None)]
        dev_labels = [{"class_index": value} for value in (0, 1, 2)]
        # The teacher has its own source/protocol replay tests. Here its adapter
        # contract is controlled so all three semantic labels and one execution
        # failure exercise the unchanged real optimizer/checkpoint machinery.
        with patch.object(teacher, "validate_labels",
                          side_effect=lambda rows, labels, protocol: [label["class_index"] for label in labels]), \
             patch.object(student.legacy, "verified_encoder", return_value=(self.root / "parent", {"fixture": True})), \
             patch.object(student._core, "_load_encoder", side_effect=self.loader), \
             patch.object(student.aliases, "matched_aliases", return_value=[]):
            trained = self.root / "trained"
            manifest = student.train_model(train, train_labels, dev, dev_labels, trained,
                train_protocol={"fixture": True}, dev_protocol={"fixture": True}, device="cpu",
                config={"epochs": 2, "learning_rate": 0.02, "head_dropout": 0.,
                        "gradient_accumulation_docs": 2, "max_tokens": 32, "window_microbatch": 2})
            self.assertEqual(manifest["class_counts"]["train"],
                             {"include": 1, "exclude": 1, "review": 1, "unusable": 1})
            self.assertEqual(manifest["unusable_teacher_labels"]["train"], [3])
            self.assertTrue(all(trial["train_pairs_used"] == 3 for trial in manifest["trials"]))
            self.assertEqual(manifest["selected_epoch"],
                             min(manifest["trials"], key=lambda trial: trial["model_dev_log_loss"])["epoch"])
            before = self.loader(self.root / "parent")[0].state_dict()
            after = load_file(str(trained / "checkpoint/encoder/model.safetensors"))
            self.assertTrue(any(not torch.equal(before[key], after[key]) for key in before))
            row = fixtures.source("unseen", body="new useful economic facts")
            original_row = copy.deepcopy(row)
            first = student.predict([row], trained, device="cpu")[0]
            second = student.predict([row], trained, device="cpu")[0]
            self.assertEqual(first["probabilities"], second["probabilities"])
            self.assertAlmostEqual(sum(first["probabilities"].values()), 1., places=6)
            self.assertEqual(first["input_sha256"], original.preview.canonical_input_sha256(original_row))
            self.assertEqual(row, original_row)
            self.assertEqual(first["label_policy"], teacher.VERSION)
            self.assertFalse(first["graph_materialization_ready"])
            self.assertIsNone(first["evidence"])
            overflow = student.predict([fixtures.source("long", body="a" * 3000)], trained, device="cpu")[0]
            self.assertTrue(overflow["requires_fallback"])
            self.assertIsNone(overflow["probabilities"])
            with patch.object(student._core, "code_hashes", return_value={**student.code_hashes(), "alias_changed": "f" * 64}), \
                 self.assertRaisesRegex(ValueError, "training code changed"):
                student.predict([row], trained, device="cpu")
            with (trained / "checkpoint/encoder/config.json").open("a", encoding="utf8") as stream:
                stream.write(" ")
            with self.assertRaisesRegex(ValueError, "checkpoint bytes changed"):
                student.predict([row], trained, device="cpu")


if __name__ == "__main__":
    unittest.main()
