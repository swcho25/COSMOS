"""Streaming global identities, revisions, prior UUID conflicts and atomic resume."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lake_backfill_state import BackfillState, PriorIdentityConflict, semantic_hash
from lake_document_ids import assign_document_ids


class BackfillStateTests(unittest.TestCase):
    def row(self, record, *, body=None, url=None, at="2026-09-14T12:00:00Z", source="archive", **extra):
        body = body if body is not None else f"Distinct document {record} detailed financial report. " * 4
        normalized = " ".join(body.split())
        row = {"record_id": str(record), "source_dataset": source, "title": "Company report",
               "body": body, "published_at": at, "url": url, "canonical_url": url,
               "region": "domestic", "language": "ko",
               "content_hash": hashlib.sha256(normalized.encode()).hexdigest()}
        return row | extra

    def ref(self, number=0):
        return {"shard": "source-001.bin", "offset": number * 100, "length": 100}

    def prior(self, key, document_id=None, **extra):
        return {"identity_key": key, "document_id": document_id or str(uuid.uuid4()),
                "representative_record_id": "recent-record", "representative_source_dataset": "recent",
                "available_at": "2026-09-14T12:00:00Z", **extra}

    def test_global_components_match_existing_batch_algorithm(self):
        long_a = "Company A disclosed a supply partnership with company B. " * 4
        long_b = "Company B reported audited results and a new product launch. " * 4
        rows = [self.row("a", body=long_a, url="https://example.com/a", at="2026-09-12T00:00:00Z"),
                self.row("b", body=long_b, url="https://example.com/b"),
                self.row("bridge", body=long_b, url="https://example.com/a"), self.row("separate")]
        expected, mapping, _ = assign_document_ids(rows)
        for order in (rows, list(reversed(rows))):
            with BackfillState(":memory:") as state:
                for i, row in enumerate(order):
                    state.ingest(row, self.ref(i))
                actual = list(state.iter_representatives())
                self.assertEqual({r["document_id"]: (r["record_id"], r["duplicate_count"]) for r in actual},
                                 {r["document_id"]: (r["record_id"], r["duplicate_count"]) for r in expected})
                self.assertEqual(list(state.iter_identity_map()), mapping)
                self.assertEqual(state.summary()["component_count"], 2)

    def test_representative_publication_length_record_and_source_ties(self):
        rows = [self.row("z", body="short", url="https://example.com/shared"),
                self.row("b", body="Longer text", url="https://example.com/shared"),
                self.row("a", body="Longer text", url="https://example.com/shared", source="z-source"),
                self.row("a", body="Longer text", url="https://example.com/shared", source="a-source")]
        with BackfillState(":memory:") as state:
            for i, row in enumerate(rows): state.ingest(row, self.ref(i))
            representative, = state.iter_representatives()
            self.assertEqual((representative["record_id"], representative["source_dataset"]), ("a", "a-source"))
            earlier = self.row("old", body="x", url="https://example.com/shared", at="2012-01-01T00:00:00Z")
            state.ingest(earlier, self.ref(9))
            self.assertEqual(next(state.iter_representatives())["record_id"], "old")

    def test_prior_stream_preserves_old_keys_without_inventing_payload(self):
        document_id = str(uuid.uuid4())
        prior = [self.prior("record:recent:one", document_id), self.prior("record:recent:two", document_id)]
        with BackfillState(":memory:") as state:
            self.assertEqual(state.seed_prior(entry for entry in prior), 2)
            before = state.summary()
            self.assertEqual(state.seed_prior(iter(prior)), 0)
            self.assertEqual(state.summary(), before)
            self.assertEqual(list(state.iter_representatives()), [])
            self.assertEqual(list(state.iter_id_map()), sorted(prior, key=lambda entry: entry["identity_key"]))

    def test_older_article_changes_representative_but_preserves_prior_uuid(self):
        current = self.row("recent", url="https://example.com/shared")
        _, prior, _ = assign_document_ids([current])
        with BackfillState(":memory:") as state:
            state.seed_prior(iter(prior))
            historical = self.row("old", url="https://example.com/shared", at="2011-05-01T01:00:00Z")
            state.ingest(historical, self.ref())
            representative, = state.iter_representatives()
            self.assertEqual(representative["document_id"], prior[0]["document_id"])
            self.assertEqual(representative["record_id"], "old")
            self.assertTrue(all(row["available_at"] == historical["published_at"] for row in state.iter_id_map()))

    def test_prior_bridge_is_atomic_and_explicit_under_both_policies(self):
        a = self.row("a", url="https://example.com/a")
        b = self.row("b", url="https://example.com/b")
        _, prior, _ = assign_document_ids([a, b])
        bridge = self.row("bridge", url=a["url"], body=b["body"])
        for policy in ("raise", "quarantine"):
            with BackfillState(":memory:", prior_conflict_policy=policy) as state:
                state.seed_prior(prior)
                initial_keys = list(state.iter_id_map())
                if policy == "raise":
                    with self.assertRaises(PriorIdentityConflict): state.ingest(bridge, self.ref())
                else:
                    self.assertEqual(state.ingest(bridge, self.ref())["status"], "quarantined")
                self.assertEqual(list(state.iter_id_map()), initial_keys)
                self.assertEqual(list(state.iter_representatives()), [])
                self.assertEqual(state.summary()["version_count"], 0)
                conflict, = state.iter_conflicts()
                self.assertEqual(conflict["kind"], "prior_identity_conflict")
                self.assertEqual(len(conflict["details"]["document_ids"]), 2)

    def test_identical_reingest_is_idempotent_and_revisions_are_not_ignored(self):
        original = self.row("same", body="Original content. " * 8)
        revised = self.row("same", body="An expanded material revision. " * 8)
        with BackfillState(":memory:") as state:
            first = state.ingest(original, self.ref())
            baseline = state.summary()
            again = state.ingest(original | {"source_path": "/a/replayed/copy"}, self.ref(5))
            self.assertEqual(again["status"], "duplicate")
            self.assertEqual(first["version_id"], again["version_id"])
            self.assertEqual(state.summary(), baseline)
            self.assertEqual(state.ingest(revised, self.ref(1))["status"], "version_added")
            representative, = state.iter_representatives()
            self.assertEqual(representative["payload_ref"], self.ref(1))
            self.assertEqual((representative["duplicate_count"], representative["version_count"]), (1, 2))
            conflict, = state.iter_conflicts()
            self.assertEqual(conflict["kind"], "source_revision")
            self.assertEqual(conflict["semantic_hash"], semantic_hash(revised))

    def test_revision_ties_are_deterministic_and_all_versions_remain(self):
        a = self.row("same", body="AAAA")
        b = self.row("same", body="BBBB")
        results = []
        for rows in ((a, b), (b, a)):
            with BackfillState(":memory:") as state:
                for i, row in enumerate(rows): state.ingest(row, self.ref(i))
                representative, = state.iter_representatives()
                results.append((representative["document_id"], representative["semantic_hash"]))
                self.assertEqual(state.summary()["source_revision_count"], 1)
        self.assertEqual(results[0], results[1])

    def test_checkpoint_rolls_back_with_ingest_and_rejects_hash_change(self):
        with BackfillState(":memory:") as state:
            state.ingest(self.row("first"), self.ref())
            state.set_checkpoint("file-1", "sha-1", 1)
            state.commit()
            before = state.summary()
            state.ingest(self.row("second"), self.ref(1))
            state.set_checkpoint("file-1", "sha-1", 2)
            state.rollback()
            self.assertEqual(state.get_checkpoint("file-1"), {"sourcekey": "file-1", "sha256": "sha-1", "row_count": 1})
            self.assertEqual(state.summary(), before)
            state.set_checkpoint("file-1", "sha-1", 1)
            self.assertEqual(state.summary(), before)
            with self.assertRaises(ValueError): state.set_checkpoint("file-1", "different", 2)
            with self.assertRaises(ValueError): state.set_checkpoint("file-1", "sha-1", 0)

    def test_disk_restart_readonly_freeze_and_streamed_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.sqlite"
            row = self.row("restart")
            with BackfillState(path) as state:
                state.ingest(row, self.ref())
                state.set_checkpoint("file", "stable-sha", 1)
                state.commit()
            with BackfillState(path) as state:
                baseline = state.summary()
                self.assertEqual(state.ingest(row, self.ref(20))["status"], "duplicate")
                self.assertEqual(state.summary(), baseline)
                state.finalize(); state.commit()
                fingerprint = state.summary()["fingerprint"]
            with BackfillState(path, read_only=True) as state:
                self.assertTrue(state.summary()["finalized"])
                self.assertEqual(state.summary()["fingerprint"], fingerprint)
                self.assertEqual(next(state.iter_representatives())["payload_ref"], self.ref())
                self.assertEqual([r["identity_key"] for r in state.iter_id_map()],
                                 sorted(r["identity_key"] for r in state.iter_id_map()))
                with self.assertRaises(ValueError): state.ingest(self.row("bad"), self.ref(9))
            with BackfillState(path) as state:
                with self.assertRaises(ValueError): state.set_checkpoint("file", "stable-sha", 2)

    def test_uncaught_prior_conflict_rolls_back_whole_source_but_keeps_failure_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.sqlite"
            a, b = self.row("a", url="https://example.com/a"), self.row("b", url="https://example.com/b")
            _, prior, _ = assign_document_ids([a, b])
            with BackfillState(path) as state:
                state.seed_prior(prior)
                state.set_checkpoint("prior", "prior-sha", len(prior))
            with self.assertRaises(PriorIdentityConflict):
                with BackfillState(path) as state:
                    state.ingest(self.row("partial-file-row"), self.ref())
                    state.ingest(self.row("bridge", url=a["url"], body=b["body"]), self.ref(1))
            with BackfillState(path, read_only=True) as state:
                self.assertEqual(state.summary()["version_count"], 0)
                self.assertEqual(state.summary()["prior_conflict_count"], 1)
                self.assertIsNotNone(state.get_checkpoint("prior"))
                self.assertEqual(list(state.iter_representatives()), [])
                self.assertEqual(len(list(state.iter_conflicts())), 1)

    def test_body_is_not_stored_and_long_body_does_not_expand_database_payload(self):
        marker = "NEVER_STORE_THE_FULL_ARTICLE_BODY_"
        row = self.row("long", body=marker * 40000)
        with BackfillState(":memory:") as state:
            state.ingest(row, self.ref())
            columns = {r[1] for r in state.db.execute("PRAGMA table_info(versions)")}
            self.assertNotIn("body", columns)
            self.assertNotIn("title", columns)
            self.assertLess(sum(len(str(value)) for item in state.db.execute("SELECT * FROM versions") for value in item), 500)
            self.assertNotIn(marker, json.dumps(list(state.iter_representatives())))

    def test_single_pass_generator_seed_and_many_ingests(self):
        with BackfillState(":memory:", cache_mib=1) as state:
            state.seed_prior(self.prior(f"record:prior:{i}", str(uuid.uuid5(uuid.NAMESPACE_URL, str(i)))) for i in range(300))
            for i in range(500): state.ingest(self.row(f"new-{i}"), self.ref(i))
            self.assertEqual(sum(1 for _ in state.iter_representatives()), 500)
            self.assertEqual(state.summary()["source_record_count"], 500)
            self.assertEqual(state.summary()["prior_document_count"], 300)

    def test_semantic_fields_ignore_provenance_and_normalize_timestamp_zone(self):
        row = self.row("one")
        copy = row | {"source_path": "/other", "collected_at": "2026-09-15T00:00:00Z", "published_at": "2026-09-14T21:00:00+09:00"}
        self.assertEqual(semantic_hash(row), semantic_hash(copy))
        self.assertNotEqual(semantic_hash(row), semantic_hash(row | {"language": "en"}))


if __name__ == "__main__":
    unittest.main()
