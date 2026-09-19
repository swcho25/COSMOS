"""CPU-only tests for resumable, explicitly provisional existing-B selection.

All articles/checkpoints are tiny synthetic fixtures. No real CAL/TEST, GPU,
teacher generation, production writes, or existing model changes are performed.
"""
from contextlib import closing
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
import uuid
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import kg_provisional_bulk as bulk
import knowledge_graph_news_preview as preview
import knowledge_graph_reselection_worker as durable
import knowledge_graph_batch_inference as teacher_runtime


class FakeStudent:
    def __init__(self, checkpoint, *, device, owner):
        self.owner = owner
        self.manifest_sha256 = preview.file_sha256(Path(checkpoint) / "training_manifest.json")
        owner.loads += 1
        owner.devices.append(device)

    def predict(self, rows):
        self.owner.calls += 1
        self.owner.attempted.append([preview.pair_key(row) for row in rows])
        if self.owner.calls == self.owner.fail_call:
            raise KeyboardInterrupt("synthetic interruption before chunk commit")
        result = []
        for row in rows:
            index = int(row["article_id"][1:])
            winner = ("include", "exclude", "review")[index % 3]
            scores = {label: .05 for label in ("include", "exclude", "review")}
            scores[winner] = .9
            value = {"article_id": row["article_id"], "ticker": row["ticker"],
                "input_sha256": preview.canonical_input_sha256(row),
                "student_manifest_sha256": self.manifest_sha256,
                "model_version": bulk.MODEL_VERSION,
                "label_policy": "kg_semantic_teacher_v1",
                "probabilities": scores,
                "coverage": {"full_coverage": True, "encoding_completed": True, "truncated": False,
                             "total_content_tokens": 12, "covered_content_tokens": 12},
                "requires_fallback": False, "fallback_reasons": [], "evidence": None}
            if self.owner.alter:
                value = self.owner.alter(row, value)
            result.append(value)
        if self.owner.after_predict:
            self.owner.after_predict()
        return result

    def close(self):
        self.owner.closes += 1


class Factory:
    def __init__(self, fail_call=None, alter=None, after_predict=None):
        self.fail_call, self.alter, self.after_predict = fail_call, alter, after_predict
        self.loads = self.calls = self.closes = 0
        self.attempted, self.devices = [], []

    def __call__(self, checkpoint, *, device):
        return FakeStudent(checkpoint, device=device, owner=self)


