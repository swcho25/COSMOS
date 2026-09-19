"""No-GPU tests for compact teacher semantics, full text and durable provenance."""
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_semantic_teacher as teacher
import kg_cascade_student_v2 as student


def source(ident="one", body="Company announced a new factory."):
    return {"article_id": ident, "ticker": "K", "company_name": "Company", "title": ident + " title",
            "body": body, "published_at": "2026-09-17", "source_url": None}


def protocol(pairs=1):
    return {"schema": "cosmos-semantic-teacher-run-1", "policy_version": teacher.VERSION,
        "classes": list(teacher.CLASSES), "model_repo": "Qwen/Qwen3-4B-Instruct-2507",
        "model_revision": "cdbee75f17c01a7cc42f958dc650907174af0554",
        "model_receipt_sha256": teacher.preview.file_sha256(teacher.HERE / "local_models/relevance_model_receipt.json"),
        "prompt_sha256": teacher.digest({"criteria": teacher.CRITERIA, "steps": teacher.STEPS}),
        "code_hashes": teacher.code_hashes(), "input_sha256": "0" * 64, "pairs": pairs,
        "max_input_tokens": teacher.MAX_INPUT_TOKENS, "max_new_tokens": 1,
        "two_answer_blind_passes": True, "truncation": False, "human_gold": False,
        "graph_materialization_ready": False, "grounded_old_protocol_parity": False,
        "postprocessing": teacher.POSTPROCESSING}


def result(row, spec, codes=("A", "A")):
    stages = {}
    for name, code in zip(teacher.STEPS, codes):
        failed = code is None
        stages[name] = {"token": code, "input_tokens": teacher.MAX_INPUT_TOKENS + 1 if failed else 100,
            "output_tokens": 0 if failed else 1, "full_coverage": not failed,
            "errors": ["input_too_long"] if failed else [], "seconds": 0.,
            "messages_sha256": teacher.digest(teacher.messages(row, name)), "prompt_sha256": "1" * 64}
    return {"article_id": row["article_id"], "ticker": row["ticker"],
        "input_sha256": teacher.preview.canonical_input_sha256(row), "stages": stages,
        **teacher.guarded_decision(row, stages), "protocol_sha256": teacher.digest(spec),
        "human_gold": False, "graph_materialization_ready": False}


class FakeTensor:
    def __init__(self, count): self.shape = (1, count)
    def to(self, device): return self


class FakeTokenizer:
    eos_token_id = 99
    def __init__(self, count=100): self.count, self.prompts, self.tokenization = count, [], []
    def apply_chat_template(self, messages, **kwargs):
        self.prompts.append(copy.deepcopy(messages))
        return teacher.preview.canonical(messages)
    def __call__(self, prompt, **kwargs):
        self.tokenization.append((prompt, kwargs))
        return {"input_ids": FakeTensor(self.count)}


class FakeModel:
    def __init__(self): self.calls = []
    def generate(self, **kwargs):
        self.calls.append(kwargs)
        allowed = kwargs["prefix_allowed_tokens_fn"](0, None)
        class Output:
            def __getitem__(self, key): return SimpleNamespace(tolist=lambda: [allowed[0]])
        return Output()


def fake_teacher(count=100):
    obj = teacher.SemanticTeacher.__new__(teacher.SemanticTeacher)
    obj.ids = {"A": 1, "B": 2, "C": 3, "D": 4}
    obj.tokenizer, obj.model = FakeTokenizer(count), FakeModel()
    class FakeOOM(Exception): pass
    obj.torch = SimpleNamespace(inference_mode=nullcontext, cuda=SimpleNamespace(
        synchronize=lambda: None, empty_cache=lambda: None, OutOfMemoryError=FakeOOM))
    obj.metadata = {"fixture": "no_gpu"}
    return obj


