"""Disclosure IDs, publication time, literal table evidence and issuer mapping."""
import copy
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lake_disclosures import normalize_dart, normalize_sec


class LakeDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.as_of = "2026-09-15T00:00:00Z"
        self.companies = [
            {"ticker": "005930", "market": "KOSPI", "name_official": "삼성전자", "dart_corp_code": "126380.0"},
            {"ticker": "032830", "market": "KOSPI", "name_official": "삼성생명", "dart_corp_code": "126398.0"},
            {"ticker": "GOOG", "market": "NASDAQ", "name_official": "Alphabet C", "cik": "1652044.0"},
            {"ticker": "GOOGL", "market": "NASDAQ", "name_official": "Alphabet A", "cik": "1652044"},
            {"ticker": "AAPL", "market": "NASDAQ", "name_official": "Apple", "cik": "320193"}]
        self.aliases = [{"alias": "삼성생명보험", "ticker": "032830", "ambiguity": "safe"}]
        self.table = ("회사 개요\n최대주주 및 그 특수관계인의 주식소유 현황\n"
                      "(기준일 : 2026년 06월 30일)\n성명 | 관계 | 주식종류 | 기초주식수 | 기초지분율 | 기말주식수 | 기말지분율\n"
                      "삼성생명보험(주) | 최대주주 본인 | 보통주 | 1,000 | 8.5 | 1,200 | 8.7\n"
                      "계 | 보통주 | 1,000 | 8.5 | 1,200 | 8.7\n후속 항목")
        self.dart = {"corp_code": "00126380", "rcept_no": "20260814000123", "rcept_dt": "20260814",
                     "report_nm": "반기보고서 (2026.06)", "source_path": "/archive/00126380_detail.jsonl.gz",
                     "files": [{"filename": "20260814000123.xml", "text": self.table}]}
        self.sec = {"accessionNumber": "0001652044-26-000100", "cik": "1652044", "symbols": ["GOOG", "GOOGL"],
                    "acceptanceDateTime": "2026-09-14T21:30:00Z", "filingDate": "2026-09-14",
                    "reportDate": "2026-06-30", "form": "10-Q", "source_path": "/sec/filings.jsonl"}

    def dart_result(self, report=None, aliases=None):
        return normalize_dart(report or self.dart, self.companies,
                              self.aliases if aliases is None else aliases, as_of=self.as_of)

    def sec_result(self, report=None):
        return normalize_sec(report or self.sec, self.companies, as_of=self.as_of)

    def test_dart_literal_seven_cell_evidence_and_receipt_publication(self):
        result = self.dart_result()
        document = result["document"]
        self.assertEqual(document["published_at"], "2026-08-14T23:59:59.999999Z")
        self.assertEqual(document["ownership_parse_status"], "parsed")
        self.assertEqual(uuid.UUID(document["document_id"]).version, 5)
        relation, = result["relationships"]
        self.assertEqual((relation["src_ticker"], relation["dst_ticker"]), ("032830", "005930"))
        self.assertEqual(relation["share_pct"], 8.7)
        self.assertEqual(relation["as_of_date"], "2026-06-30")
        self.assertEqual(relation["record_id"], document["document_id"])
        raw = self.dart["files"][relation["evidence_file_index"]]["text"]
        self.assertEqual(relation["evidence_text"], raw[relation["evidence_start"]:relation["evidence_end"]])
        self.assertEqual(relation["evidence_text"].count("|"), 6)

    def test_newline_cells_preserve_actual_whitespace_and_offsets(self):
        report = copy.deepcopy(self.dart)
        report["files"][0]["text"] = self.table.replace(" | ", "\n\t")
        relation, = self.dart_result(report)["relationships"]
        self.assertIn("\n\t", relation["evidence_text"])
        raw = report["files"][0]["text"]
        self.assertEqual(relation["evidence_text"], raw[relation["evidence_start"]:relation["evidence_end"]])

    def test_missing_numeric_cell_is_not_reconstructed(self):
        report = copy.deepcopy(self.dart)
        report["files"][0]["text"] = self.table.replace("| 1,000 | 8.5 | 1,200 | 8.7", "| 1,000 | | 1,200 | 8.7", 1)
        result = self.dart_result(report)
        self.assertEqual(result["relationships"], [])
        self.assertEqual(result["document"]["ownership_parse_status"], "no_recognized_table")

    def test_unsupported_reports_no_table_and_no_universe_holder_are_distinct(self):
        report = copy.deepcopy(self.dart)
        report["report_nm"] = "대규모기업집단현황공시[분기별공시(개별회사용)]"
        self.assertEqual(self.dart_result(report)["document"]["ownership_parse_status"], "not_supported_report")
        report["report_nm"] = "사업보고서 (2025.12)"
        report["files"] = [{"text": "최대주주 이름이 본문에 언급되지만 표는 없음"}]
        self.assertEqual(self.dart_result(report)["document"]["ownership_parse_status"], "no_recognized_table")
        report["files"] = [{"text": self.table.replace("삼성생명보험(주)", "유니버스밖회사")}]
        self.assertEqual(self.dart_result(report)["document"]["ownership_parse_status"], "parsed_no_universe_holder")

    def test_corrected_quarterly_reports_supported_but_future_accounting_refused(self):
        report = copy.deepcopy(self.dart)
        report["report_nm"] = "[기재정정]분기보고서 (2026.03)"
        self.assertEqual(self.dart_result(report)["document"]["ownership_parse_status"], "parsed")
        report["files"][0]["text"] = self.table.replace("2026년 06월 30일", "2026년 12월 31일")
        result = self.dart_result(report)
        self.assertEqual(result["relationships"], [])
        self.assertEqual(result["document"]["ownership_parse_status"], "invalid_accounting_date")

    def test_receipt_identity_date_and_unknown_filer_validation(self):
        for update in ({"rcept_no": "20260814"}, {"rcept_dt": "20260813"}, {"rcept_dt": "20261314"},
                       {"corp_code": "99999999"}, {"rcept_dt": "20260915", "rcept_no": "20260915000123"}):
            self.assertIsNone(self.dart_result({**self.dart, **update}))
        first = self.dart_result()["document"]["document_id"]
        self.assertEqual(first, self.dart_result({**self.dart, "source_path": "/moved/path"})["document"]["document_id"])

    def test_safe_unique_aliases_common_stock_positive_percentage_and_no_self(self):
        for value in ("0", "101", "-1"):
            report = copy.deepcopy(self.dart)
            report["files"][0]["text"] = self.table.replace("1,200 | 8.7", f"1,200 | {value}")
            self.assertEqual(self.dart_result(report)["relationships"], [])
        report = copy.deepcopy(self.dart)
        report["files"][0]["text"] = self.table.replace("보통주", "우선주")
        self.assertEqual(self.dart_result(report)["relationships"], [])
        self.assertEqual(self.dart_result(aliases=[{**self.aliases[0], "ambiguity": "ambiguous"}])["relationships"], [])
        conflicting = self.aliases + [{"alias": "삼성생명보험", "ticker": "005930", "ambiguity": "safe"}]
        self.assertEqual(self.dart_result(aliases=conflicting)["relationships"], [])
        report["files"][0]["text"] = self.table.replace("삼성생명보험(주)", "삼성전자")
        self.assertEqual(self.dart_result(report)["relationships"], [])

    def test_duplicate_attachments_do_not_duplicate_relationships(self):
        report = copy.deepcopy(self.dart)
        report["files"].append(copy.deepcopy(report["files"][0]))
        self.assertEqual(len(self.dart_result(report)["relationships"]), 1)

    def test_sec_cik_and_symbol_intersection_preserves_multiple_share_classes(self):
        documents = self.sec_result()
        self.assertEqual([d["ticker"] for d in documents], ["GOOG", "GOOGL"])
        self.assertEqual(len({d["document_id"] for d in documents}), 1)
        self.assertEqual(documents[0]["published_at"], "2026-09-14T21:30:00Z")
        self.assertEqual(documents[0]["relationship_extraction_status"], "not_supported_sec_relationship_extraction")
        self.assertEqual(self.sec_result({**self.sec, "symbols": ["AAPL"]}), [])
        self.assertEqual(self.sec_result({**self.sec, "symbols": []}), [])
        one = self.sec_result({**self.sec, "symbols": ["GOOG", "AAPL"]})
        self.assertEqual([d["ticker"] for d in one], ["GOOG"])

    def test_sec_timezone_normalization_and_date_fallback(self):
        zoned = self.sec_result({**self.sec, "acceptanceDateTime": "2026-09-14T17:30:00-04:00"})
        self.assertEqual(zoned[0]["published_at"], "2026-09-14T21:30:00Z")
        for value in (None, "2026-09-14T17:30:00", "invalid"):
            document = self.sec_result({**self.sec, "acceptanceDateTime": value})[0]
            self.assertEqual(document["published_at"], "2026-09-14T23:59:59.999999Z")
            self.assertEqual(document["published_at_precision"], "date")
        self.assertEqual(self.sec_result({**self.sec, "acceptanceDateTime": None, "filingDate": None}), [])

    def test_sec_future_acceptance_cannot_fall_back_to_past_filing_date(self):
        report = {**self.sec, "acceptanceDateTime": self.as_of, "filingDate": "2026-09-01"}
        self.assertEqual(self.sec_result(report), [])
        self.assertEqual(self.sec_result({**report, "acceptanceDateTime": "2026-09-16T01:00:00Z"}), [])

    def test_sec_accession_identity_is_stable_for_dashless_form_and_rejects_malformed(self):
        original = self.sec_result()[0]["document_id"]
        dashless = self.sec_result({**self.sec, "accessionNumber": "000165204426000100", "source_path": "/elsewhere"})
        self.assertEqual(dashless[0]["document_id"], original)
        self.assertEqual(uuid.UUID(original).version, 5)
        self.assertEqual(self.sec_result({**self.sec, "accessionNumber": "0001"}), [])


if __name__ == "__main__":
    unittest.main()
