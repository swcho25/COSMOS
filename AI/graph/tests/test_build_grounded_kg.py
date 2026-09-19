"""Synthetic CPU transaction/coverage tests; no live corpus or model execution."""
from __future__ import annotations

from contextlib import closing, contextmanager
from argparse import Namespace
import copy
import errno
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
import build_grounded_kg as build
import kg_grounded_relations
import kg_target_sentiment


def row(article="a1", ticker="SELL", name="삼성전자", **updates):
    value = dict(article_id=article, ticker=ticker, company_name=name, published_at="2026-01-02",
                 title="기업 계약", body="삼성전자는 애플에 반도체를 공급했다.", source_url="https://example.invalid/" + article)
    value.update(updates)
    return value


def grounded(value, decision="candidate_ready"):
    return dict(article_id=value["article_id"], ticker=value["ticker"],
                source_input_sha256=build.preview.canonical_input_sha256(value),
                materialization_decision=decision, cleaned_title=value["title"], cleaned_body=value["body"],
                cleaned_text_sha256=build.digest([value["title"], value["body"]]),
                date_audit={"synthetic": True}, cleaning_audit=[],
                evidence=[dict(source="body", start=0, end=len(value["body"]), quote=value["body"])],
                target_context={"matched_names": [value["company_name"]]})