class SemanticTeacherTests(unittest.TestCase):
    def test_canonical_surface_respects_word_and_subsidiary_boundaries(self):
        for name, text in (("ARM", "There is serious harm to the community."),
                           ("삼성전자", "삼성전자서비스는 별도 계약을 발표했다."),
                           ("삼성전자", "삼성전자에너지는 별도 법인이다."),
                           ("LG", "LG전자는 신규 공장을 발표했다."),
                           ("삼성전자", "가삼성전자는 이름이 다른 회사다.")):
            with self.subTest(name=name, text=text):
                self.assertFalse(teacher.literal_canonical_surface(name, text))
                row = {**source(body=text), "company_name": name}
                spec = protocol()
                deferred = result(row, spec, ("C", "C"))
                self.assertEqual(deferred["raw_decision"], "exclude")
                self.assertEqual(deferred["decision"], "review")
                teacher.validate_result(deferred, row, spec)
                self.assertEqual(result(row, spec, ("A", "A"))["decision"], "include")
                deferred["guard_details"]["literal_canonical_surface_found"] = True
                with self.assertRaisesRegex(ValueError, "explicit guards"):
                    teacher.validate_result(deferred, row, spec)

    def test_canonical_surface_keeps_particles_spacing_and_legal_suffixes(self):
        for name, text in (("삼성전자", "삼성전자는 공급 계약을 발표했다."),
                           ("삼성전자", "삼성전자에서 생산을 시작했다."),
                           ("삼성전자", "삼 성 전 자의 생산실적이 증가했다."),
                           ("LG화학", "LG 화학은 신규 공장을 발표했다."),
                           ("ARM", "ARM's new contract was announced."),
                           ("Apple Inc.", "Apple announced earnings."),
                           ("Samsung Electronics", "Samsung Electronics reported profit.")):
            with self.subTest(name=name, text=text):
                self.assertTrue(teacher.literal_canonical_surface(name, text))
        self.assertFalse(teacher.literal_canonical_surface("(주)", "새 공급 계약"))

    def test_identity_absence_can_only_defer_exclusion(self):
        row = source(body="An old issuer name signed a supply contract.")
        spec = protocol()
        value = result(row, spec, ("C", "C"))
        self.assertEqual(value["raw_decision"], "exclude")
        self.assertEqual(value["decision"], "review")
        self.assertEqual(value["decision_source"], "canonical_identity_abstention")
        teacher.validate_result(value, row, spec)
        positive = result(row, spec, ("A", "A"))
        self.assertEqual(positive["decision"], "include")
        failed = result(row, spec, (None, None))
        self.assertIsNone(failed["decision"])

    def test_complete_tape_rule_preserves_raw_llm_error_and_refuses_added_event(self):
        from test_kg_tape_only_guard import LG, LG_TITLE
        row = {**source(body=LG), "company_name": "LG화학", "title": LG_TITLE}
        spec = protocol()
        value = result(row, spec, ("A", "A"))
        self.assertEqual(value["raw_decision"], "include")
        self.assertEqual(value["decision"], "exclude")
        self.assertEqual(value["decision_source"], "strict_full_tape_template_rule")
        teacher.validate_result(value, row, spec)
        changed = {**row, "body": row["body"] + " LG화학은 신규 공급 계약을 발표했다."}
        corrected = result(changed, spec, ("A", "A"))
        self.assertEqual(corrected["decision"], "include")
        self.assertFalse(corrected["guard_details"]["tape_only_matched"])
        value["raw_decision"] = "exclude"
        with self.assertRaises(ValueError):
            teacher.validate_result(value, row, spec)

    def setUp(self):
        self.root = teacher.HERE / "artifacts" / ("semantic_teacher_cpu_test_" + uuid.uuid4().hex)
        self.root.mkdir()
    def tearDown(self):
        target = self.root.resolve()
        assert target.parent == (teacher.HERE / "artifacts").resolve()
        shutil.rmtree(target)

    def test_direct_indirect_agreement_is_include_and_conflict_is_review(self):
        row, spec = source(), protocol()
        cases = {("A", "A"): "include", ("A", "B"): "include", ("B", "A"): "include",
                 ("B", "B"): "include", ("C", "C"): "exclude", ("A", "C"): "review",
                 ("C", "B"): "review", ("D", "D"): "review", ("A", "D"): "review"}
        for codes, expected in cases.items():
            record = result(row, spec, codes)
            teacher.validate_result(record, row, spec)
            self.assertEqual(record["decision"], expected)

    def test_unsupported_has_no_semantic_label_and_v2_omits_it(self):
        rows = [source(str(i)) for i in range(4)]
        spec = protocol(4)
        labels = [result(row, spec, codes) for row, codes in zip(rows, (("A", "B"), ("C", "C"), ("D", "D"), (None, "A")))]
        self.assertEqual(teacher.validate_labels(rows, labels[::-1], spec), [0, 1, 2, None])
        self.assertEqual(student.resolve_training_labels(rows, labels, spec, student.configuration()), [0, 1, 2, None])

    def test_both_prompts_retain_late_text_without_first_answer(self):
        row = source(body="Unrelated background. " * 2000 + "TAIL: Company sells its subsidiary.")
        model = fake_teacher()
        record = model.classify(row, protocol())
        self.assertEqual(record["decision"], "include")
        self.assertEqual(len(model.tokenizer.prompts), 2)
        self.assertNotEqual(model.tokenizer.prompts[0], model.tokenizer.prompts[1])
        for messages in model.tokenizer.prompts:
            self.assertEqual([message["role"] for message in messages], ["system", "user"])
            payload = json.loads(messages[1]["content"].split("분석할 데이터(JSON):\n", 1)[1])
            self.assertEqual(payload["body"], row["body"])
            self.assertEqual(set(payload), {"company_name", "ticker", "published_at", "title", "body"})
        self.assertTrue(all(args["truncation"] is False for _, args in model.tokenizer.tokenization))
        self.assertTrue(all(call["max_new_tokens"] == 1 and call["do_sample"] is False for call in model.model.calls))

    def test_overlength_does_not_generate_or_turn_into_negative(self):
        model = fake_teacher(teacher.MAX_INPUT_TOKENS + 1)
        record = model.classify(source(), protocol())
        self.assertIsNone(record["decision"])
        self.assertEqual(model.model.calls, [])
        self.assertTrue(all(stage["errors"] == ["input_too_long"] for stage in record["stages"].values()))

    def test_rehashed_changed_execution_contract_is_still_rejected(self):
        row, spec = source(), protocol()
        changes = {"schema": "other", "code_hashes": {}, "max_input_tokens": 9999, "max_new_tokens": 192,
                   "truncation": True, "two_answer_blind_passes": False, "model_receipt_sha256": "1" * 64,
                   "grounded_old_protocol_parity": True, "human_gold": True, "prompt_sha256": "0" * 64,
                   "postprocessing": {}}
        for field, value in changes.items():
            altered = dict(spec, **{field: value})
            forged = result(row, altered)
            with self.subTest(field=field), self.assertRaises(ValueError):
                teacher.validate_result(forged, row, altered)

    def test_changed_input_or_stage_messages_rejected(self):
        row, spec = source(), protocol()
        record = result(row, spec)
        with self.assertRaisesRegex(ValueError, "input mismatch"):
            teacher.validate_result(record, dict(row, body=row["body"] + " changed"), spec)
        record["stages"]["verify"]["messages_sha256"] = record["stages"]["screen"]["messages_sha256"]
        with self.assertRaisesRegex(ValueError, "messages differ"):
            teacher.validate_result(record, row, spec)

    def test_invalid_output_and_duplicate_or_missing_pairs_rejected(self):
        row, spec = source(), protocol()
        record = result(row, spec, (None, "A"))
        record["stages"]["screen"]["output_tokens"] = 1
        with self.assertRaisesRegex(ValueError, "Failed teacher"):
            teacher.validate_result(record, row, spec)
        valid = result(row, spec)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            teacher.validate_labels([row, row], [valid, valid], spec)
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            teacher.validate_labels([source("different")], [valid], spec)

    def test_resume_commits_once_and_rejects_changed_source(self):
        inputs, out = self.root / "inputs.jsonl", self.root / "run"
        rows = [source(str(i)) for i in range(3)]
        inputs.write_text("".join(teacher.preview.canonical(row) + "\n" for row in rows), encoding="utf8")
        class InterruptedTeacher:
            metadata = {"fixture": "no_gpu"}
            calls = 0
            def classify(self, row, spec):
                InterruptedTeacher.calls += 1
                if InterruptedTeacher.calls == 2:
                    raise RuntimeError("synthetic interruption")
                return result(row, spec)
        with patch.object(teacher, "SemanticTeacher", InterruptedTeacher), self.assertRaisesRegex(RuntimeError, "synthetic"):
            teacher.run(inputs, out)
        status = json.loads((out / "status.json").read_text(encoding="utf8"))
        self.assertEqual((status["state"], status["processed_pairs"]), ("FAILED", 1))
        class ResumedTeacher:
            metadata = {"fixture": "no_gpu"}
            calls = 0
            def classify(self, row, spec):
                ResumedTeacher.calls += 1
                return result(row, spec)
        with patch.object(teacher, "SemanticTeacher", ResumedTeacher):
            teacher.run(inputs, out)
        self.assertEqual(ResumedTeacher.calls, 2)
        receipt = json.loads((out / "completion_receipt.json").read_text(encoding="utf8"))
        self.assertEqual((receipt["pairs"], receipt["resumed_pairs"]), (3, 1))
        first_sha = teacher.preview.file_sha256(out / "results.jsonl")
        with patch.object(teacher, "SemanticTeacher", side_effect=AssertionError("must not reload finished model")):
            teacher.run(inputs, out)
        self.assertEqual(teacher.preview.file_sha256(out / "results.jsonl"), first_sha)
        inputs.write_text(inputs.read_text(encoding="utf8").replace("new factory", "different factory"), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "changed teacher inputs/protocol"):
            teacher.run(inputs, out)


if __name__ == "__main__":
    unittest.main()
