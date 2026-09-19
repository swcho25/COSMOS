"""CPU checks of full coverage, leakage refusal, actual gradients and reload."""
from pathlib import Path
import copy
import json
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_cascade_student as student
import knowledge_graph_relevance_policy as policy
import knowledge_graph_news_preview as preview


def source(ident="one", body=None, **kwargs):
    return {"article_id": ident, "ticker": "K", "company_name": "Company",
        "published_at": "2021-01-01", "source_url": None, "title": ident + " title",
        "body": body or ident + " economic content", **kwargs}


class Encoding:
    def __init__(self, ids, types=None):
        self.ids, self.type_ids, self.overflowing = ids, types, []
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
    def encode(self, text, add_special_tokens=False):
        return Encoding([ord(x) for x in text])
    def num_special_tokens_to_add(self, pair=True):
        return 3
    def post_process(self, first, second, add_special_tokens=True):
        ids = [1, *first.ids, 2, *second.ids, 3]
        return Encoding(ids, [0] * (len(first.ids) + 2) + [1] * (len(second.ids) + 1))
    def pad(self, rows, padding=True, return_tensors="pt"):
        import torch
        class Batch(dict):
            def to(self, device):
                return Batch({k: v.to(device) for k, v in self.items()})
        width = max(len(row["input_ids"]) for row in rows)
        return Batch({key: torch.tensor([row[key] + [0] * (width - len(row[key])) for row in rows])
                      for key in ("input_ids", "token_type_ids", "attention_mask")})
    def save_pretrained(self, path):
        path.mkdir(parents=True, exist_ok=True)
        (path / "tokenizer.json").write_text('{"fixture":true}', encoding="utf8")


PROTOCOL = {"policy_version": policy.VERSION, "model_repo": "Qwen/Qwen3-4B-Instruct-2507",
    "model_revision": "cdbee75f17c01a7cc42f958dc650907174af0554", "max_input_tokens": 6144,
    "max_new_tokens": 192}


def label(row, relation):
    raw = json.dumps({"relation": relation, "evidence_ids": [1]})
    prediction, errors = policy.parse(raw, row)
    first = {"raw_output": raw, "prediction": prediction, "errors": errors, "input_tokens": 30,
             "output_tokens": 9, "generation_complete": True, "elapsed_seconds": .01}
    second = copy.deepcopy(first) if relation != "unclear" else None
    decision, result_relation, evidence, reason = policy.combine(first, second)
    selection = {"article_id": row["article_id"], "ticker": row["ticker"],
        "input_sha256": preview.canonical_input_sha256(row), "decision": decision,
        "relation": result_relation, "evidence": evidence, "reason": reason,
        "policy_version": policy.VERSION, "model_provenance": {"type": "local_llm_inference",
            "model_repo": PROTOCOL["model_repo"], "model_revision": PROTOCOL["model_revision"]}}
    return {"selection": selection, "stages": {"screen": first, "verify": second}}


