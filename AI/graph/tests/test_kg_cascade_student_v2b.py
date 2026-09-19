"""The B encoding repair must preserve the frozen original A implementation."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_cascade_student_v2 as original
import kg_cascade_student_v2b as repaired


class PairRepairAdapterTests(unittest.TestCase):
    def test_original_module_and_model_identity_remain_separate(self):
        self.assertIsNot(original, repaired._core)
        self.assertIsNot(original.tokenize_document, repaired.tokenize_document)
        self.assertEqual(original.model_version({"candidate": "A"}), "kg-cascade-student-v2-A")
        self.assertEqual(original.FEATURE_PROTOCOL["target_content_masks"],
                         "tokenizer_sequence_ids_exclude_special_tokens_for_B")
        self.assertNotIn("kg_cascade_student_v2b.py", original.code_hashes())
        self.assertNotIn("kg_cascade_pair_encoding_fix.py", original.code_hashes())
        self.assertEqual(repaired.StudentPredictor.__module__, "_cosmos_student_v2_pairfix_core")

    def test_fixed_architecture_training_budget_and_b_only(self):
        self.assertEqual(repaired.configuration(), original.PREDECLARED_CANDIDATES["B"])
        self.assertEqual(repaired.configuration(original.PREDECLARED_CANDIDATES["B"]),
                         original.PREDECLARED_CANDIDATES["B"])
        self.assertEqual(repaired._core._make_model.__code__.co_code, original._make_model.__code__.co_code)
        self.assertEqual(repaired.train_model.__code__.co_code, original.train_model.__code__.co_code)
        self.assertNotEqual(repaired.model_version(repaired.configuration()), original.model_version({"candidate":"B"}))
        with self.assertRaisesRegex(ValueError, "B only"):
            repaired.configuration({"candidate": "A"})

    def test_new_provenance_binds_original_and_repair_bytes(self):
        bound = repaired.code_hashes()
        for name, value in original.code_hashes().items():
            self.assertEqual(bound[name], value)
        for name in ("kg_cascade_student_v2b.py", "kg_cascade_pair_encoding_fix.py"):
            self.assertEqual(bound[name], original.preview.file_sha256(original.HERE/name))
        self.assertEqual(repaired._core.code_hashes(), bound)
        self.assertIs(repaired._core.FEATURE_PROTOCOL, repaired.FEATURE_PROTOCOL)
        self.assertIs(repaired._core.tokenize_document, repaired.tokenize_document)


if __name__ == "__main__":
    unittest.main()
