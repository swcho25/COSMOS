"""Bounded Parquet conversion and durable recovery of committed lake identities."""
import contextlib
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid
import zlib

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recover_lake_normalization as recovery
from lake_backfill_state import BackfillState
from lake_news_normalize import normalize_article
from prepare_lake_recent import map_news


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")


def source(path, kind):
    return {"path": path.as_posix(), "bytes": path.stat().st_size,
            "modified_ms": path.stat().st_mtime_ns // 1_000_000, "kind": kind,
            "id": hashlib.sha256(path.as_posix().encode()).hexdigest()[:24]}


def prepared_row(number, **extra):
    return {"record_id": str(number), "source_dataset": "prepared_fixture",
            "title": "기업 실적 발표 " + str(number), "body": "새로운 실적과 투자 계획을 발표했다. " + str(number),
            "url": "https://example.test/article/" + str(number),
            "published_at": "2020-06-01T10:30:00Z", "language": "ko", **extra}


def old_spark_clean(raw, item):
    """Independent equivalent of the old Spark loop on synthetic Row.asDict data."""
    record = json.loads(json.dumps(raw | {"source_path": item["path"]}, default=str, ensure_ascii=False))
    clean = normalize_article(map_news(record, item["kind"]))
    if clean is None or recovery.stamp(clean["published_at"]) >= recovery.stamp("2026-09-16T00:00:00Z"):
        return None
    clean = {key: clean.get(key) for key in recovery.KEEP}
    clean["record_id"] = str(clean["record_id"])
    return clean


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture_root = Path(__file__).resolve().parents[1] / "artifacts/recovery_test_tmp"
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.root = self.fixture_root / ("case-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.old, self.new = self.root / "old", self.root / "recovered"
        self.old.mkdir()
        (self.old / ".runner.lock").write_bytes(b"0")
        write_json(self.old / "status.json", {"state": "TERMINATED", "runner_pid": None, "child_pid": None})
        (self.old / "inputs").mkdir()
        (self.old / "inputs/document_id_map.jsonl").write_bytes(b"")
        roots = {"news_prepared": (self.root / "prepared").as_posix(),
                 "news_live": (self.root / "live").as_posix(),
                 "news_realtime": (self.root / "realtime").as_posix()}
        self.raw = [[prepared_row(0)],
                    [prepared_row(1), prepared_row(2, published_at="2020-06-02"),
                     prepared_row(3, published_at=None), prepared_row(4, published_at="2027-01-01T00:00:00Z")],
                    [prepared_row(5)]]
        paths = [Path(roots["news_prepared"]) / "region=domestic/year=2020/month=06" / (str(i) + ".parquet") for i in range(3)]
        for path, rows in zip(paths, self.raw):
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(rows), path, row_group_size=1)
        self.sources = [source(path, "news_prepared") for path in paths]
        self.manifest = {"schema_version": "lake-backfill-sources-1", "as_of": "2026-09-16T00:00:00Z",
                         "roots": roots, "files": self.sources, "controls": [], "source_file_count": 3,
                         "source_bytes": sum(s["bytes"] for s in self.sources)}
        write_json(self.old / "source_manifest.json", self.manifest)
        first = self.sources[0]
        clean = old_spark_clean(self.raw[0][0] | {"region": "domestic", "year": 2020, "month": 6}, first)
        data = zlib.compress(json.dumps(clean, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(), 3)
        payload = self.old / "normalized/original.bin"
        payload.parent.mkdir()
        payload.write_bytes(data)
        quarantine = self.old / "quarantine/original.jsonl"
        quarantine.parent.mkdir()
        quarantine.write_bytes(b"")
        with BackfillState(self.old / "identity.sqlite") as state:
            state.set_checkpoint("prior_id_map", recovery.digest_file(self.old / "inputs/document_id_map.jsonl"), 0)
            state.ingest(clean, {"shard": payload.name, "offset": 0, "length": len(data)})
            state.set_checkpoint(first["id"], recovery.fingerprint(first), 1)
            state.commit()
        receipt = {"source": first, "source_rows": 1, "accepted_rows": 1, "invalid_rows": 0, "outside_as_of_rows": 0,
                   "payload_file": "normalized/original.bin", "payload_sha256": recovery.digest_file(payload),
                   "quarantine_file": "quarantine/original.jsonl"}
        write_json(self.old / "source_receipts" / (first["id"] + ".json"), receipt)
        code = Path(recovery.__file__).parent
        plan = {"code_fingerprints": [{"path": str(code / name), "sha256": recovery.digest_file(code / name)} for name in recovery.CRITICAL],
                "source_fingerprints": [{"path": str(self.old / "source_manifest.json"), "sha256": recovery.digest_file(self.old / "source_manifest.json")}]}
        write_json(self.old / "plan.locked.json", plan)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.fixture_root.resolve()))
        shutil.rmtree(self.root)

    def prepare(self):
        return recovery.prepare(self.old, self.new, batch_size=1, minimum_free_gb=0)

    def decoded_representatives(self):
        result = {}
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            for row in state.iter_representatives():
                ref = row["payload_ref"]
                with (self.new / "normalized" / ref["shard"]).open("rb") as stream:
                    stream.seek(ref["offset"])
                    article = json.loads(zlib.decompress(stream.read(ref["length"])))
                result[row["record_id"]] = article
        return result

    def test_hive_and_bounded_batches_match_old_cleaned_rows(self):
        item = self.sources[1]
        with mock.patch.object(pq, "read_table", side_effect=AssertionError("whole table forbidden")):
            rows = list(recovery.parquet_rows(item["path"], item, self.manifest["roots"]["news_prepared"], 1))
        self.assertEqual(len(rows), 4)
        for actual, original in zip(rows, self.raw[1]):
            self.assertEqual(actual["region"], "domestic")
            self.assertEqual(actual["year"], 2020)
            self.assertEqual(actual["month"], 6)
            expected = old_spark_clean(original | {"region": "domestic", "year": 2020, "month": 6}, item)
            self.assertEqual(old_spark_clean(actual, item), expected)

    def test_spark_timestamp_decimal_binary_serialization(self):
        path = self.root / "timestamps.parquet"
        stamp = dt.datetime(2020, 6, 1, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))
        pq.write_table(pa.table({"aware": pa.array([stamp], type=pa.timestamp("us", tz="Asia/Seoul")),
                                "naive": [dt.datetime(2020, 6, 1, 10, 30)], "day": [dt.date(2020, 6, 1)],
                                "amount": [Decimal("1.25")], "binary": [b"abc"]}), path)
        item = source(path, "news_live")
        row, = recovery.parquet_rows(path, item, "unused", 1)
        self.assertEqual(row["aware"], "2020-06-01 01:30:00")
        self.assertEqual(row["naive"], "2020-06-01 10:30:00")
        self.assertEqual((row["day"], row["amount"], row["binary"]), ("2020-06-01", "1.25", "bytearray(b'abc')"))

    def test_checkpoint_resume_and_normalized_identity_equivalence(self):
        original = {str(p.relative_to(self.old)): recovery.digest_file(p) for p in self.old.rglob("*") if p.is_file()}
        prepared = self.prepare()
        self.assertEqual(prepared["inherited_files"], 1)
        paused = recovery.normalize(self.new, recovery.LocalFiles(), max_files=1)
        self.assertEqual(paused["state"], "PAUSED")
        self.assertEqual(paused["counts"], {"source_rows": 5, "accepted_rows": 3, "invalid_rows": 1, "outside_as_of_rows": 1, "completed_files": 2})
        done = recovery.normalize(self.new, recovery.LocalFiles())
        self.assertEqual(done["state"], "COMPLETED")
        self.assertFalse(done["publish_enabled"])
        self.assertEqual(done["counts"]["accepted_rows"], 4)
        expected = {}
        with BackfillState(self.root / "expected.sqlite") as state:
            for group, item in zip(self.raw, self.sources):
                for raw in group:
                    clean = old_spark_clean(raw | {"region": "domestic", "year": 2020, "month": 6}, item)
                    if clean:
                        expected[clean["record_id"]] = clean
                        state.ingest(clean, {"shard": "baseline.bin", "offset": 0, "length": 1})
            identities = list(state.iter_identity_map())
        self.assertEqual(self.decoded_representatives(), expected)
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            self.assertEqual(list(state.iter_identity_map()), identities)
        before = sorted(p.name for p in (self.new / "normalized").iterdir())
        self.assertEqual(recovery.normalize(self.new, recovery.LocalFiles())["state"], "COMPLETED")
        self.assertEqual(sorted(p.name for p in (self.new / "normalized").iterdir()), before)
        for relative, digest in original.items():
            self.assertEqual(recovery.digest_file(self.old / relative), digest, relative)

    def test_failure_rolls_back_whole_source_and_retry_does_not_duplicate(self):
        self.prepare()
        def failing(*args):
            yield next(recovery.parquet_rows(*args))
            raise RuntimeError("injected iterator failure")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            recovery.normalize(self.new, recovery.LocalFiles(), row_reader=failing)
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            self.assertIsNone(state.get_checkpoint(self.sources[1]["id"]))
            self.assertEqual(len(list(state.iter_representatives())), 1)
        self.assertEqual(recovery.read_json(self.new / "recovery_status.json")["state"], "FAILED")
        result = recovery.normalize(self.new, recovery.LocalFiles())
        self.assertEqual(result["counts"]["accepted_rows"], 4)
        self.assertEqual(len(self.decoded_representatives()), 4)

    def test_source_change_before_commit_rolls_back(self):
        self.prepare()
        class ChangedAfterRead(recovery.LocalFiles):
            @contextlib.contextmanager
            def materialize(inner, item, cache):
                inner.check(item)
                yield Path(item["path"])
                raise ValueError("source changed during reading")
        with self.assertRaisesRegex(ValueError, "source changed"):
            recovery.normalize(self.new, ChangedAfterRead())
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            self.assertIsNone(state.get_checkpoint(self.sources[1]["id"]))
            self.assertEqual(len(list(state.iter_representatives())), 1)

    def test_rejects_running_original_before_creating_recovery(self):
        write_json(self.old / "status.json", {"state": "RUNNING"})
        with self.assertRaisesRegex(ValueError, "Stop the original"):
            self.prepare()
        self.assertFalse(self.new.exists())

    def test_rejects_old_plan_source_fingerprint_change(self):
        with (self.old / "source_manifest.json").open("a") as stream:
            stream.write(" ")
        with self.assertRaisesRegex(ValueError, "Frozen file changed"):
            self.prepare()
        self.assertFalse(self.new.exists())

    def test_rejects_reused_or_nested_run(self):
        with self.assertRaisesRegex(ValueError, "new, disjoint"):
            recovery.prepare(self.old, self.old / "nested", minimum_free_gb=0)
        self.prepare()
        with self.assertRaisesRegex(ValueError, "new, disjoint"):
            self.prepare()

    def test_modified_inherited_payload_is_detected(self):
        self.prepare()
        (self.new / "normalized/original.bin").write_bytes(b"tamper")
        with self.assertRaisesRegex(ValueError, "Frozen file changed"):
            recovery.normalize(self.new, recovery.LocalFiles())

    def test_modified_pending_source_is_detected(self):
        self.prepare()
        with Path(self.sources[1]["path"]).open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "Frozen local source changed"):
            recovery.normalize(self.new, recovery.LocalFiles())

    def test_hive_conflict_fails_without_overwriting_source_value(self):
        item = self.sources[2]
        pq.write_table(pa.Table.from_pylist([prepared_row(5, region="overseas")]), item["path"])
        with self.assertRaisesRegex(ValueError, "partition conflict"):
            list(recovery.parquet_rows(item["path"], item, self.manifest["roots"]["news_prepared"], 1))

    def test_limit_zero_pauses_without_mutating_identity(self):
        self.prepare()
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            before = state.summary()
        result = recovery.normalize(self.new, recovery.LocalFiles(), max_files=0)
        self.assertEqual((result["state"], result["counts"]["completed_files"]), ("PAUSED", 1))
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            self.assertEqual(state.summary(), before)

    def test_probe_compares_old_payload_and_does_not_ingest_new_source(self):
        self.prepare()
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            before = state.summary()
        old = recovery.probe(self.new, recovery.LocalFiles(), self.sources[0]["id"])
        fresh = recovery.probe(self.new, recovery.LocalFiles(), self.sources[1]["id"])
        self.assertIs(old["committed_payload_comparison"], True)
        self.assertIsNone(fresh["committed_payload_comparison"])
        self.assertEqual(fresh["counts"]["accepted_rows"], 2)
        with BackfillState(self.new / "identity.sqlite", read_only=True) as state:
            self.assertEqual(state.summary(), before)
            self.assertIsNone(state.get_checkpoint(self.sources[1]["id"]))

    def test_probe_disagreement_is_persisted_and_fails(self):
        self.prepare()
        with mock.patch.object(recovery, "compressed", return_value=b"changed"):
            with self.assertRaisesRegex(ValueError, "differs from the committed"):
                recovery.probe(self.new, recovery.LocalFiles(), self.sources[0]["id"])
        receipt, = (self.new / "probes").glob("*.json")
        self.assertIs(recovery.read_json(receipt)["committed_payload_comparison"], False)

    def test_webhdfs_uses_only_read_operations_and_removes_own_cache(self):
        client = recovery.WebHDFS("http://example.invalid:9870", "ubuntu")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {"FileStatus": {"type": "FILE", "length": 3, "modificationTime": 1}}
        response.iter_content.return_value = iter([b"abc"])
        client.session.get = mock.Mock(return_value=response)
        cache = self.root / "cache"
        cache.mkdir()
        item = {"path": "/datasets/news/test.parquet", "bytes": 3, "modified_ms": 1}
        with client.materialize(item, cache) as path:
            self.assertEqual(path.read_bytes(), b"abc")
        self.assertEqual(list(cache.iterdir()), [])
        operations = [call.kwargs["params"]["op"] for call in client.session.get.call_args_list]
        self.assertEqual(operations, ["GETFILESTATUS", "OPEN", "GETFILESTATUS", "GETFILESTATUS"])

    def test_webhdfs_download_stops_at_frozen_size(self):
        client = recovery.WebHDFS("http://example.invalid:9870")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {"FileStatus": {"type": "FILE", "length": 3, "modificationTime": 1}}
        response.iter_content.return_value = iter([b"abcdef"])
        client.session.get = mock.Mock(return_value=response)
        cache = self.root / "cache"
        cache.mkdir()
        with self.assertRaisesRegex(ValueError, "frozen length"):
            with client.materialize({"path": "/datasets/test", "bytes": 3, "modified_ms": 1}, cache):
                self.fail("Oversized file was yielded")
        self.assertEqual(list(cache.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