class ProvisionalTests(unittest.TestCase):
    def setUp(self):
        self.root = HERE / "artifacts" / ("provisional_test_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.source, self.checkpoint, self.out = self.root / "source", self.root / "model", self.root / "output"
        self.source.mkdir()
        self.checkpoint.mkdir()
        self.rows = [dict(article_id="a" + str(i), ticker="A", company_name="기업", published_at=None,
                          title="기업 계약", body="원문 계약 내용 " + str(i), source_url=None) for i in range(6)]
        durable.atomic_json(self.checkpoint / "training_manifest.json", {"complete": True, "fake": True})
        (self.checkpoint / "weights.bin").write_bytes(b"tiny synthetic weights")
        self.model_sha = preview.file_sha256(self.checkpoint / "training_manifest.json")
        self.write_source(self.rows)

    def tearDown(self):
        if not self.root.resolve().is_relative_to((HERE / "artifacts").resolve()):
            raise ValueError("Test cleanup escaped workspace artifacts")
        shutil.rmtree(self.root)

    def write_source(self, rows, expected=None, expected_articles=None):
        path = self.source / "inputs.jsonl"
        with path.open("w", encoding="utf8", newline="\n") as stream:
            for row in rows:
                stream.write(preview.canonical(row) + "\n")
        protocol = {"model_receipt_sha256": preview.file_sha256(HERE / "local_models/relevance_model_receipt.json"),
                    "code_sha256": {name: preview.file_sha256(HERE / name) for name in
                                    ("knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py")}}
        durable.atomic_json(self.source / "protocol.json", protocol)
        manifest = {"schema": durable.MANIFEST_SCHEMA, "complete": True,
                    "protocol_sha256": preview.file_sha256(self.source / "protocol.json"),
                    "files": [{"path": path.name, "bytes": path.stat().st_size,
                               "sha256": preview.file_sha256(path), "rows": len(rows)}],
                    "expected_unique_pairs": len(rows) if expected is None else expected,
                    "counts": {"mention_unique_articles": len({r["article_id"] for r in rows})
                               if expected_articles is None else expected_articles}}
        durable.atomic_json(self.source / "manifest.json", manifest)
        durable.atomic_json(self.source / "_VERIFIED", {"manifest_sha256": preview.file_sha256(self.source / "manifest.json")})

    def query(self, sql):
        with closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            return db.execute(sql).fetchall()

    def prediction(self):
        model = Factory()(self.checkpoint, device="cpu")
        return model.predict([self.rows[0]])[0]

    def contract(self, checkpoint):
        path = Path(checkpoint).resolve()
        return {"checkpoint": str(path),
                "student_manifest_sha256": preview.file_sha256(path / "training_manifest.json"),
                "model_version": "kg-cascade-student-v2-B-pairfix-1",
                "label_policy": "kg_semantic_teacher_v1",
                "checkpoint_files": {"weights.bin": preview.file_sha256(path / "weights.bin")}}

    def run_pass(self, factory=None, **kwargs):
        with patch.object(bulk, "model_contract", side_effect=self.contract), \
             patch.object(teacher_runtime, "_load_runtime", side_effect=AssertionError("No LLM may load")):
            return bulk.run_bulk(self.source / "manifest.json", self.checkpoint, self.out,
                device="cpu", predictor_factory=factory or Factory(), chunk_size=2, shard_size=2, **kwargs)

    def result_rows(self):
        return self.query("SELECT article_id,ticker,input_sha256,decision,result_json FROM pairs ORDER BY article_id,ticker")

    def test_argmax_is_provisional_even_below_validated_routing_confidence(self):
        prediction = self.prediction()
        prediction["probabilities"] = {"include": .36, "exclude": .34, "review": .30}
        result = bulk.interpret(self.rows[0], prediction, self.model_sha)
        self.assertEqual(result["decision"], "include")
        self.assertFalse(result["production_approved"])
        self.assertFalse(result["human_gold"])
        self.assertFalse(result["graph_materialization_ready"])
        preview.canonical(result)

    def test_tie_invalid_score_or_review_never_becomes_auto_exclusion(self):
        scores = [None, {"include": .5, "exclude": .5, "review": 0},
                  {"include": float("nan"), "exclude": 0, "review": 0},
                  {"include": float("inf"), "exclude": 0, "review": 0},
                  {"include": -.1, "exclude": .8, "review": .3},
                  {"include": True, "exclude": 0, "review": 0},
                  {"include": .1, "exclude": .1, "review": .1},
                  {"include": .9, "exclude": .1},
                  {"include": .05, "exclude": .05, "review": .9}]
        for score in scores:
            with self.subTest(score=score):
                prediction = dict(self.prediction(), probabilities=score)
                result = bulk.interpret(self.rows[0], prediction, self.model_sha)
                self.assertEqual(result["decision"], "needs_review")
                preview.canonical(result)  # Nonfinite numbers must still be auditable.

    def test_coverage_failure_is_pending_not_negative(self):
        changes = [{"coverage": {}}, {"requires_fallback": True}, {"fallback_reasons": ["unsupported"]}]
        for field, value in (("full_coverage", False), ("encoding_completed", False),
                             ("truncated", True), ("covered_content_tokens", 11)):
            changes.append({"coverage": {**self.prediction()["coverage"], field: value}})
        for change in changes:
            with self.subTest(change=change):
                result = bulk.interpret(self.rows[0], {**self.prediction(), **change}, self.model_sha)
                self.assertEqual(result["decision"], "needs_review")

    def test_prediction_identity_input_or_checkpoint_mismatch_is_fatal(self):
        for key in ("article_id", "ticker", "input_sha256", "student_manifest_sha256"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                bulk.interpret(self.rows[0], {**self.prediction(), key: "different"}, self.model_sha)

    def test_committed_pause_resume_produces_identical_pair_results(self):
        first = Factory()
        self.run_pass(first, max_pairs=2)
        committed = self.result_rows()
        self.assertEqual(len(committed), 2)
        self.assertFalse((self.out / "provisional_receipt.json").exists())
        second = Factory()
        self.run_pass(second, resume=True)
        resumed = self.result_rows()
        self.assertEqual(len(resumed), 6)
        self.assertEqual(second.calls, 2)
        self.assertEqual(resumed[:2], committed)
        first_out = self.out
        self.out = self.root / "uninterrupted_output"
        self.run_pass()
        self.assertEqual(self.result_rows(), resumed)
        self.assertTrue((first_out / "provisional_receipt.json").exists())

    def test_interruption_rolls_back_pending_chunk_and_cursor_then_retries_once(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_pass(Factory(fail_call=2))
        self.assertEqual(len(self.result_rows()), 2)
        offset, consumed = self.query("SELECT offset,rows_consumed FROM files")[0]
        self.assertEqual(consumed, 2)
        with (self.source / "inputs.jsonl").open("rb") as stream:
            self.assertEqual(offset, len(stream.readline()) + len(stream.readline()))
        second = Factory()
        self.run_pass(second, resume=True)
        self.assertEqual(second.attempted, [[("a2", "A"), ("a3", "A")], [("a4", "A"), ("a5", "A")]])
        self.assertEqual(len(self.result_rows()), 6)

    def test_wrong_output_identity_commits_neither_rows_nor_cursor(self):
        alter = lambda row, prediction: {**prediction, "ticker": "wrong"}
        with self.assertRaises(ValueError):
            self.run_pass(Factory(alter=alter))
        self.assertEqual(self.query("SELECT COUNT(*) FROM pairs")[0][0], 0)
        self.assertEqual(self.query("SELECT offset,rows_consumed FROM files"), [(0, 0)])

    def test_same_pair_duplicates_reused_and_full_source_rows_consumed(self):
        self.write_source([self.rows[0], self.rows[0], self.rows[1], self.rows[0], self.rows[2]],
                          expected=3, expected_articles=3)
        factory = Factory()
        result = self.run_pass(factory)
        self.assertEqual(len(self.result_rows()), 3)
        self.assertEqual(sum(map(len, factory.attempted)), 3)
        self.assertEqual(self.query("SELECT rows_consumed,complete FROM files"), [(5, 1)])
        self.assertEqual(result["processed_unique_pairs"], 3)

    def test_conflicting_duplicate_is_not_silently_reused(self):
        changed = {**self.rows[0], "body": "different economic source"}
        self.write_source([self.rows[0], changed], expected=1, expected_articles=1)
        factory = Factory()
        with self.assertRaises(ValueError):
            self.run_pass(factory)
        self.assertFalse((self.out / "provisional_receipt.json").exists())
        self.assertEqual(factory.loads, 0)

    def test_source_bytes_change_on_resume_denied_before_model_load(self):
        self.run_pass(max_pairs=2)
        with (self.source / "inputs.jsonl").open("a", encoding="utf8") as stream:
            stream.write("{}\n")
        factory = Factory()
        with self.assertRaises(ValueError):
            self.run_pass(factory, resume=True)
        self.assertEqual(factory.loads, 0)

    def test_model_manifest_or_weight_change_on_resume_denied_before_load(self):
        self.run_pass(max_pairs=2)
        for name in ("training_manifest.json", "weights.bin"):
            path = self.checkpoint / name
            old = path.read_bytes()
            try:
                path.write_bytes(old + b" changed")
                factory = Factory()
                with self.subTest(name=name), self.assertRaises(ValueError):
                    self.run_pass(factory, resume=True)
                self.assertEqual(factory.loads, 0)
            finally:
                path.write_bytes(old)

    def test_code_hash_change_on_resume_denied_before_model_load(self):
        self.run_pass(max_pairs=2)
        original = bulk.code_hashes()
        factory = Factory()
        with patch.object(bulk, "code_hashes", return_value={**original, "synthetic_changed.py": "0" * 64}):
            with self.assertRaises(ValueError):
                self.run_pass(factory, resume=True)
        self.assertEqual(factory.loads, 0)

    def test_database_result_tampering_detected_before_resume(self):
        self.run_pass(max_pairs=2)
        with closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            db.execute("UPDATE pairs SET result_json=? WHERE article_id='a0'", ('{"changed":true}',))
            db.commit()
        factory = Factory()
        with self.assertRaises(ValueError):
            self.run_pass(factory, resume=True)
        self.assertEqual(factory.loads, 0)

    def test_wrong_expected_unique_pair_count_prevents_completion_receipt(self):
        self.write_source(self.rows, expected=5)
        with self.assertRaises(ValueError):
            self.run_pass()
        self.assertFalse((self.out / "provisional_receipt.json").exists())

    def test_wrong_expected_unique_article_count_prevents_completion_receipt(self):
        self.write_source(self.rows, expected_articles=7)
        with self.assertRaises(ValueError):
            self.run_pass()
        self.assertFalse((self.out / "provisional_receipt.json").exists())

    def test_preexisting_stop_file_prevents_any_model_load(self):
        stop = self.root / "STOP"
        stop.write_text("stop", encoding="utf8")
        factory = Factory()
        self.run_pass(factory, stop_file=stop)
        self.assertEqual(factory.loads, 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM pairs")[0][0], 0)
        self.assertFalse((self.out / "provisional_receipt.json").exists())

    def test_stop_between_chunks_retains_committed_work_for_resume(self):
        stop = self.root / "STOP"
        first = Factory(after_predict=lambda: stop.write_text("stop", encoding="utf8"))
        self.run_pass(first, stop_file=stop)
        self.assertEqual(len(self.result_rows()), 2)
        self.assertFalse((self.out / "provisional_receipt.json").exists())
        stop.unlink()
        second = Factory()
        self.run_pass(second, stop_file=stop, resume=True)
        self.assertEqual(len(self.result_rows()), 6)
        self.assertEqual(second.calls, 2)

    def test_transaction_exception_rolls_back_pairs_chunks_progress_and_cursor(self):
        original = durable.save_meta
        tripped = []

        def fail_before_transaction_commit(db, key, value):
            if key == "state_sha256" and not tripped and db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]:
                tripped.append(True)
                raise RuntimeError("synthetic transaction failure after inserted results")
            return original(db, key, value)

        with patch.object(durable, "save_meta", side_effect=fail_before_transaction_commit):
            with self.assertRaisesRegex(RuntimeError, "transaction failure"):
                self.run_pass()
        self.assertTrue(tripped)
        self.assertEqual(self.query("SELECT COUNT(*) FROM pairs"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM chunks"), [(0,)])
        self.assertEqual(self.query("SELECT offset,rows_consumed FROM files"), [(0, 0)])
        resumed = Factory()
        self.run_pass(resumed, resume=True)
        self.assertEqual(sum(map(len, resumed.attempted)), 6)
        self.assertEqual(len(self.result_rows()), 6)

    def test_complete_exports_preserve_full_predictions_source_refs_and_provisional_flags(self):
        factory = Factory()
        result = self.run_pass(factory)
        self.assertTrue(result["complete"])
        self.assertEqual(result["state"], "provisional_complete")
        self.assertEqual(result["processed_unique_pairs"], 6)
        self.assertEqual(result["observed_unique_articles"], 6)
        self.assertEqual(result["teacher_calls"], 0)
        self.assertEqual(result["decisions"], {"include": 2, "exclude": 2, "needs_review": 2})
        self.assertEqual((factory.loads, factory.closes, factory.devices), (1, 1, ["cpu"]))
        self.assertFalse((self.out / "release_receipt.json").exists())
        self.assertFalse((self.out / "fast_pass_receipt.json").exists())
        for key, expected in bulk.FLAGS.items():
            self.assertEqual(result[key], expected)
            self.assertIs(type(result[key]), type(expected))
        exported = []
        export_root = self.out / result["outputs"]["root"]
        self.assertEqual(set(result["outputs"]["files"]), set(bulk.DECISIONS))
        for decision, parts in result["outputs"]["files"].items():
            for part in parts:
                path = export_root / part["path"]
                self.assertEqual(preview.file_sha256(path), part["sha256"])
                self.assertEqual(path.stat().st_size, part["bytes"])
                values = preview.read_jsonl(path)
                self.assertEqual(len(values), part["rows"])
                for value in values:
                    self.assertEqual(value["decision"], decision)
                    self.assertFalse(value["teacher_called"])
                    self.assertTrue(value["provisional_decision"])
                    self.assertEqual(value["raw_prediction"]["student_manifest_sha256"], self.model_sha)
                    self.assertEqual(value["evidence_status"], "not_extracted")
                    for key, expected in bulk.FLAGS.items():
                        self.assertEqual(value[key], expected)
                    ref = value["source_ref"]
                    with (self.source / ref["path"]).open("rb") as stream:
                        stream.seek(ref["byte_offset"])
                        row = json.loads(stream.read(ref["line_bytes"]).decode("utf8"))
                    self.assertEqual(preview.pair_key(row), preview.pair_key(value))
                    self.assertEqual(preview.canonical_input_sha256(row), value["input_sha256"])
                exported.extend(values)
        self.assertEqual(len(exported), 6)
        self.assertEqual(len({preview.pair_key(value) for value in exported}), 6)

    def test_complete_resume_loads_no_model_and_reuses_verified_exports(self):
        result = self.run_pass()
        factory = Factory()
        again = self.run_pass(factory, resume=True)
        self.assertEqual(factory.loads, 0)
        self.assertEqual(result["outputs"], again["outputs"])
        self.assertEqual(result["decisions"], again["decisions"])

    def test_modified_export_prevents_completed_resume(self):
        result = self.run_pass()
        part = result["outputs"]["files"]["include"][0]
        path = self.out / result["outputs"]["root"] / part["path"]
        path.write_text("{}\n", encoding="utf8")
        factory = Factory()
        with self.assertRaises(ValueError):
            self.run_pass(factory, resume=True)
        self.assertEqual(factory.loads, 0)

    def test_completed_receipt_cannot_promote_provisional_result_on_resume(self):
        self.run_pass()
        path = self.out / "provisional_receipt.json"
        original = json.loads(path.read_text(encoding="utf8"))
        for key, changed in (("validated_auto_selection", True), ("production_approved", True),
                             ("classification_scope", "validated_selection"), ("teacher_calls", 1)):
            durable.atomic_json(path, {**original, key: changed})
            factory = Factory()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.run_pass(factory, resume=True)
            self.assertEqual(factory.loads, 0)
        durable.atomic_json(path, original)


if __name__ == "__main__":
    unittest.main()
