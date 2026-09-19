"""CPU-only regression of repaired masks against frozen tokens and actual train/dev."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_cascade_pair_encoding_fix as repair
import kg_cascade_student as v1
import kg_cascade_student_v2 as v2

HERE = Path(__file__).resolve().parents[1]
FULL_REGRESSION = {}


def row(body, title="TargetCompany news", company="TargetCompany"):
    return {"article_id": "synthetic-mask-fixture", "ticker": "FIXTURE", "company_name": company,
            "title": title, "body": body, "published_at": "2026-09-17", "source_url": None}


class VerifiedMasksTests(unittest.TestCase):
    def test_colliding_sequence_metadata_is_ignored_but_tokens_are_verified(self):
        pair = SimpleNamespace(ids=[0, 10, 11, 2, 2, 20, 21, 22, 2],
                               special_tokens_mask=[1, 0, 0, 1, 1, 0, 0, 0, 1],
                               sequence_ids=[None, None, None, None, None, 1, 1, 1, None])
        target, content = repair.verified_pair_masks(pair, [10, 11], [20, 21, 22], expected_special_tokens=4)
        self.assertEqual(target, [0, 1, 1, 0, 0, 0, 0, 0, 0])
        self.assertEqual(content, [0, 0, 0, 0, 0, 1, 1, 1, 0])

    def test_literal_special_token_values_are_kept_when_not_inserted(self):
        pair = SimpleNamespace(ids=[0, 0, 2, 2, 0, 2, 2],
                               special_tokens_mask=[1, 0, 1, 1, 0, 0, 1])
        target, content = repair.verified_pair_masks(pair, [0], [0, 2], expected_special_tokens=4)
        self.assertEqual([value for value, flag in zip(pair.ids, target) if flag], [0])
        self.assertEqual([value for value, flag in zip(pair.ids, content) if flag], [0, 2])

    def test_changed_order_incomplete_mask_and_extra_insertions_raise(self):
        base = {"ids": [0, 10, 2, 2, 20, 21, 2], "special_tokens_mask": [1, 0, 1, 1, 0, 0, 1]}
        for changed in ({**base, "ids": [0, 20, 2, 2, 10, 21, 2]},
                        {**base, "special_tokens_mask": [1, 0, 1, 1, 1, 0, 1]},
                        {**base, "special_tokens_mask": [1, 0, 1, 1, 0, 0]},
                        {**base, "special_tokens_mask": [1, 0, 1, 1, -1, 0, 1]}):
            with self.assertRaises(ValueError):
                repair.verified_pair_masks(SimpleNamespace(**changed), [10], [20, 21], expected_special_tokens=4)


class ActualTokenizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from transformers import AutoTokenizer
        cls.encoder = HERE / "artifacts/news_impact_v2_bundle/models/embedding"
        if not (cls.encoder / "tokenizer.json").is_file():
            raise unittest.SkipTest("Pinned local tokenizer artifact is unavailable")
        cls.tokenizer = AutoTokenizer.from_pretrained(str(cls.encoder), local_files_only=True, trust_remote_code=False)

    def assert_preserved(self, source, config=None):
        config = {"candidate": "B", **(config or {})}
        effective = v2.configuration(config)
        base_config = {key: value for key, value in effective.items() if key not in ("candidate", "label_policy")}
        baseline, base_coverage = v1.tokenize_document(self.tokenizer, source, base_config)
        fixed, coverage = repair.tokenize_document(self.tokenizer, source, config)
        self.assertEqual(coverage, base_coverage)
        self.assertEqual(len(fixed), len(baseline))
        if not fixed:
            self.assertFalse(coverage["full_coverage"])
            return coverage
        target_ids = self.tokenizer.backend_tokenizer.encode(source["company_name"], add_special_tokens=False).ids
        source_ids = self.tokenizer.backend_tokenizer.encode(source["title"] + "\n" + source["body"], add_special_tokens=False).ids
        reconstructed = []
        for (features, weight), (old_features, old_weight) in zip(fixed, baseline):
            self.assertEqual({key: value for key, value in features.items() if key not in ("target_mask", "content_mask")}, old_features)
            self.assertEqual(weight, old_weight)
            self.assertEqual([token for token, flag in zip(features["input_ids"], features["target_mask"]) if flag], target_ids)
            tokens = [token for token, flag in zip(features["input_ids"], features["content_mask"]) if flag]
            self.assertEqual(len(tokens), weight)
            self.assertTrue(all(not (a and b) for a, b in zip(features["target_mask"], features["content_mask"])))
            reconstructed.extend(tokens)
        self.assertEqual(reconstructed, source_ids)
        return coverage

    def test_literal_specials_repeated_target_empty_title_and_multiple_windows(self):
        # ASCII escapes make these exact Korean characters independent of shell encoding.
        company = "\ub450\uc0b0\ubc25\ucea3"
        for source in (row("TargetCompany <s> </s> <mask> [CLS] " * 120 + "LATE EVENT."),
                       row("TargetCompany signed a contract. " * 70, title=""),
                       row(company + " news " + company + " profit", company=company),
                       row("", title="Only a title about TargetCompany")):
            self.assert_preserved(source, {"max_tokens": 32})

    def test_unsupported_and_candidate_a_are_unchanged(self):
        self.assert_preserved(row("Long content " * 100), {"max_tokens": 32, "max_windows": 1})
        source = row("Candidate A is unchanged.")
        self.assertEqual(repair.tokenize_document(self.tokenizer, source, {"candidate": "A"}),
                         v2.tokenize_document(self.tokenizer, source, {"candidate": "A"}))

    def test_padding_side_preserves_masks_and_model_logits(self):
        import torch
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_kg_cascade_student import FineTuningTests
        encoder, _ = FineTuningTests.loader(HERE / "artifacts/nonexistent_mask_fixture_encoder")
        config = v2.configuration({"candidate": "B", "max_tokens": 32, "head_dropout": 0.})
        torch.manual_seed(31)
        model = v2._make_model(encoder, config, torch).eval()
        source = row("TargetCompany profits rise. " * 13 + "Late contract.")
        original_side = self.tokenizer.padding_side
        try:
            outputs = []
            for side in ("left", "right"):
                self.tokenizer.padding_side = side
                self.assert_preserved(source, config)
                windows, _ = repair.tokenize_document(self.tokenizer, source, config)
                with torch.inference_mode():
                    outputs.append(model(windows, self.tokenizer, "cpu"))
            self.assertTrue(torch.allclose(outputs[0], outputs[1], rtol=1e-5, atol=1e-6))
        finally:
            self.tokenizer.padding_side = original_side

    def test_all_actual_train_and_dev_inputs_preserve_frozen_tokens(self):
        # Deliberately fixed TRAIN/DEV paths; no calibration/test inputs are read.
        sample = HERE / "artifacts/kg_cascade_v1_20260917_v1/samples/attempt_001"
        paths = {fold: sample / fold / "inputs.jsonl" for fold in ("train", "dev")}
        if not all(path.is_file() for path in paths.values()):
            self.skipTest("The approved actual TRAIN/DEV fixtures are unavailable")
        pinned = {name: v2.preview.file_sha256(HERE / name) for name in ("kg_cascade_student.py", "kg_cascade_student_v2.py")}
        for fold, path in paths.items():
            file_sha = v2.preview.file_sha256(path)
            sources = v2.preview.read_jsonl(path)
            stats = {"pairs": len(sources), "supported": 0, "unsupported": 0, "windows": 0,
                     "content_tokens": 0, "multiwindow_pairs": 0, "max_windows": 0, "input_sha256": file_sha}
            for source in sources:
                coverage = self.assert_preserved(source)
                stats["supported" if coverage["full_coverage"] else "unsupported"] += 1
                stats["windows"] += coverage["windows_processed"]
                stats["content_tokens"] += coverage["covered_content_tokens"]
                stats["multiwindow_pairs"] += int(coverage["windows_processed"] > 1)
                stats["max_windows"] = max(stats["max_windows"], coverage["windows_processed"])
            self.assertEqual(file_sha, v2.preview.file_sha256(path))
            FULL_REGRESSION[fold] = stats
        self.assertEqual(pinned, {name: v2.preview.file_sha256(HERE / name) for name in pinned})
        FULL_REGRESSION["frozen_code_sha256"] = pinned
        FULL_REGRESSION["no_calibration_or_test_access"] = True
        FULL_REGRESSION["cuda_used"] = False


if __name__ == "__main__":
    unittest.main()
