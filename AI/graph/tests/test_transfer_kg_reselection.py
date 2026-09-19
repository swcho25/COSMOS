"""No-network/no-GPU tests for archive trust, locking and coordinator resume."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import transfer_kg_reselection as transfer


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.root = HERE / "artifacts" / ("kg_transfer_test_" + uuid.uuid4().hex)
        self.root.mkdir()

    def tearDown(self):
        if not self.root.resolve().is_relative_to((HERE / "artifacts").resolve()):
            raise ValueError("Test cleanup escaped workspace artifacts")
        shutil.rmtree(self.root)

    def archive(self, *, name="inputs/part-00000.txt", content=None, mutate_manifest=None,
                extra=None, marker_sha=None, protocol_body=None, duplicate=False, link=False):
        data = encoded({"article_id": "a", "ticker": "A", "company_name": "기업", "published_at": None,
                        "title": "뉴스", "body": "본문", "source_url": None})
        protocol = encoded({"schema": "test-protocol", "truncation": False})
        manifest = {"schema": "cosmos-kg-candidate-export-1", "complete": True,
                    "protocol_sha256": digest(protocol), "expected_unique_pairs": 1,
                    "files": [{"path": name, "sha256": digest(data), "bytes": len(data), "rows": 1}]}
        if mutate_manifest:
            mutate_manifest(manifest)
        raw = encoded(manifest)
        entries = [("manifest.json", raw), ("protocol.json", protocol if protocol_body is None else protocol_body),
                   ("_VERIFIED", encoded({"manifest_sha256": marker_sha or digest(raw)})),
                   (name, data if content is None else content)]
        if extra:
            entries.append(extra)
        if duplicate:
            entries.append(entries[-1])
        archive = self.root / "archive.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path, value in entries:
                member = tarfile.TarInfo(path)
                member.size = len(value)
                if link and path == name:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "../../escape"
                    member.size = 0
                tar.addfile(member, io.BytesIO(value) if member.isfile() else None)
        receipt = {"archive_sha256": transfer.sha(archive), "archive_bytes": archive.stat().st_size,
                   "manifest_sha256": digest(raw)}
        return archive, receipt, manifest, data

    def test_complete_exact_extract_and_idempotent_resume(self):
        archive, receipt, manifest, data = self.archive()
        destination = self.root / "source"
        self.assertEqual(transfer.extract_verified(archive, destination, receipt), manifest)
        self.assertEqual((destination / "inputs/part-00000.txt").read_bytes(), data)
        self.assertEqual(transfer.extract_verified(archive, destination, receipt), manifest)

    def test_corrupt_archive_or_bad_metadata_fails_before_extract(self):
        archive, receipt, _, _ = self.archive()
        for bad in (dict(receipt, archive_bytes=True), dict(receipt, archive_sha256="0" * 64),
                    dict(receipt, manifest_sha256="x")):
            with self.assertRaises(ValueError):
                transfer.extract_verified(archive, self.root / "source", bad)
        self.assertFalse((self.root / "source").exists())

    def test_path_traversal_absolute_drive_backslash_and_controls_as_inputs_rejected(self):
        for name in ("inputs/../../escape.txt", "/escape.txt", "C:/escape.txt", "inputs\\escape.txt",
                     "inputs/./part.txt", "inputs//part.txt", "manifest.json"):
            with self.subTest(name=name):
                archive, receipt, _, _ = self.archive(name=name)
                with self.assertRaises(ValueError):
                    transfer.extract_verified(archive, self.root / "source", receipt)
                self.assertFalse((self.root / "source").exists())

    def test_unlisted_duplicate_and_symlink_members_rejected(self):
        for options in ({"extra": ("inputs/extra.txt", b"malicious")}, {"duplicate": True}, {"link": True}):
            archive, receipt, _, _ = self.archive(**options)
            with self.assertRaises(ValueError):
                transfer.extract_verified(archive, self.root / "source", receipt)

    def test_manifest_duplicate_paths_rows_and_shas_rejected(self):
        mutations = [lambda m: m["files"].append(copy.deepcopy(m["files"][0])),
                     lambda m: m["files"][0].update(rows=True),
                     lambda m: m["files"][0].update(sha256="not-a-sha"),
                     lambda m: m.update(expected_unique_pairs=2),
                     lambda m: m.update(complete=False)]
        for mutation in mutations:
            archive, receipt, _, _ = self.archive(mutate_manifest=mutation)
            with self.assertRaises(ValueError):
                transfer.extract_verified(archive, self.root / "source", receipt)

    def test_wrong_protocol_and_completion_marker_rejected(self):
        for options in ({"marker_sha": "f" * 64}, {"protocol_body": b"changed"}):
            archive, receipt, _, _ = self.archive(**options)
            with self.assertRaises(ValueError):
                transfer.extract_verified(archive, self.root / "source", receipt)
            self.assertFalse((self.root / "source/_VERIFIED").exists())

    def test_wrong_input_hash_never_exposes_completion_marker(self):
        archive, receipt, _, data = self.archive()
        changed = data.replace(b'"a"', b'"b"', 1)
        archive, receipt, _, _ = self.archive(content=changed)
        with self.assertRaises(ValueError):
            transfer.extract_verified(archive, self.root / "source", receipt)
        self.assertFalse((self.root / "source/_VERIFIED").exists())

    def test_existing_changed_input_is_not_overwritten(self):
        archive, receipt, _, data = self.archive()
        destination = self.root / "source"
        transfer.extract_verified(archive, destination, receipt)
        target = destination / "inputs/part-00000.txt"
        target.write_bytes(b"do not overwrite")
        with self.assertRaises(ValueError):
            transfer.extract_verified(archive, destination, receipt)
        self.assertEqual(target.read_bytes(), b"do not overwrite")

    def test_duplicate_json_keys_and_nonfinite_numbers_rejected(self):
        for text in ('{"key":1,"key":2}', '{"value":NaN}'):
            with self.assertRaises(ValueError):
                transfer.strict_json(text)

    def test_os_lock_blocks_second_coordinator_then_releases(self):
        path = self.root / "lock"
        with transfer.CoordinatorLock(path):
            with self.assertRaises(RuntimeError):
                with transfer.CoordinatorLock(path):
                    self.fail("two coordinators acquired the same lock")
        with transfer.CoordinatorLock(path):
            pass

    def coordinator_args(self):
        return SimpleNamespace(remote_run="/home/ubuntu/ner-work/kg_reselection_test",
                               out=self.root / "run", batch_size=1, max_batch_tokens=8192, max_pairs=None)

    def test_status_and_manifest_timeouts_retry_and_existing_worker_resumes(self):
        args = self.coordinator_args()
        args.out.mkdir()
        (args.out / "candidate_inputs.tar.gz").write_bytes(b"already downloaded")
        (args.out / "inference").mkdir()
        ready = subprocess.CompletedProcess([], 0, json.dumps({"state": "READY_FOR_DOWNLOAD"}))
        receipt = dict(archive_sha256="a" * 64, manifest_sha256="b" * 64, archive_bytes=18)
        fetched = subprocess.CompletedProcess([], 0, json.dumps(receipt))
        answers = [subprocess.TimeoutExpired("ssh", 45), ready, subprocess.TimeoutExpired("ssh", 45), fetched]
        with patch.object(transfer, "ssh", side_effect=answers) as remote, \
                patch.object(transfer.time, "sleep") as sleep, \
                patch.object(transfer, "extract_verified", return_value={"expected_unique_pairs": 12}), \
                patch.object(transfer.subprocess, "run") as worker:
            transfer.run(args)
        self.assertEqual(remote.call_count, 4)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("--resume", worker.call_args.args[0])
        self.assertEqual(json.loads((args.out / "transfer_status.json").read_text())["state"], "WORKER_EXITED")

    def test_server_failure_is_not_swallowed_or_launched(self):
        args = self.coordinator_args()
        failed = subprocess.CompletedProcess([], 0, json.dumps({"state": "FAILED"}))
        with patch.object(transfer, "ssh", return_value=failed), patch.object(transfer.subprocess, "run") as worker:
            with self.assertRaises(RuntimeError):
                transfer.run(args)
        worker.assert_not_called()
        self.assertEqual(json.loads((args.out / "transfer_error.json").read_text())["type"], "RuntimeError")

    def test_download_only_never_starts_full_inference(self):
        args = self.coordinator_args()
        args.download_only = True
        args.out.mkdir()
        (args.out / "candidate_inputs.tar.gz").write_bytes(b"existing archive")
        answers = [subprocess.CompletedProcess([], 0, json.dumps({"state": "READY_FOR_DOWNLOAD"})),
                   subprocess.CompletedProcess([], 0, json.dumps(dict(
                       archive_sha256="a" * 64, manifest_sha256="b" * 64, archive_bytes=16)))]
        with patch.object(transfer, "ssh", side_effect=answers), \
                patch.object(transfer, "extract_verified", return_value={"expected_unique_pairs": 2656806}), \
                patch.object(transfer.subprocess, "run") as worker:
            result = transfer.run(args)
        worker.assert_not_called()
        self.assertEqual(result["expected_unique_pairs"], 2656806)
        status = json.loads((args.out / "transfer_status.json").read_text())
        self.assertEqual(status["state"], "DOWNLOAD_VERIFIED")
        self.assertIs(status["inference_started"], False)

    def test_rejected_second_coordinator_does_not_write_shared_error(self):
        args = self.coordinator_args()
        args.out.mkdir()
        with transfer.CoordinatorLock(args.out / ".transfer.lock"):
            with self.assertRaises(RuntimeError):
                transfer.run(args)
        self.assertFalse((args.out / "transfer_error.json").exists())


if __name__ == "__main__":
    unittest.main()