class GroundedBuilderTests(unittest.TestCase):
    def setUp(self):
        self.workspace = (HERE / "artifacts").resolve()
        self.root = self.workspace / ("_build_grounded_cpu_test_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()
        self.db = sqlite3.connect(self.out / "state.sqlite3")
        build.schema(self.db)
        self.source = sqlite3.connect(":memory:")
        self.source.execute("CREATE TABLE pairs (decision TEXT,input_sha256 TEXT,result_sha256 TEXT,input_json TEXT)")
        self.print_patch = patch("builtins.print")
        self.print_patch.start()

    def tearDown(self):
        self.print_patch.stop()
        self.source.close()
        self.db.close()
        resolved = self.root.resolve()
        assert resolved.is_relative_to(self.workspace) and resolved.name.startswith("_build_grounded_cpu_test_")
        shutil.rmtree(resolved)

    def insert_source(self, values, decision="include"):
        for value in values:
            self.source.execute("INSERT INTO pairs VALUES (?,?,?,?)", (
                decision, build.preview.canonical_input_sha256(value), "f" * 64, build.canonical(value)))
        self.source.commit()

    def prepare(self, count, producer=grounded, batch_size=2):
        with patch.object(build, "ground", side_effect=producer):
            return build.prepare(self.db, self.source, self.out, {"expected_pairs": count}, 1, batch_size)

    def checkpoint(self):
        return {"pairs": self.db.execute("SELECT article_id,ticker,decision,payload FROM pairs ORDER BY article_id,ticker").fetchall(),
                "articles": self.db.execute("SELECT article_id,source_sha,cleaned_sha,payload FROM articles ORDER BY article_id").fetchall(),
                "prepared": build.meta(self.db, "prepared_pairs"), "cursor": build.meta(self.db, "source_cursor"),
                "counts": build.meta(self.db, "decision_counts")}

    def export_protocol(self, count):
        source_dir = self.root / "snapshot"
        source_dir.mkdir(exist_ok=True)
        source_db = source_dir / "state.sqlite3"
        source_db.write_bytes(b"synthetic immutable snapshot")
        protocol = dict(expected_pairs=count, settings={"limit_pairs": count}, code_and_assets={},
                        source={"directory": str(source_dir), "db_sha256": build.sha(source_db), "parent_unprocessed_pairs": 1000})
        build.atomic(self.out / "protocol.json", protocol)
        return protocol

    def insert_sentiments(self):
        build.relations(self.db, self.out, 2)
        counts = 0
        with self.db:
            for article, ticker in self.db.execute("SELECT article_id,ticker FROM pairs WHERE decision='candidate_ready'").fetchall():
                self.db.execute("INSERT INTO sentiments VALUES (?,?,?)", (article, ticker, build.canonical(
                    {"article_id": article, "ticker": ticker, "status": "analyzed", "label": "NEUTRAL", "human_gold": False})))
                counts += 1
            build.put(self.db, "sentiment_pairs", counts)
            build.put(self.db, "sentiment_counts", {"analyzed": counts} if counts else {})
            cursor = self.db.execute("SELECT COALESCE(MAX(rowid),0) FROM pairs WHERE decision='candidate_ready'").fetchone()[0]
            build.put(self.db, "sentiment_cursor", cursor)
            build.put(self.db, "stage", "export")

    def test_prepare_deduplicates_articles_not_company_pairs(self):
        self.insert_source([row(), row(ticker="BUY", name="애플")])
        self.assertTrue(self.prepare(2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0], 2)
        self.assertEqual(build.meta(self.db, "decision_counts"), {"candidate_ready": 2})
        self.assertEqual(build.meta(self.db, "stage"), "relations")

    def test_stop_commits_current_chunk_then_resumes_without_duplicates(self):
        values = [row(), row("a2")]
        self.insert_source(values)
        attempted = []
        def stop_after_first(value):
            attempted.append(value["article_id"])
            (self.out / "STOP").touch()
            return grounded(value)
        self.assertFalse(self.prepare(2, stop_after_first, batch_size=1))
        self.assertEqual(attempted, ["a1"])
        self.assertEqual(build.meta(self.db, "prepared_pairs"), 1)
        self.assertEqual(build.meta(self.db, "source_cursor"), 1)
        (self.out / "STOP").unlink()
        resumed = []
        def record(value):
            resumed.append(value["article_id"])
            return grounded(value)
        self.assertTrue(self.prepare(2, record, batch_size=1))
        self.assertEqual(resumed, ["a2"])
        self.assertEqual(build.meta(self.db, "prepared_pairs"), 2)
        actual = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM pairs ORDER BY article_id")]
        self.assertEqual([p["source_input_sha256"] for p in actual], [build.preview.canonical_input_sha256(v) for v in values])

    def test_stopped_before_prepare_does_not_invoke_processor(self):
        self.insert_source([row()])
        (self.out / "STOP").touch()
        with patch.object(build, "ground") as processor:
            self.assertFalse(build.prepare(self.db, self.source, self.out, {"expected_pairs": 1}, 1, 2))
        processor.assert_not_called()
        self.assertEqual(build.meta(self.db, "prepared_pairs"), 0)

    def test_invalid_second_grounding_rolls_back_entire_batch_and_cursor(self):
        self.insert_source([row(), row("a2")])
        before = self.checkpoint()
        def corrupt(value):
            result = grounded(value)
            if value["article_id"] == "a2":
                result["evidence"][0]["quote"] = "fabricated evidence"
            return result
        with self.assertRaises(ValueError):
            self.prepare(2, corrupt)
        self.assertEqual(self.checkpoint(), before)
        self.assertTrue(self.prepare(2))

    def test_interruption_before_transaction_replays_pending_batch(self):
        self.insert_source([row(), row("a2")])
        seen = []
        def interrupted(value):
            seen.append(value["article_id"])
            if value["article_id"] == "a2":
                raise KeyboardInterrupt("synthetic stop before commit")
            return grounded(value)
        with self.assertRaises(KeyboardInterrupt):
            self.prepare(2, interrupted)
        self.assertEqual(build.meta(self.db, "prepared_pairs"), 0)
        replayed = []
        def replay(value):
            replayed.append(value["article_id"])
            return grounded(value)
        self.assertTrue(self.prepare(2, replay))
        self.assertEqual(replayed, seen)

    def test_conflicting_original_article_and_cleaning_rejected(self):
        for change in ("original", "cleaning"):
            with self.subTest(change=change):
                self.source.execute("DELETE FROM pairs")
                values = [row(), row(ticker="BUY", name="애플", **({"title": "different article"} if change == "original" else {}))]
                self.insert_source(values)
                def producer(value):
                    result = grounded(value)
                    if change == "cleaning" and value["ticker"] == "BUY":
                        result["cleaned_text_sha256"] = "different"
                    return result
                with self.assertRaisesRegex(ValueError, "Conflicting"):
                    self.prepare(2, producer)
                self.assertEqual(build.meta(self.db, "prepared_pairs"), 0)
                self.assertEqual(self.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 0)

    def test_input_hash_and_declared_coverage_are_enforced(self):
        self.insert_source([row()])
        self.source.execute("UPDATE pairs SET input_sha256='wrong'")
        self.source.commit()
        with self.assertRaisesRegex(ValueError, "Source input hash"):
            self.prepare(1)
        self.source.execute("UPDATE pairs SET input_sha256=?", (build.preview.canonical_input_sha256(row()),))
        self.source.commit()
        with self.assertRaisesRegex(ValueError, "Source exhausted"):
            self.prepare(2)
        self.assertEqual(build.meta(self.db, "prepared_pairs"), 1)

    def test_grounding_identity_missing_evidence_and_span_forgery_rejected(self):
        value = row()
        mutations = [lambda r: r.update(ticker="OTHER"), lambda r: r.update(source_input_sha256="wrong"),
                     lambda r: r.update(evidence=[]), lambda r: r["evidence"][0].update(start=-1),
                     lambda r: r["evidence"][0].update(start=True)]
        for mutate in mutations:
            result = grounded(value)
            mutate(result)
            with self.subTest(result=result), self.assertRaises(ValueError):
                build.validate_grounding(value, result)

    def test_relations_only_use_ready_endpoints_and_original_spans(self):
        self.insert_source([row(), row(ticker="BUY", name="애플")])
        self.prepare(2)
        self.assertTrue(build.relations(self.db, self.out, 2))
        stored = self.db.execute("SELECT payload FROM relations").fetchall()
        self.assertEqual(len(stored), 1)
        edge = json.loads(stored[0][0])
        self.assertEqual((edge["src_ticker"], edge["dst_ticker"]), ("SELL", "BUY"))
        self.assertEqual(edge["source_url"], row()["source_url"])
        self.assertEqual(edge["offset_basis"], "original_title_newline_body")
        self.assertFalse(edge["production_approved"])

    def test_excluded_endpoint_never_generates_company_relation(self):
        self.insert_source([row(), row(ticker="BUY", name="애플")])
        self.prepare(2, lambda value: grounded(value, "exclude" if value["ticker"] == "BUY" else "candidate_ready"))
        build.relations(self.db, self.out, 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)

    def test_removed_sentence_becomes_review_not_relationship(self):
        self.insert_source([row(), row(ticker="BUY", name="애플")])
        def clean(value):
            result = grounded(value)
            result["cleaned_body"] = "removed widget"
            result["cleaned_text_sha256"] = build.digest("removed widget")
            return result
        self.prepare(2, clean)
        build.relations(self.db, self.out, 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)
        reviews = json.loads(self.db.execute("SELECT payload FROM relation_reviews").fetchone()[0])
        self.assertEqual(reviews[0]["reason"], "relation_sentence_not_retained_in_cleaned_article")

    def test_relation_endpoint_forgery_rolls_back_cursor(self):
        self.insert_source([row(), row(ticker="BUY", name="애플")])
        self.prepare(2)
        valid = kg_grounded_relations.extract_grounded_relations(row()["title"], row()["body"], [
            {"ticker": "SELL", "company_name": "삼성전자"}, {"ticker": "BUY", "company_name": "애플"}])
        valid["relationships"][0]["dst_ticker"] = "UNAPPROVED"
        with patch.object(kg_grounded_relations, "extract_grounded_relations", return_value=valid):
            with self.assertRaisesRegex(ValueError, "ungrounded endpoint"):
                build.relations(self.db, self.out, 2)
        self.assertEqual(build.meta(self.db, "relation_cursor"), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)

    def test_export_requires_sentiment_for_every_ready_pair(self):
        self.insert_source([row()])
        self.prepare(1)
        protocol = self.export_protocol(1)
        with self.assertRaisesRegex(ValueError, "no sentiment"):
            build.export(self.db, self.out, protocol)
        self.assertFalse((self.out / "manifest.json").exists())
        self.assertNotEqual(build.meta(self.db, "stage"), "complete")

    def test_export_complete_scope_hashes_and_provisional_flags(self):
        self.insert_source([row(), row("a2")])
        self.prepare(2, lambda value: grounded(value, "review" if value["article_id"] == "a2" else "candidate_ready"))
        self.insert_sentiments()
        protocol = self.export_protocol(2)
        result = build.export(self.db, self.out, protocol)
        self.assertTrue(result["artifact_complete"])
        self.assertTrue(result["bounded_pilot"])
        self.assertFalse(result["full_population_complete"])
        self.assertEqual(result["input_pairs"], 2)
        self.assertEqual(result["parent_unprocessed_pairs"], 1000)
        for key, value in build.FLAGS.items():
            self.assertEqual(result[key], value)
        expected = {"selection_audit": 2, "news_nodes": 1, "news_company_edges": 1,
                    "company_nodes": 1, "relationship_evidence": 0, "company_relationships": 0, "relation_review": 0}
        for kind, metadata in result["files"].items():
            path = self.out / metadata["path"]
            self.assertEqual(metadata["rows"], expected[kind])
            self.assertEqual(path.stat().st_size, metadata["bytes"])
            self.assertEqual(build.sha(path), metadata["sha256"])
            with path.open(encoding="utf8") as stream:
                rows = [json.loads(line) for line in stream]
            self.assertEqual(len(rows), metadata["rows"])
            for output in rows:
                self.assertFalse(output["production_approved"])
                self.assertFalse(output["human_gold"])
        self.assertEqual(result["state_sqlite_sha256"], build.sha(self.out / "state.sqlite3"))

    def test_export_denominator_mismatch_never_publishes_manifest(self):
        self.insert_source([row()])
        self.prepare(1)
        self.insert_sentiments()
        protocol = self.export_protocol(2)
        with self.assertRaises(ValueError):
            build.export(self.db, self.out, protocol)
        self.assertFalse((self.out / "manifest.json").exists())

    def test_source_snapshot_contract_rejects_metadata_and_database_tampering(self):
        directory = self.root / "source_contract"
        directory.mkdir()
        (directory / "state.sqlite3").write_bytes(b"fixture")
        for filename in ("coverage.json", "protocol.json"):
            build.atomic(directory / filename, {"fixture": filename})
        decisions = {"include": 2, "exclude": 3, "needs_review": 1}
        receipt = {"artifact_complete": True, "decisions": decisions,
                   "state_sqlite_sha256": build.sha(directory / "state.sqlite3"),
                   "coverage_sha256": build.sha(directory / "coverage.json"),
                   "protocol_sha256": build.sha(directory / "protocol.json"),
                   "covered_unique_pairs": 6, "unprocessed_unique_pairs": 4, "full_population_complete": False}
        build.atomic(directory / "partial_receipt.json", receipt)
        build.atomic(directory / "status.json", {"stage": "partial_artifact_complete", "artifact_complete": True, "decisions": decisions})
        self.assertEqual(build.source_contract(directory)["include_pairs"], 2)
        build.atomic(directory / "coverage.json", {"tampered": True})
        with self.assertRaisesRegex(ValueError, "metadata checksum"):
            build.source_contract(directory)
        receipt["coverage_sha256"] = build.sha(directory / "coverage.json")
        build.atomic(directory / "partial_receipt.json", receipt)
        (directory / "state.sqlite3").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "database checksum"):
            build.source_contract(directory)

    @contextmanager
    def fake_run(self):
        """Exercise actual run/resume orchestration with tiny disk SQLite and fake CPU predictor."""
        directory = self.root / ("run_fixture_" + uuid.uuid4().hex)
        directory.mkdir()
        source_dir = directory / "source"
        source_dir.mkdir()
        source_db = source_dir / "state.sqlite3"
        connection = sqlite3.connect(source_db)
        connection.execute("CREATE TABLE pairs (decision TEXT,input_sha256 TEXT,result_sha256 TEXT,input_json TEXT)")
        for value in [row(), row("a2")]:
            connection.execute("INSERT INTO pairs VALUES (?,?,?,?)", (
                "include", build.preview.canonical_input_sha256(value), "f" * 64, build.canonical(value)))
        connection.commit()
        connection.close()
        source = dict(directory=str(source_dir), db_sha256=build.sha(source_db), include_pairs=2,
                      parent_covered_pairs=2, parent_unprocessed_pairs=8, full_population_complete=False)
        args = Namespace(out=directory / "output", merged_dir=source_dir, bundle=directory / "bundle",
                         workers=1, batch_size=1, model_batch_size=1, device="cpu", limit_pairs=2, resume=False)
        state = {"predictions": 0, "closes": 0, "after_predict": None, "alter": None}
        class Predictor:
            provenance = {"synthetic_cpu": True, "checkpoint": "fixture-1"}
            def __init__(self, *a, **k):
                pass
            def predict(self, inputs):
                state["predictions"] += 1
                outputs = [{"article_id": x["article_id"], "ticker": x["ticker"],
                            "status": "analyzed", "label": "NEUTRAL", "human_gold": False} for x in inputs]
                if state["alter"]:
                    outputs = state["alter"](outputs)
                if state["after_predict"]:
                    state["after_predict"]()
                return outputs
            def close(self):
                state["closes"] += 1
        with patch.object(build, "source_contract", return_value=source), \
             patch.object(build, "code_and_assets", return_value={}), \
             patch.object(build, "ground", side_effect=grounded), \
             patch.object(build, "AliasCatalog") as catalog, \
             patch.object(kg_target_sentiment, "TargetSentimentPredictor", Predictor):
            catalog.return_value.others.return_value = []
            yield args, state

    def test_full_orchestrator_resume_does_not_reinfer_completed_output(self):
        with self.fake_run() as (args, state):
            first = build.run(args)
            self.assertEqual(state["predictions"], 2)
            self.assertEqual(state["closes"], 1)
            args.resume = True
            second = build.run(args)
            self.assertEqual(first, second)
            self.assertEqual(state["predictions"], 2)

    def test_sentiment_stop_resume_uses_atomic_committed_cursor(self):
        with self.fake_run() as (args, state):
            state["after_predict"] = lambda: (args.out / "STOP").touch()
            self.assertIsNone(build.run(args))
            self.assertEqual(state["predictions"], 1)
            self.assertEqual(state["closes"], 1)
            with closing(sqlite3.connect(args.out / "state.sqlite3")) as db:
                self.assertEqual(build.meta(db, "sentiment_pairs"), 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sentiments").fetchone()[0], 1)
            state["after_predict"] = None
            (args.out / "STOP").unlink()
            args.resume = True
            result = build.run(args)
            self.assertTrue(result["artifact_complete"])
            self.assertEqual(state["predictions"], 2)
            self.assertEqual(state["closes"], 2)

    def test_bad_sentiment_identity_rolls_back_cursor_and_closes_predictor(self):
        with self.fake_run() as (args, state):
            def alter(outputs):
                outputs[0]["ticker"] = "wrong"
                return outputs
            state["alter"] = alter
            with self.assertRaisesRegex(ValueError, "Sentiment identity"):
                build.run(args)
            self.assertEqual(state["closes"], 1)
            with closing(sqlite3.connect(args.out / "state.sqlite3")) as db:
                self.assertEqual(build.meta(db, "sentiment_cursor"), 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sentiments").fetchone()[0], 0)
            state["alter"] = None
            args.resume = True
            self.assertTrue(build.run(args)["artifact_complete"])

    def test_missing_final_manifest_after_commit_recovers_without_reinference(self):
        with self.fake_run() as (args, state):
            original = build.atomic
            def interrupted(path, value):
                if Path(path).name == "manifest.json":
                    raise OSError("synthetic crash after complete DB commit")
                return original(path, value)
            with patch.object(build, "atomic", side_effect=interrupted), self.assertRaises(OSError):
                build.run(args)
            self.assertFalse((args.out / "manifest.json").exists())
            before = state["predictions"]
            args.resume = True
            self.assertTrue(build.run(args)["artifact_complete"])
            self.assertEqual(state["predictions"], before)

    def test_resume_rejects_changed_settings_or_protocol(self):
        with self.fake_run() as (args, state):
            build.run(args)
            args.resume = True
            args.batch_size = 2
            with self.assertRaisesRegex(ValueError, "frozen run"):
                build.run(args)
            self.assertEqual(state["predictions"], 2)

    def test_complete_resume_rejects_export_and_database_tampering(self):
        for kind in ("export", "database"):
            with self.subTest(kind=kind), self.fake_run() as (args, state):
                result = build.run(args)
                args.resume = True
                if kind == "export":
                    path = args.out / result["files"]["news_company_edges"]["path"]
                    with path.open("a", encoding="utf8") as stream:
                        stream.write("{}\n")
                else:
                    with closing(sqlite3.connect(args.out / "state.sqlite3")) as db:
                        db.execute("UPDATE sentiments SET payload='{}'")
                        db.commit()
                with self.assertRaises(ValueError):
                    build.run(args)

    def test_complete_resume_rejects_manifest_scope_flag_tampering(self):
        with self.fake_run() as (args, state):
            result = build.run(args)
            result["production_approved"] = True
            build.atomic(args.out / "manifest.json", result)
            args.resume = True
            with self.assertRaises(ValueError):
                build.run(args)

    def test_complete_resume_rejects_missing_export_inventory(self):
        with self.fake_run() as (args, state):
            result = build.run(args)
            result["files"] = {}
            build.atomic(args.out / "manifest.json", result)
            args.resume = True
            with self.assertRaises(ValueError):
                build.run(args)

    def test_checkpoint_missing_payload_identity_cannot_hide_behind_sql_null(self):
        self.insert_source([row()])
        self.prepare(1)
        with self.db:
            self.db.execute("UPDATE pairs SET payload='{}'")
        with self.assertRaises(ValueError):
            build.validate_checkpoint(self.db, 1)

    def test_atomic_retries_transient_permission_error_and_publishes_complete_json(self):
        target = self.out / "atomic_status.json"
        previous = b'{"stage":"previous"}\n'
        expected = {"stage": "next", "unicode": "완료", "count": 8}
        replace = build.os.replace
        for winerror in (None, 5, 32, 33):
            with self.subTest(winerror=winerror):
                target.write_bytes(previous)
                error = PermissionError(errno.EACCES, "synthetic temporary file lock")
                if winerror is not None:
                    error.winerror = winerror
                clock = [100.0]
                attempts, delays = [], []
                def sleep(delay):
                    delays.append(delay)
                    clock[0] += delay
                def locked_then_replace(source, destination):
                    # Failed rename attempts must leave both the previous
                    # publication and the complete pending JSON intact.
                    self.assertEqual(Path(destination), target)
                    self.assertEqual(target.read_bytes(), previous)
                    self.assertEqual(json.loads(Path(source).read_text(encoding="utf8")), expected)
                    attempts.append(Path(source))
                    if len(attempts) <= 8:
                        raise error
                    return replace(source, destination)
                with patch.object(build.os, "replace", side_effect=locked_then_replace) as mocked, \
                     patch.object(build.time, "monotonic", side_effect=lambda: clock[0]), \
                     patch.object(build.time, "sleep", side_effect=sleep):
                    build.atomic(target, expected)
                self.assertEqual(mocked.call_count, 9)
                self.assertEqual(len(set(attempts)), 1)
                self.assertEqual(delays[:5], [0.02, 0.04, 0.08, 0.16, 0.2])
                self.assertTrue(all(0 < delay <= 0.2 for delay in delays))
                self.assertEqual(json.loads(target.read_text(encoding="utf8")), expected)
                self.assertFalse(attempts[0].exists())

    def test_atomic_persistent_permission_error_expires_and_preserves_original(self):
        target = self.out / "atomic_status.json"
        previous = b'{"stage":"previous","cursor":42}\n'
        target.write_bytes(previous)
        failure = PermissionError(errno.EACCES, "synthetic persistent file lock")
        failure.winerror = 5
        clock, delays = [50.0], []
        def sleep(delay):
            delays.append(delay)
            clock[0] += delay
        with patch.object(build.os, "replace", side_effect=failure) as replace, \
             patch.object(build.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(build.time, "sleep", side_effect=sleep):
            with self.assertRaises(PermissionError) as raised:
                build.atomic(target, {"stage": "never_published"})
        self.assertIs(raised.exception, failure)
        self.assertGreater(replace.call_count, 1)
        self.assertGreaterEqual(clock[0] - 50.0, 5.0)
        self.assertLessEqual(clock[0] - 50.0, 5.2 + 1e-9)
        self.assertTrue(all(0 < delay <= 0.2 for delay in delays))
        self.assertEqual(target.read_bytes(), previous)

    def test_atomic_other_os_errors_fail_immediately_and_preserve_original(self):
        target = self.out / "atomic_status.json"
        previous = b'{"stage":"previous"}\n'
        for error in (OSError(errno.EIO, "synthetic I/O failure"),
                      OSError(errno.ENOSPC, "synthetic full disk"),
                      FileNotFoundError(errno.ENOENT, "synthetic missing destination"),
                      PermissionError(errno.EPERM, "synthetic unrelated permission failure")):
            with self.subTest(error=error):
                target.write_bytes(previous)
                with patch.object(build.os, "replace", side_effect=error) as replace, \
                     patch.object(build.time, "monotonic", return_value=100.0), \
                     patch.object(build.time, "sleep") as sleep:
                    with self.assertRaises(OSError) as raised:
                        build.atomic(target, {"stage": "never_published"})
                self.assertIs(raised.exception, error)
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(target.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