class CoverageTests(unittest.TestCase):
    def test_separate_encoder_and_random_head_learning_rates(self):
        config = student.configuration()
        self.assertEqual(config["learning_rate"], 2e-5)
        self.assertEqual(config["head_learning_rate"], 1e-3)
        self.assertEqual(student.configuration({"head_learning_rate": .002})["head_learning_rate"], .002)
        for bad in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                student.configuration({"head_learning_rate": bad})

    def test_late_text_is_covered_once_and_target_repeated(self):
        row = source(body="a" * 700 + "late relevant fact")
        windows, coverage = student.tokenize_document(CharacterTokenizer(), row)
        restored = []
        n = len(row["company_name"])
        for pair, weight in windows:
            self.assertEqual(pair["input_ids"][1:n + 1], [ord(x) for x in row["company_name"]])
            content = pair["input_ids"][n + 2:-1]
            self.assertEqual(len(content), weight)
            restored += content
        self.assertEqual(restored, [ord(x) for x in row["title"] + "\n" + row["body"]])
        self.assertTrue(coverage["full_coverage"])
        self.assertFalse(coverage["truncated"])
        self.assertGreater(coverage["windows_processed"], 1)

    def test_over_limit_does_not_return_a_prefix_probability(self):
        windows, coverage = student.tokenize_document(CharacterTokenizer(), source(body="a" * 600), {"max_windows": 1})
        self.assertEqual(windows, [])
        self.assertEqual(coverage["covered_content_tokens"], 0)
        self.assertFalse(coverage["full_coverage"])
        self.assertEqual(coverage["fallback_reasons"], ["unsupported_document_window_count"])

    def test_metadata_and_labels_do_not_enter_encoder(self):
        row = source()
        other = dict(row, published_at="2099-12-31", article_id="different", ticker="other",
                     source_url="https://future.invalid", human_label="include", future_return=1e9)
        self.assertEqual(student.tokenize_document(CharacterTokenizer(), row),
                         student.tokenize_document(CharacterTokenizer(), other))
        self.assertNotEqual(student.tokenize_document(CharacterTokenizer(), row),
                            student.tokenize_document(CharacterTokenizer(), dict(row, company_name="Different")))

    def test_title_only_and_body_only_are_valid(self):
        for row in (source(body="", title="title only"), source(title="", body="body only")):
            self.assertTrue(student.tokenize_document(CharacterTokenizer(), row)[1]["full_coverage"])

    def test_three_classes_and_replay_tamper_check(self):
        rows = [source("row-" + str(i)) for i in range(3)]
        labels = [label(row, relation) for row, relation in zip(rows, ("direct", "incidental", "unclear"))]
        self.assertEqual(student.validate_teacher_labels(rows, labels[::-1], PROTOCOL), [0, 1, 2])
        labels[0]["selection"]["decision"] = "review"
        with self.assertRaisesRegex(ValueError, "differs from source stages"):
            student.validate_teacher_labels(rows, labels, PROTOCOL)

    def test_split_leakage_across_ticker_id_url_or_content(self):
        train = source("train")
        for dev in (dict(train, ticker="OTHER"),
                    dict(train, article_id="new", ticker="OTHER"),
                    source("new", body="different", source_url="https://example.test/a/")):
            ref = dict(train, source_url="https://example.test/a") if dev["source_url"] else train
            with self.assertRaisesRegex(ValueError, "leakage"):
                student.validate_split_isolation([ref], [dev])
        student.validate_split_isolation([train], [source("unseen")])


class FineTuningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "artifacts" / ("student_cpu_test_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)

    def tearDown(self):
        target = self.root.resolve()
        assert target.parent == (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        shutil.rmtree(target)

    @staticmethod
    def loader(path):
        import torch
        from safetensors.torch import load_file, save_file
        class TinyEncoder(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.config = SimpleNamespace(hidden_size=4)
                # Deliberately non-random so a changed encoder can be checked.
                self.embedding = torch.nn.Embedding(32, 4)
                with torch.no_grad():
                    self.embedding.weight.copy_(torch.arange(128).reshape(32, 4).float() / 128)
                self.dense = torch.nn.Linear(4, 4)
                with torch.no_grad():
                    self.dense.weight.copy_(torch.eye(4)); self.dense.bias.zero_()
            def gradient_checkpointing_enable(self, **kwargs):
                pass
            def forward(self, input_ids, token_type_ids, attention_mask):
                return SimpleNamespace(last_hidden_state=self.dense(self.embedding(input_ids % 32)))
            def save_pretrained(self, path, safe_serialization=True):
                path.mkdir(parents=True, exist_ok=True)
                save_file({k: v.detach().cpu().contiguous() for k, v in self.state_dict().items()}, str(path / "model.safetensors"))
                (path / "config.json").write_text('{"fixture":true}', encoding="utf8")
        encoder = TinyEncoder()
        if (Path(path) / "model.safetensors").exists():
            encoder.load_state_dict(load_file(str(Path(path) / "model.safetensors")))
        return encoder, CharacterTokenizer()

    def dataset(self):
        train = [source("train-" + str(i), body=chr(65 + i) * 30) for i in range(3)]
        dev = [source("dev-" + str(i), body=chr(70 + i) * 30) for i in range(3)]
        relations = ("direct", "incidental", "unclear")
        return train, [label(r, k) for r, k in zip(train, relations)], dev, [label(r, k) for r, k in zip(dev, relations)]

    def test_finetune_changes_encoder_reload_exact_and_tamper_refused(self):
        import torch
        from safetensors.torch import load_file
        trained = self.root / "trained"
        with patch.object(student.legacy, "verified_encoder", return_value=(self.root / "parent", {"fixture": True})), \
             patch.object(student, "_load_encoder", side_effect=self.loader):
            manifest = student.train_model(*self.dataset(), trained, train_protocol=PROTOCOL,
                dev_protocol=PROTOCOL, device="cpu", config={"epochs": 2, "learning_rate": .02, "head_dropout": 0.,
                    "gradient_accumulation_docs": 2, "max_tokens": 32, "window_microbatch": 2})
            before = self.loader(self.root / "parent")[0].state_dict()
            after = load_file(str(trained / "checkpoint/encoder/model.safetensors"))
            self.assertTrue(any(not torch.equal(before[key], after[key]) for key in before))
            self.assertTrue(manifest["feature_protocol"]["encoder_finetuned"])
            self.assertFalse(manifest["graph_materialization_ready"])
            rows = [source("fresh", body="new useful facts")]
            first, second = student.predict(rows, trained, device="cpu"), student.predict(rows, trained, device="cpu")
            self.assertEqual(first[0]["probabilities"], second[0]["probabilities"])
            self.assertAlmostEqual(sum(first[0]["probabilities"].values()), 1., places=6)
            self.assertIsNone(first[0]["evidence"])
            long = student.predict([source("long", body="a" * 3000)], trained, device="cpu")[0]
            self.assertIsNone(long["probabilities"])
            self.assertTrue(long["requires_fallback"])
            with (trained / "checkpoint/encoder/config.json").open("a", encoding="utf8") as stream:
                stream.write(" ")
            with self.assertRaisesRegex(ValueError, "checkpoint bytes changed"):
                student.predict(rows, trained, device="cpu")

    def test_late_window_has_encoder_gradient(self):
        import torch
        encoder, tokenizer = self.loader(self.root / "parent")
        config = student.configuration({"max_tokens": 32, "window_microbatch": 1, "head_dropout": 0.})
        model = student._make_model(encoder, config, torch)
        windows, _ = student.tokenize_document(tokenizer, source(body="a" * 100 + "Z"), config)
        model(windows, tokenizer, "cpu").sum().backward()
        self.assertGreater(encoder.embedding.weight.grad[ord("Z") % 32].abs().sum().item(), 0.)

    def test_changed_source_cannot_publish_complete_checkpoint(self):
        pinned = self.root / "source.json"
        pinned.write_text("{}", encoding="utf8")
        expected = preview.file_sha256(pinned)
        saved = student._save_model
        def mutate_after_saving(*args):
            saved(*args)
            pinned.write_text('{"changed":true}', encoding="utf8")
        out = self.root / "must_remain_incomplete"
        with patch.object(student.legacy, "verified_encoder", return_value=(self.root / "parent", {"fixture": True})), \
             patch.object(student, "_load_encoder", side_effect=self.loader), \
             patch.object(student, "_save_model", side_effect=mutate_after_saving):
            with self.assertRaisesRegex(ValueError, "source files changed"):
                student.train_model(*self.dataset(), out, train_protocol=PROTOCOL, dev_protocol=PROTOCOL,
                    device="cpu", config={"epochs": 1}, source_files={str(pinned): expected})
        self.assertFalse((out / "training_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
