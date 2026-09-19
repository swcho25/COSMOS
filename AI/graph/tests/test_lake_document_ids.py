"""Stable identity, transitive duplicates and publication-time provenance."""
import copy
import hashlib
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lake_document_ids import DOCUMENT_NAMESPACE, assign_document_ids


LONG_A = "A company announced a completed supply contract with a major customer. " * 3
LONG_B = "A different company reported audited results for the completed fiscal year. " * 3


class DocumentIdentityTests(unittest.TestCase):
    def row(self, record, url=None, body=LONG_A, published_at="2026-09-15T12:00:00Z", **extra):
        body = " ".join(body.split())
        row = {
            "source_dataset": "prepared", "record_id": record, "canonical_url": url,
            "title": "Company news", "body": body, "published_at": published_at,
            "content_hash": hashlib.sha256((body or "Company news").encode()).hexdigest(),
        }
        row.update(extra)
        return row

    def test_url_and_body_transitive_union(self):
        rows = [self.row("a", "https://example.com/a", LONG_A),
                self.row("b", "https://example.com/a", LONG_B),
                self.row("c", "https://other.example/b", LONG_B)]
        representatives, mapping, summary = assign_document_ids(rows)
        self.assertEqual(len(representatives), 1)
        self.assertEqual(representatives[0]["duplicate_count"], 3)
        self.assertEqual(len({entry["document_id"] for entry in mapping}), 1)
        self.assertEqual(summary["duplicate_source_records"], 2)

    def test_short_empty_or_title_only_hash_cannot_merge_articles(self):
        for body in ("", "short", "x" * 79):
            with self.subTest(length=len(body)):
                rows = [self.row("a", body=body), self.row("b", body=body)]
                representatives, mapping, _ = assign_document_ids(rows)
                self.assertEqual(len(representatives), 2)
                self.assertFalse(any(entry["identity_key"].startswith("content:") for entry in mapping))

    def test_body_threshold_uses_normalized_characters(self):
        representatives, _, _ = assign_document_ids([self.row("a", body="x" * 80), self.row("b", body="x" * 80)])
        self.assertEqual(len(representatives), 1)
        row = self.row("a", body="x")
        row["body"] += " " * 100
        representatives, _, _ = assign_document_ids([row, self.row("b", body="x")])
        self.assertEqual(len(representatives), 2)

    def test_new_uuid_is_namespace_and_lexicographic_key_deterministic(self):
        rows = [self.row("b", "https://example.com/b"), self.row("a", "https://example.com/a")]
        representatives, mapping, _ = assign_document_ids(rows)
        expected = str(uuid.uuid5(DOCUMENT_NAMESPACE, min(entry["identity_key"] for entry in mapping)))
        self.assertEqual(representatives[0]["document_id"], expected)
        self.assertEqual(assign_document_ids(list(reversed(rows))), assign_document_ids(rows))

    def test_prior_uuid_survives_new_lexicographically_earlier_alias(self):
        first, mapping, _ = assign_document_ids([self.row("z", body="short", url="https://example.com/a")])
        later = self.row("a", body=LONG_A, url="https://example.com/a")
        # content: sorts before url: or record:, so a fresh UUID would differ.
        self.assertNotEqual(assign_document_ids([later])[0][0]["document_id"], first[0]["document_id"])
        reps, updated, summary = assign_document_ids([later], mapping)
        self.assertEqual(reps[0]["document_id"], first[0]["document_id"])
        self.assertIn("record:prepared:z", {entry["identity_key"] for entry in updated})
        self.assertEqual(summary["reused_document_ids"], 1)

    def test_prior_same_uuid_unions_separate_current_rows(self):
        existing_id = str(uuid.uuid4())
        prior = [{"identity_key": "record:prepared:a", "document_id": existing_id},
                 {"identity_key": "record:prepared:b", "document_id": existing_id}]
        reps, mapping, _ = assign_document_ids([self.row("a", body="one"), self.row("b", body="two")], prior)
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0]["document_id"], existing_id)
        self.assertTrue(all(entry["available_at"] == reps[0]["published_at"] for entry in mapping))

    def test_prior_uuid_conflict_on_duplicate_component_fails(self):
        prior = [{"identity_key": "record:prepared:a", "document_id": str(uuid.uuid4())},
                 {"identity_key": "record:prepared:b", "document_id": str(uuid.uuid4())}]
        with self.assertRaisesRegex(ValueError, "conflicting existing"):
            assign_document_ids([self.row("a"), self.row("b")], prior)

    def test_conflicting_source_rows_fail_and_exact_repeat_is_one_source(self):
        row = self.row("a")
        changed = {**row, "title": "A later headline"}
        with self.assertRaisesRegex(ValueError, "Conflicting rows"):
            assign_document_ids([row, changed])
        reps, _, summary = assign_document_ids([row, dict(row)])
        self.assertEqual(reps[0]["duplicate_count"], 1)
        self.assertEqual(summary["repeated_identical_rows"], 1)
        # Identifiers belonging to separate source datasets do not collide.
        assign_document_ids([row, {**changed, "source_dataset": "realtime"}])

    def test_later_longer_body_never_backdated(self):
        early = self.row("early", "https://example.com/a", body="Original brief.", published_at="2026-09-14T08:00:00Z")
        later = self.row("later", "https://example.com/a", body=LONG_A, published_at="2026-09-15T08:00:00Z")
        reps, mapping, _ = assign_document_ids([later, early])
        self.assertEqual(reps[0]["record_id"], "early")
        self.assertEqual(reps[0]["body"], "Original brief.")
        self.assertTrue(all(entry["available_at"] == early["published_at"] for entry in mapping))

    def test_same_instant_prefers_longest_body_then_record_id(self):
        rows = [self.row("z", "https://example.com/a", body="Short"),
                self.row("b", "https://example.com/a", body=LONG_A),
                self.row("a", "https://example.com/a", body=LONG_A, published_at="2026-09-15T21:00:00+09:00")]
        reps, mapping, _ = assign_document_ids(rows)
        self.assertEqual(reps[0]["record_id"], "a")
        self.assertTrue(all(entry["available_at"] == "2026-09-15T12:00:00Z" for entry in mapping))

    def test_prior_old_availability_not_applied_to_new_revision_body(self):
        first, prior, _ = assign_document_ids([self.row("a", "https://example.com/a", body="Old brief", published_at="2026-01-01T10:00:00Z")])
        later = self.row("b", "https://example.com/a", body=LONG_A)
        reps, mapping, _ = assign_document_ids([later], prior)
        self.assertEqual(reps[0]["document_id"], first[0]["document_id"])
        self.assertEqual(reps[0]["published_at"], later["published_at"])
        self.assertTrue(all(entry["available_at"] == later["published_at"] for entry in mapping))

    def test_existing_row_uuid_preserved_and_conflict_rejected(self):
        existing_id = str(uuid.uuid4())
        row = self.row("a", document_id=existing_id)
        self.assertEqual(assign_document_ids([row])[0][0]["document_id"], existing_id)
        prior = [{"identity_key": "record:prepared:a", "document_id": str(uuid.uuid4())}]
        with self.assertRaises(ValueError):
            assign_document_ids([row], prior)

    def test_input_and_prior_unchanged_and_prior_only_keys_retained(self):
        _, prior, _ = assign_document_ids([self.row("old", body="old")])
        rows = [self.row("new", body="new")]
        before = copy.deepcopy((rows, prior))
        _, updated, _ = assign_document_ids(rows, prior)
        self.assertEqual((rows, prior), before)
        old_key = "record:prepared:old"
        self.assertEqual(next(entry for entry in updated if entry["identity_key"] == old_key), prior[0])

    def test_empty_batch_preserves_map_and_declares_scope(self):
        _, prior, _ = assign_document_ids([self.row("a")])
        reps, mapping, summary = assign_document_ids([], prior)
        self.assertEqual(reps, [])
        self.assertEqual(mapping, prior)
        self.assertFalse(summary["global_dedup_certified"])
        self.assertTrue(summary["prior_map_required_for_subsequent_runs"])
        self.assertIn("90-day", summary["limitations"][0])

    def test_invalid_identity_timestamp_hash_and_prior_fail(self):
        for row in (self.row("a", source_dataset=""), self.row("a", source_dataset="x:y"),
                    self.row(""), self.row("a", published_at="2026-09-15T12:00:00"),
                    self.row("a", content_hash="0" * 64)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                assign_document_ids([row])
        with self.assertRaises(ValueError):
            assign_document_ids([], [{"identity_key": "content:short", "document_id": str(uuid.uuid4())}])
        with self.assertRaises(ValueError):
            assign_document_ids([], [{"identity_key": "record:prepared:a", "document_id": "not-a-uuid"}])


if __name__ == "__main__":
    unittest.main()
