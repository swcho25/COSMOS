"""CPU durable-worker tests using fake token generation, never real GPU/model."""
import contextlib
import gzip
import io
import json
from pathlib import Path
import sqlite3
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_graph_reselection_worker as worker
import local_news_relevance as base
from test_knowledge_graph_batch_inference import classifier, row


class Factory:
    def __init__(self, fail_call=None, counts=None):
        self.instances, self.calls = [], []
        self.fail_call, self.counts = fail_call, counts

    def __call__(self, **kwargs):
        instance = classifier(batch_size=kwargs["batch_size"], token_budget=kwargs["max_batch_tokens"], counts=self.counts)
        receipt_path = worker.HERE / "local_models/relevance_model_receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf8"))
        instance.protocol.update(model_repo=receipt["repo"], model_revision=receipt["revision"],
                                 model_receipt_sha256=base.sha(receipt_path))
        original = instance.classify
        def classify(rows, provenance):
            self.calls.append([x["article_id"] for x in rows])
            result = original(rows, provenance)
            if len(self.calls) == self.fail_call:
                raise KeyboardInterrupt("simulated interruption after inference, before commit")
            return result
        instance.classify = classify
        self.instances.append(instance)
        return instance


class Worker(unittest.TestCase):
    def setUp(self):
        test_root = (worker.HERE / "artifacts").resolve()
        self.root = test_root / ("kg_worker_test_" + uuid.uuid4().hex)
        if not self.root.resolve().is_relative_to(test_root):
            raise ValueError("Test cleanup must stay inside artifacts")
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.out = self.root / "run"

    def manifest(self, shards, expected=None, compressed=False):
        folder = self.root / "inputs"
        folder.mkdir(exist_ok=True)
        parts = []
        for index, rows in enumerate(shards):
            path = folder / (f"part-{index}.txt.gz" if compressed else f"part-{index}.txt")
            payload = "".join(base.canonical(x) + "\n" for x in rows).encode("utf8")
            if compressed:
                with gzip.open(path, "wb") as stream:
                    stream.write(payload)
            else:
                path.write_bytes(payload)
            parts.append({"path": path.relative_to(self.root).as_posix(), "sha256": base.sha(path),
                          "rows": len(rows), "bytes": path.stat().st_size})
        dataset_protocol = {"model_receipt_sha256": base.sha(worker.HERE / "local_models/relevance_model_receipt.json"),
            "code_sha256": {name: base.sha(worker.HERE / name) for name in
                            ("knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py")}}
        worker.atomic_json(self.root / "protocol.json", dataset_protocol)
        value = {"schema": worker.MANIFEST_SCHEMA, "complete": True, "files": parts,
                 "protocol_sha256": base.sha(self.root / "protocol.json"),
                 "expected_unique_pairs": expected or len({(x["article_id"], x["ticker"]) for rows in shards for x in rows})}
        path = self.root / "manifest.json"
        worker.atomic_json(path, value)
        worker.atomic_json(self.root / "_VERIFIED", {"manifest_sha256": base.sha(path)})
        return path

    def run_job(self, manifest, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return worker.run_worker(manifest, self.out, **kwargs)

    def test_multi_shard_model_loaded_once_and_events_drained(self):
        manifest = self.manifest([[row(1), row(2)], [row(3)]])
        factory = Factory()
        result = self.run_job(manifest, chunk_size=1, classifier_factory=factory)
        self.assertTrue(result["complete"])
        self.assertEqual(result["processed_unique_pairs"], 3)
        self.assertEqual(result["completed_files"], 2)
        self.assertEqual(len(factory.instances), 1)
        self.assertEqual(factory.instances[0].batch_events, [])
        self.assertEqual(len((self.out / "results.jsonl").read_text(encoding="utf8").splitlines()), 3)
        with contextlib.closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batch_events").fetchone()[0], 6)

    def test_bounded_run_pauses_then_resumes_without_reinference(self):
        manifest = self.manifest([[row(1), row(2), row(3)]])
        first = Factory()
        result = self.run_job(manifest, chunk_size=2, max_pairs=1, classifier_factory=first)
        self.assertEqual(result["state"], "paused_limit")
        self.assertFalse(result["complete"])
        self.assertFalse((self.out / "completion_receipt.json").exists())
        second = Factory()
        result = self.run_job(manifest, resume=True, chunk_size=2, classifier_factory=second)
        self.assertTrue(result["complete"])
        self.assertEqual(second.calls, [["2", "3"]])
        third = Factory()
        result = self.run_job(manifest, resume=True, chunk_size=2, classifier_factory=third)
        self.assertTrue(result["complete"])
        self.assertEqual(third.instances, [])

    def test_interrupted_uncommitted_chunk_retries_and_committed_chunk_survives(self):
        manifest = self.manifest([[row(1), row(2), row(3), row(4)]])
        failed = Factory(fail_call=2)
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(manifest, chunk_size=2, classifier_factory=failed)
        current = json.loads((self.out / "status.json").read_text(encoding="utf8"))
        self.assertEqual((current["state"], current["processed_unique_pairs"]), ("interrupted", 2))
        second = Factory()
        result = self.run_job(manifest, resume=True, chunk_size=2, classifier_factory=second)
        self.assertTrue(result["complete"])
        self.assertEqual(second.calls, [["3", "4"]])
        with contextlib.closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 2)

    def test_changed_input_bytes_fail_before_runtime(self):
        manifest = self.manifest([[row(1), row(2)]])
        self.run_job(manifest, max_pairs=1, classifier_factory=Factory())
        with (self.root / "inputs/part-0.txt").open("ab") as stream:
            stream.write(b" ")
        factory = Factory()
        with self.assertRaisesRegex(ValueError, "bytes/hash differ"):
            self.run_job(manifest, resume=True, classifier_factory=factory)
        self.assertFalse(factory.instances)

    def test_changed_manifest_or_protocol_settings_fail(self):
        manifest = self.manifest([[row(1), row(2)]])
        self.run_job(manifest, max_pairs=1, classifier_factory=Factory())
        with self.assertRaisesRegex(ValueError, "protocol differs"):
            self.run_job(manifest, resume=True, chunk_size=1, classifier_factory=Factory())
        protocol = json.loads((self.out / "protocol.json").read_text(encoding="utf8"))
        protocol["max_new_tokens"] = 191
        worker.atomic_json(self.out / "protocol.json", protocol)
        with self.assertRaisesRegex(ValueError, "protocol differs"):
            self.run_job(manifest, resume=True, classifier_factory=Factory())

    def test_identical_duplicate_pairs_reused_but_all_source_rows_consumed(self):
        manifest = self.manifest([[row(1), row(1)], [row(1), row(2)]], expected=2)
        factory = Factory()
        result = self.run_job(manifest, chunk_size=1, classifier_factory=factory)
        self.assertEqual((result["processed_unique_pairs"], result["consumed_source_rows"]), (2, 4))
        self.assertEqual(factory.calls, [["1"], ["2"]])

    def test_conflicting_duplicate_in_pending_chunk_fails_without_commits(self):
        conflict = row(1)
        conflict["body"] += " Changed input."
        manifest = self.manifest([[row(1), conflict]], expected=1)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.run_job(manifest, classifier_factory=Factory())
        with contextlib.closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0], 0)
        self.assertFalse((self.out / "completion_receipt.json").exists())

    def test_conflicting_duplicate_against_committed_pair_fails(self):
        conflict = row(1)
        conflict["title"] += " Different title."
        manifest = self.manifest([[row(1)], [conflict]], expected=1)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.run_job(manifest, chunk_size=1, classifier_factory=Factory())
        with contextlib.closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0], 1)

    def test_gzip_cursor_resume_and_length_review(self):
        manifest = self.manifest([[row(1), row(2)]], compressed=True)
        self.run_job(manifest, max_pairs=1, classifier_factory=Factory(counts={1: 6145}))
        result = self.run_job(manifest, resume=True, classifier_factory=Factory(counts={1: 6145}))
        self.assertTrue(result["complete"])
        self.assertEqual(result["review_pairs"], 1)
        records = [json.loads(x) for x in (self.out / "results.jsonl").read_text(encoding="utf8").splitlines()]
        self.assertEqual(records[0]["stages"]["screen"]["errors"], ["input_too_long_full_article_not_processed"])

    def test_row_count_mismatch_cannot_complete(self):
        manifest = self.manifest([[row(1), row(2)]])
        value = json.loads(manifest.read_text(encoding="utf8"))
        value["files"][0]["rows"] = 3
        worker.atomic_json(manifest, value)
        worker.atomic_json(self.root / "_VERIFIED", {"manifest_sha256": base.sha(manifest)})
        with self.assertRaisesRegex(ValueError, "row count differs"):
            self.run_job(manifest, classifier_factory=Factory())
        self.assertFalse((self.out / "completion_receipt.json").exists())

    def test_file_lock_rejects_second_worker_and_releases_after_exit(self):
        lock = self.root / "lock"
        with worker.WorkerLock(lock):
            with self.assertRaisesRegex(RuntimeError, "Another worker"):
                with worker.WorkerLock(lock):
                    self.fail("lock admitted a second owner")
        with worker.WorkerLock(lock):
            pass

    def test_incomplete_manifest_marker_rejected_before_model(self):
        manifest = self.manifest([[row(1)]])
        worker.atomic_json(self.root / "_VERIFIED", {"manifest_sha256": "0" * 64})
        factory = Factory()
        with self.assertRaisesRegex(ValueError, "verification marker"):
            self.run_job(manifest, classifier_factory=factory)
        self.assertFalse(factory.instances)

    def test_runtime_version_change_on_resume_fails_before_new_inference(self):
        manifest = self.manifest([[row(1), row(2)]])
        self.run_job(manifest, max_pairs=1, classifier_factory=Factory())
        factory = Factory()
        def changed(**kwargs):
            instance = factory(**kwargs)
            instance.protocol["versions"] = {"torch": "changed-runtime"}
            return instance
        with self.assertRaisesRegex(ValueError, "versions differ"):
            self.run_job(manifest, resume=True, classifier_factory=changed)
        self.assertEqual(factory.calls, [])

    def test_snapshot_change_and_missing_protocol_block_resume(self):
        manifest = self.manifest([[row(1), row(2)]])
        self.run_job(manifest, max_pairs=1, classifier_factory=Factory())
        snapshot = self.out / "sources/knowledge_graph_reselection_worker.py"
        original = snapshot.read_bytes()
        snapshot.write_bytes(original + b"\n# changed\n")
        with self.assertRaisesRegex(ValueError, "snapshot differs"):
            self.run_job(manifest, resume=True, classifier_factory=Factory())
        snapshot.write_bytes(original)
        (self.out / "protocol.json").unlink()
        with self.assertRaisesRegex(ValueError, "original frozen protocol"):
            self.run_job(manifest, resume=True, classifier_factory=Factory())

    def test_wrong_dataset_model_policy_or_evidence_rejected_before_gpu(self):
        for field in ("model_receipt_sha256", "knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py"):
            with self.subTest(field=field):
                manifest = self.manifest([[row(1)]])
                protocol_path = self.root / "protocol.json"
                protocol = json.loads(protocol_path.read_text(encoding="utf8"))
                if field == "model_receipt_sha256":
                    protocol[field] = "0" * 64
                else:
                    protocol["code_sha256"][field] = "0" * 64
                worker.atomic_json(protocol_path, protocol)
                value = json.loads(manifest.read_text(encoding="utf8"))
                value["protocol_sha256"] = base.sha(protocol_path)
                worker.atomic_json(manifest, value)
                worker.atomic_json(self.root / "_VERIFIED", {"manifest_sha256": base.sha(manifest)})
                factory = Factory()
                with self.assertRaisesRegex(ValueError, "Dataset pinned"):
                    self.run_job(manifest, classifier_factory=factory)
                self.assertEqual(factory.instances, [])

    def test_dataset_protocol_raw_hash_mismatch_rejected_before_gpu(self):
        manifest = self.manifest([[row(1)]])
        with (self.root / "protocol.json").open("a", encoding="utf8") as stream:
            stream.write("\n")
        factory = Factory()
        with self.assertRaisesRegex(ValueError, "raw-file SHA"):
            self.run_job(manifest, classifier_factory=factory)
        self.assertEqual(factory.instances, [])


if __name__ == "__main__":
    unittest.main()
