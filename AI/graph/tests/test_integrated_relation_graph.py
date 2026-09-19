"""Independent source/time/direction/null invariants for the merged research KG."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
import shutil
import uuid
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrated_relation_graph import (IntegratedRelationExplainer, IntegratedRelationRetriever,
    available_time, finalize, init_db, insert_evidence, score_rows, validate_evidence)


@contextmanager
def workspace_temp():
    workspace = (Path(__file__).resolve().parents[1] / "artifacts").resolve()
    folder = workspace / ("_integrated_test_" + uuid.uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        assert folder.resolve().is_relative_to(workspace) and folder.name.startswith("_integrated_test_")
        shutil.rmtree(folder)


def fixture(kind="NEWS", rel="INVEST", date="2026-08-14", record="r1", quote=None):
    quote = quote or ("삼성SDI는 삼성E&A의 주식을 보유한다." if kind == "NEWS" else "삼성SDI\n최대주주\n11.69")
    doc = {"document_id": kind + ":" + record, "record_id": record, "source_kind": kind,
           "text": "제목\n" + quote, "published_at": date, "source_url": "https://example.invalid/" + record}
    doc["text_sha256"] = hashlib.sha256(doc["text"].encode()).hexdigest()
    if kind == "DISCLOSURE":
        doc.update(filing_ticker="028050", filing_corp_code="001")
    row = {"evidence_id": kind + ":" + record + ":1", "record_id": record, "document_id": doc["document_id"],
           "source_kind": kind, "src_ticker": "006400", "dst_ticker": "028050", "rel_type": rel,
           "directed": rel in {"INVEST", "SUPPLY"}, "relation_subtype": "shareholding",
           "published_at": date, "source_url": doc["source_url"], "quote": quote,
           "quote_start": 3, "quote_end": len(doc["text"]), "source_quote": "삼성SDI",
           "target_quote": "삼성E&A" if kind == "NEWS" else "", "support_weight": 1.0}
    return row, doc


class IntegratedTests(unittest.TestCase):
    def test_worldwide_end_and_no_timezone_guess(self):
        expected = "2026-08-15T12:00:00Z"
        self.assertEqual(available_time("2026-08-14")[0], expected)
        self.assertEqual(available_time("2026-08-14 08:45")[0], expected)
        self.assertEqual(available_time("2026-08-14T08:45:00+09:00")[0], "2026-08-13T23:45:00Z")
        for value in ("", "garbage", "1970-01-01", "2026-02-30"):
            with self.assertRaises(ValueError):
                available_time(value)

    def test_quote_and_filing_identity(self):
        row, doc = fixture("DISCLOSURE")
        self.assertEqual(validate_evidence(row, doc)["source_kind"], "DISCLOSURE")
        for invalid in ({**row, "dst_ticker": "OTHER"}, {**row, "quote_end": row["quote_end"] - 1},
                        {**row, "directed": False}, {**row, "rel_type": "CO_MENTION"}):
            with self.assertRaises(ValueError):
                validate_evidence(invalid, doc)

    def test_null_and_document_dedup_blending(self):
        one = validate_evidence(*fixture())
        second = {**one, "evidence_id": "other_sentence_same_document"}
        markets = {"006400": "KOSPI", "028050": "KOSPI"}
        rows, unknown = score_rows([one, second], markets, "2026-08-16T00:00:00Z")
        self.assertFalse(unknown)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["evidence_count"] == 1 and r["disclosure_score"] is None
                            and r["score"] == r["news_score"] for r in rows))
        filing = validate_evidence(*fixture("DISCLOSURE"))
        rows, _ = score_rows([one, filing], markets, "2026-08-16T00:00:00Z")
        self.assertTrue(all(r["evidence_count"] == 2 and r["news_score"] == r["disclosure_score"] for r in rows))
        self.assertEqual(score_rows([one], markets, one["available_at"])[0], [])

    def test_complete_artifact_retrieval_and_tamper(self):
        with workspace_temp() as folder:
            directory = Path(folder)
            con = init_db(directory / "integrated.sqlite3")
            insert_evidence(con, *fixture())
            insert_evidence(con, *fixture("DISCLOSURE"))
            insert_evidence(con, *fixture(rel="PARTNER", record="p"))
            finalize(con, directory, companies={"006400": "KOSPI", "028050": "KOSPI"},
                     as_of="2026-08-16T00:00:00Z", provenance={}, coverage={"partial": True})
            reader = IntegratedRelationRetriever(directory)
            result = reader.retrieve("006400", "028050", "INVEST")
            self.assertEqual(result["sources_returned"], ["DISCLOSURE", "NEWS"])
            self.assertEqual(reader.retrieve("028050", "006400", "INVEST")["status"], "NO_EVIDENCE")
            self.assertEqual(reader.retrieve("028050", "006400", "PARTNER")["status"], "FOUND")
            self.assertEqual(reader.retrieve("006400", "028050", "INVEST", as_of="2026-08-15T12:00:00Z")["status"], "NO_EVIDENCE")
            explained = IntegratedRelationExplainer(reader).explain("006400", "028050", "INVEST")
            self.assertEqual(explained["mode"], "extractive")
            self.assertTrue(explained["disclosures_searched"])
            (directory / "temporal_evidence.jsonl").write_text("corrupt", encoding="utf-8")
            with self.assertRaises(ValueError):
                IntegratedRelationRetriever(directory)

    def test_conflicting_source_id_is_not_silently_merged(self):
        with workspace_temp() as folder:
            con = init_db(Path(folder) / "state.sqlite3")
            try:
                row, doc = fixture()
                insert_evidence(con, row, doc)
                insert_evidence(con, row, doc)
                changed = {**doc, "source_url": "https://different.invalid/"}
                with self.assertRaises(ValueError):
                    insert_evidence(con, {**row, "source_url": changed["source_url"]}, changed)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
