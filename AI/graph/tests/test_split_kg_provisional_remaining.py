"""CPU fixtures for immutable paused-prefix snapshots and two-PC suffix queues."""
from contextlib import closing
import gzip
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_kg_provisional_bulk as fixtures
import split_kg_provisional_remaining as splitter
import kg_provisional_bulk as bulk
import knowledge_graph_news_preview as preview
import knowledge_graph_reselection_worker as durable


class RemainingSplitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProvisionalTests(methodName="runTest")
        self.fixture.setUp()
        self.root, self.source, self.run = self.fixture.root, self.fixture.source, self.fixture.out
        self.rows = [dict(article_id="a" + str(i), ticker="A", company_name="기업", published_at=None,
                          title="기업 계약", body="원문 경제적 계약 내용 " + str(i) * (i + 1), source_url=None)
                     for i in range(12)]
        self.out = self.root / "split"
        self.write_source()

    def tearDown(self):
        self.fixture.tearDown()

    def write_source(self, compressed=False):
        protocol = bulk.read(self.source / "protocol.json")
        protocol.update(source_inventory_fingerprint="fixture-unique-source", source_inventory={"fixture": True})
        durable.atomic_json(self.source / "protocol.json", protocol)
        files, start = [], 0
        for index, count in enumerate((3, 4, 3, 2)):
            path = self.source / ("part" + str(index) + (".jsonl.gz" if compressed else ".jsonl"))
            payload = b"".join((preview.canonical(row) + "\n").encode("utf8") for row in self.rows[start:start + count])
            path.write_bytes(gzip.compress(payload) if compressed else payload)
            files.append({"path": path.name, "bytes": path.stat().st_size, "sha256": preview.file_sha256(path), "rows": count})
            start += count
        manifest = {"schema": durable.MANIFEST_SCHEMA, "complete": True,
            "source_inventory_fingerprint": "fixture-unique-source",
            "protocol_sha256": preview.file_sha256(self.source / "protocol.json"), "files": files,
            "expected_unique_pairs": 12, "counts": {"mention_unique_articles": 12}}
        durable.atomic_json(self.source / "manifest.json", manifest)
        durable.atomic_json(self.source / "_VERIFIED", {"manifest_sha256": preview.file_sha256(self.source / "manifest.json")})

    def pause(self, pairs=4):
        self.fixture.run_pass(max_pairs=pairs)
        (self.run / "STOP").write_bytes(b"Keep paused across both child jobs.\n")
        self.fixture.run_pass(resume=True, stop_file=self.run / "STOP")

    def split(self):
        with patch.object(bulk, "model_contract", side_effect=self.fixture.contract):
            return splitter.split_remaining(self.source / "manifest.json", self.run, self.out)

    def fingerprint_original(self):
        return {str(path.relative_to(self.root)): preview.file_sha256(path) for folder in (self.source, self.run)
                for path in folder.rglob("*") if path.is_file() and path.suffix not in (".lock",)
                and not path.name.endswith(("-wal", "-shm"))}

    def test_partial_prefix_split_preserves_source_and_maps_all_remaining_offsets(self):
        self.pause()
        before = self.fingerprint_original()
        plan = self.split()
        self.assertEqual(before, self.fingerprint_original())
        self.assertEqual(plan["base"]["committed_unique_pairs"], 4)
        self.assertEqual(plan["remaining_unique_pairs"], 8)
        self.assertEqual(sum(part["expected_unique_pairs"] for part in plan["partitions"]), 8)
        self.assertFalse(plan["production_approved"])
        self.assertFalse(plan["validated_auto_selection"])
        receipt = bulk.read(self.out / "split_plan_receipt.json")
        self.assertEqual(receipt["plan_sha256"], preview.file_sha256(self.out / "split_plan.json"))
        for key in ("db", "protocol", "status", "model_binding"):
            self.assertEqual(plan["base"][key + "_sha256"], preview.file_sha256(self.out / plan["base"][key + "_path"]))
        with closing(splitter._readonly(self.out / plan["base"]["db_path"])) as db:
            bulk.validate_state(db, bulk.read(self.out / plan["base"]["protocol_path"]))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0], 4)
        observed = []
        for part in plan["partitions"]:
            manifest_path = self.out / part["input_manifest"]
            self.assertEqual(durable.inventory(manifest_path)["expected_unique_pairs"], part["expected_unique_pairs"])
            protocol = bulk.read(manifest_path.parent / "protocol.json")
            self.assertEqual(protocol["model_receipt_sha256"], bulk.read(self.source / "protocol.json")["model_receipt_sha256"])
            self.assertEqual(protocol["code_sha256"], bulk.read(self.source / "protocol.json")["code_sha256"])
            for mapping in part["mappings"]:
                self.assertNotEqual(mapping["parent_path"], "part0.jsonl")
                with (manifest_path.parent / mapping["child_path"]).open("rb") as child, \
                     (self.source / mapping["parent_path"]).open("rb") as parent:
                    parent.seek(mapping["parent_start_byte"])
                    for raw in child:
                        self.assertEqual(raw, parent.readline())
                        observed.append(bulk.shared.transfer.strict_json(raw.decode("utf8"))["article_id"])
                    self.assertEqual(parent.read(), b"")
        self.assertEqual(sorted(observed), sorted("a" + str(i) for i in range(4, 12)))
        self.assertEqual(len(observed), len(set(observed)))

    def test_compressed_suffix_is_uncompressed_and_untouched_gzip_bytes_preserved(self):
        self.write_source(compressed=True)
        self.pause()
        plan = self.split()
        mappings = [(part, m) for part in plan["partitions"] for m in part["mappings"]]
        suffixes = [(part, m) for part, m in mappings if m["parent_start_row"]]
        self.assertEqual(len(suffixes), 1)
        part, item = suffixes[0]
        self.assertEqual(item["parent_start_row"], 1)
        self.assertTrue(item["child_path"].endswith(".jsonl"))
        child = self.out / Path(part["input_manifest"]).parent / item["child_path"]
        with gzip.open(self.source / item["parent_path"], "rb") as stream:
            stream.seek(item["parent_start_byte"])
            self.assertEqual(child.read_bytes(), stream.read())
        for part, item in mappings:
            if not item["parent_start_row"]:
                self.assertTrue(item["child_path"].endswith(".gz"))
                self.assertEqual(item["child_sha256"], item["parent_sha256"])

    def test_stop_at_shard_eof_does_not_recopy_consumed_file(self):
        self.pause(pairs=3)
        plan = self.split()
        paths = [m["parent_path"] for part in plan["partitions"] for m in part["mappings"]]
        self.assertNotIn("part0.jsonl", paths)
        self.assertEqual(plan["remaining_unique_pairs"], 9)

    def test_full_parent_hash_mismatch_denied_before_copy(self):
        self.pause()
        with (self.source / "part0.jsonl").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaises(ValueError):
            self.split()
        self.assertFalse(self.out.exists())

    def test_status_model_code_and_database_mismatch_are_rejected(self):
        self.pause()
        status = bulk.read(self.run / "status.json")
        durable.atomic_json(self.run / "status.json", {**status, "processed_unique_pairs": 5})
        with self.assertRaises(ValueError):
            self.split()
        durable.atomic_json(self.run / "status.json", status)
        with patch.object(bulk, "code_hashes", return_value={"different": "hash"}), self.assertRaises(ValueError):
            self.split()
        weights = self.fixture.checkpoint / "weights.bin"
        original = weights.read_bytes()
        weights.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.split()
        weights.write_bytes(original)
        with closing(sqlite3.connect(self.run / "state.sqlite3")) as db:
            db.execute("UPDATE files SET offset=offset+1 WHERE path='part1.jsonl'")
            db.commit()
        with self.assertRaises(ValueError):
            self.split()
        self.assertFalse(self.out.exists())

    def test_rehashed_non_line_cursor_is_rejected(self):
        self.pause()
        with closing(sqlite3.connect(self.run / "state.sqlite3")) as db:
            db.execute("UPDATE files SET offset=offset+1 WHERE path='part1.jsonl'")
            durable.save_meta(db, "state_sha256", bulk._state_digest(db))
            db.commit()
        with self.assertRaisesRegex(ValueError, "boundary"):
            self.split()

    def test_existing_output_and_live_worker_lock_are_rejected(self):
        self.pause()
        with durable.WorkerLock(self.run / "provisional.lock"), self.assertRaises((RuntimeError, OSError, ValueError)):
            self.split()
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError, "new split"):
            self.split()


if __name__ == "__main__":
    unittest.main()
