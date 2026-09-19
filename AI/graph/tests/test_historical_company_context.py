"""Synthetic boundary tests; no news corpus, registry facts, network, or model."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import historical_company_context as h


def fact(**changes):
    record = {"record_id": "fiction-source-1", "ticker": "001234", "entity_id": "fiction-entity-1",
              "observed_at": "2020-12-31", "published_at": "2021-03-03",
              "source_url": "https://official.example/dated-source-1", "display_name": "당시 가상지주회사",
              "aliases": ["가상지주"], "business": "관측일에 지주사업을 보고했다.",
              "relationships": ["관측일에 가상은행을 자회사로 보고했다."],
              "limitations": ["가상은행 자체와 지주회사는 별도 법인이다."],
              "not_before": None, "not_after": None}
    record.update(changes)
    return record


def registry(*records):
    return {"schema": h.SCHEMA, "records": list(records) or [fact()]}


def article(**changes):
    row = {"article_id": "fiction-article-1", "ticker": "001234", "company_name": "현재 가상회사",
           "published_at": "2021-04-27", "title": "가상 제목", "body": "가상 원문 전체.\n둘째 문장.",
           "company_context": None, "case_kind": "synthetic", "source_split": "train",
           "source_url": "https://news.example/fiction", "arbitrary_original_metadata": {"keep": [1, "x"]}}
    row.update(changes)
    return row


class ResolutionTests(unittest.TestCase):
    def test_resolve_preserves_all_except_two_allowed_fields_and_inputs(self):
        row, data = article(), registry()
        original, original_registry = copy.deepcopy(row), copy.deepcopy(data)
        new, audit = h.resolve(row, data)
        self.assertEqual(row, original)
        self.assertEqual(data, original_registry)
        self.assertEqual(new["company_name"], "당시 가상지주회사")
        self.assertEqual({k: v for k, v in new.items() if k not in {"company_name", "company_context"}},
                         {k: v for k, v in row.items() if k not in {"company_name", "company_context"}})
        context = new["company_context"]
        self.assertEqual(set(context), {"source", "as_of", "business", "relationships"})
        self.assertEqual(context["as_of"], "2021-03-04T00:00:00Z")
        self.assertEqual(context["business"], fact()["business"])
        self.assertIn(fact()["relationships"][0], context["relationships"])
        self.assertIn(fact()["limitations"][0], context["relationships"])
        self.assertIn("2020-12-31", context["relationships"][0])
        self.assertIn("2021-03-03", context["relationships"][0])
        self.assertIn("유지되었다고 확인한 것은 아니다", context["relationships"][0])
        self.assertIn("동일 법인으로 확인된 사명만", context["relationships"][1])
        self.assertEqual(audit["reason"], "matched")
        self.assertFalse(audit["continuous_ownership_assumed"])
        new["arbitrary_original_metadata"]["keep"].append(2)
        self.assertEqual(row, original)

    def test_publication_date_requires_next_day_utc(self):
        for value, reason in [("2021-03-03", "no_source_available_by_article"),
                              ("2021-03-03T23:59:59Z", "no_source_available_by_article"),
                              ("2021-03-04T08:59:59+09:00", "no_source_available_by_article"),
                              ("2021-03-04", "matched"),
                              ("2021-03-04T09:00:00+09:00", "matched")]:
            with self.subTest(value=value):
                self.assertEqual(h.resolve(article(published_at=value), registry())[1]["reason"], reason)

    def test_snapshot_day_400_allowed_and_401_expired(self):
        data = registry(fact(observed_at="2020-01-01", published_at="2020-01-02"))
        new, audit = h.resolve(article(published_at="2021-02-04"), data)
        self.assertEqual(audit["snapshot_age_days"], 400)
        self.assertTrue(audit["context_added"])
        row = article(published_at="2021-02-05")
        new, audit = h.resolve(row, data)
        self.assertEqual(audit["snapshot_age_days"], 401)
        self.assertEqual(audit["reason"], "snapshot_expired")
        self.assertEqual(new, row)

    def test_activity_boundaries_are_separate_and_end_exclusive(self):
        data = registry(fact(not_before="2021-04-27", not_after="2021-04-29"))
        for value, reason in [("2021-04-26", "before_confirmed_activity_boundary"),
                              ("2021-04-27", "matched"), ("2021-04-28T23:59:59Z", "matched"),
                              ("2021-04-29", "at_or_after_confirmed_activity_end")]:
            with self.subTest(value=value):
                self.assertEqual(h.resolve(article(published_at=value), data)[1]["reason"], reason)

    def test_latest_published_failure_never_falls_back(self):
        older = fact(record_id="older", observed_at="2021-01-01", published_at="2021-02-01")
        cases = [
            (fact(record_id="newer", observed_at="2019-01-01", published_at="2021-04-01"), "snapshot_expired"),
            (fact(record_id="newer", published_at="2021-04-01", not_after="2021-04-27"), "at_or_after_confirmed_activity_end"),
            (fact(record_id="newer", published_at="2021-04-01", not_before="2021-05-01"), "before_confirmed_activity_boundary"),
        ]
        for newest, reason in cases:
            with self.subTest(reason=reason):
                row = article()
                result, audit = h.resolve(row, registry(older, newest))
                self.assertEqual(result, row)
                self.assertEqual(audit["reason"], reason)
                self.assertEqual(audit["record_ids"], ["newer"])

    def test_unpublished_future_record_does_not_hide_available_past_record(self):
        future = fact(record_id="future", entity_id="another-entity", observed_at="2021-05-01", published_at="2021-05-01")
        _, audit = h.resolve(article(), registry(fact(), future))
        self.assertEqual(audit["reason"], "matched")
        self.assertEqual(audit["record_ids"], ["fiction-source-1"])
        self.assertEqual(audit["unavailable_record_ids"], ["future"])

    def test_ambiguous_entity_and_conflicting_latest_facts_refuse(self):
        for changes, reason in [({"entity_id": "different-entity"}, "ambiguous_latest_entity"),
                                ({"display_name": "다른 법인명 주장"}, "conflicting_latest_records"),
                                ({"not_after": "2021-04-27"}, "conflicting_latest_records")]:
            with self.subTest(changes=changes):
                result, audit = h.resolve(article(), registry(fact(), fact(record_id="second", **changes)))
                self.assertEqual(result, article())
                self.assertEqual(audit["reason"], reason)

    def test_same_entity_latest_observation_and_equivalent_sources(self):
        older = fact(record_id="old-observation", observed_at="2020-09-30", display_name="가상 이전 표기")
        _, audit = h.resolve(article(), registry(older, fact()))
        self.assertEqual(audit["record_ids"], ["fiction-source-1"])
        additional = fact(record_id="second", source_url="https://official.example/second-source")
        new, audit = h.resolve(article(), registry(fact(), additional))
        self.assertEqual(audit["record_ids"], ["fiction-source-1", "second"])
        self.assertEqual(new["company_context"]["source"], fact()["source_url"] + "\n" + additional["source_url"])

    def test_no_hit_is_exact_copy_and_existing_context_is_rejected(self):
        row = article(ticker="999999")
        new, audit = h.resolve(row, registry())
        self.assertEqual(new, row)
        self.assertIsNot(new, row)
        self.assertEqual(audit["reason"], "ticker_not_in_registry")
        for value in ({}, {"source": "already set"}, "", 0, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                h.resolve(article(company_context=value), registry())
        with self.assertRaisesRegex(ValueError, "timezone"):
            h.resolve(article(published_at="2021-04-27T12:00:00"), registry())

    def test_whole_registry_validation_including_unrelated_bad_record(self):
        bad_values = [("aliases", "alias"), ("aliases", [5]), ("aliases", [""]),
                      ("aliases", ["x", "x"]), ("relationships", [None]),
                      ("limitations", "text"), ("business", {}), ("ticker", 1234),
                      ("entity_id", ""), ("record_id", ""),
                      ("source_url", "http://official.example/doc"),
                      ("source_url", "https://user:password@official.example/doc"),
                      ("source_url", "https://official.example/with space"),
                      ("published_at", "2020-01-01"), ("observed_at", "2020-02-30"),
                      ("published_at", "2021-03-03T12:00:00Z"), ("not_before", "2021-2-3")]
        for key, value in bad_values:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                bad = fact(record_id="unrelated", ticker="999999")
                bad[key] = value
                h.resolve(article(), registry(fact(), bad))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            h.validate_registry(registry(fact(), fact()))
        with self.assertRaisesRegex(ValueError, "nonempty interval"):
            h.validate_registry(registry(fact(not_before="2021-01-01", not_after="2021-01-01")))
        extra = fact(); extra["expected_selection"] = "채택"
        with self.assertRaisesRegex(ValueError, "record fields"):
            h.validate_registry(registry(extra))


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".historical-context-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.inputs, self.registry = self.root / "input.jsonl", self.root / "registry.json"
        self.companies, self.out = self.root / "companies.csv", self.root / "output"
        self.rows = [article(), article(article_id="no-hit", ticker="999999")]
        self.inputs.write_text("".join(h.canonical(r) + "\n" for r in self.rows), encoding="utf8")
        self.registry.write_text(h.canonical(registry()), encoding="utf8")
        self.companies.write_text("ticker,name_official\n001234,미래 새 이름\n999999,미지원 회사\n", encoding="utf8")

    def tearDown(self):
        if self.root.resolve().parent != self.parent:
            raise RuntimeError("Cleanup escaped test workspace")
        shutil.rmtree(self.root)

    def run_apply(self, **overrides):
        args = dict(inputs=self.inputs, registry=self.registry, out=self.out, companies=self.companies)
        args.update(overrides)
        return h.apply(**args)

    def test_cli_publishes_hashes_coverage_and_no_current_name_leakage(self):
        before = {p: p.read_bytes() for p in (self.inputs, self.registry, self.companies)}
        completed = subprocess.run([sys.executable, "-B", str(Path(h.__file__)), "--inputs", str(self.inputs),
                                    "--registry", str(self.registry), "--out", str(self.out),
                                    "--companies", str(self.companies)], capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        manifest = h.read_json((self.out / "manifest.json").read_bytes())
        rows = [json.loads(line) for line in (self.out / "context_inputs.jsonl").read_text(encoding="utf8").splitlines()]
        self.assertEqual(manifest["records"], 2)
        self.assertEqual(manifest["context_added"], 1)
        self.assertEqual(manifest["reason_counts"], {"matched": 1, "ticker_not_in_registry": 1})
        for old, new, audit in zip(self.rows, rows, manifest["rows"]):
            self.assertEqual(audit["original_row_sha256"], h.row_sha256(old))
            self.assertEqual(audit["context_row_sha256"], h.row_sha256(new))
        self.assertEqual(rows[1], self.rows[1])
        self.assertNotIn("미래 새 이름", json.dumps(rows, ensure_ascii=False))
        for name, expected in manifest["files"].items():
            self.assertEqual(h.sha256((self.out / name).read_bytes()), expected["sha256"])
        coverage = h.read_json((self.out / "company_coverage.json").read_bytes())
        self.assertEqual((coverage["company_count"], coverage["with_records"]), (2, 1))
        self.assertFalse(coverage["current_csv_name_is_historical_evidence"])
        self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.run_apply()

    def test_duplicate_json_keys_and_duplicate_input_pairs_rejected_before_publication(self):
        self.registry.write_text('{"schema":"bad","schema":"cosmos-historical-company-context-1","records":[]}', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            self.run_apply()
        self.registry.write_text(h.canonical(registry()), encoding="utf8")
        self.inputs.write_text((h.canonical(article()) + "\n") * 2, encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Duplicate article/ticker"):
            self.run_apply()
        self.assertFalse(self.out.exists())

    def test_input_change_during_publication_never_gets_complete_manifest(self):
        real_write = h._write_new
        def change_after_write(path, raw):
            real_write(path, raw)
            if Path(path).name == "context_inputs.jsonl":
                self.inputs.write_text(h.canonical(article(body="changed")) + "\n", encoding="utf8")
        with mock.patch.object(h, "_write_new", side_effect=change_after_write):
            with self.assertRaisesRegex(ValueError, "Input changed before publication"):
                self.run_apply()
        self.assertTrue((self.out / "context_inputs.jsonl").exists())
        self.assertFalse((self.out / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
