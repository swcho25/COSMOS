"""Human annotation provenance and unsafe article-text boundaries."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_relevance_human_review as review


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".human-review-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.row = {"article_id": "fiction1", "ticker": "FICTION", "company_name": "가상회사",
            "title": "신규 계약", "body": "가상회사가 새 계약을 발표했다. </script><img src=x onerror=alert(1)>",
            "published_at": "2021-05-01", "case_kind": "real", "source_split": "train", "company_context": None}
        self.inputs = self.root / "inputs.jsonl"
        self.inputs.write_text(json.dumps(self.row, ensure_ascii=False) + "\n", encoding="utf8")
        self.data = review.packet(self.inputs)
        self.annotation = {"article_id": "fiction1", "ticker": "FICTION", "input_sha256": self.data[0]["input_sha256"],
            "human_selection": "채택", "human_label": "직접관련", "human_time_status": "신규",
            "human_evidence_quotes": ["가상회사가 새 계약을 발표했다."], "human_reason": "신규 계약이 명시됨",
            "reviewed_by": "fictional-test-reviewer", "human_reviewed": True, "completed_at": "2026-09-17T00:00:00+09:00"}

    def tearDown(self):
        if self.root.resolve().parent != self.parent or not self.root.name.startswith(".human-review-test-"):
            raise RuntimeError("Cleanup escaped test workspace")
        shutil.rmtree(self.root)

    def test_form_is_empty_of_judgments_and_source_cannot_escape_script(self):
        out = self.root / "form.html"
        review.generate(self.inputs, out)
        html = out.read_text(encoding="utf8")
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn("<img src=x", html)
        self.assertIn("\\u003c/script>", html)
        self.assertIn("textContent=d.row.body", html)
        self.assertIn("human_reviewed:false", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("fetch(", html)
        self.assertIn("connect-src 'none'", html)
        with self.assertRaises(FileExistsError):
            review.generate(self.inputs, out)

    def test_valid_completed_and_absent_rows_are_never_automatic_gold(self):
        report = review.validate_annotations(self.data, [self.annotation])
        self.assertEqual(report["completed_human_attestations"], 1)
        self.assertFalse(report["judgment_correctness_verified"])
        self.assertEqual(review.validate_annotations(self.data, [])["pending"], 1)
        draft = {**self.annotation, "human_reviewed": False, "completed_at": None}
        self.assertEqual(review.validate_annotations(self.data, [draft])["completed_human_attestations"], 0)

    def test_tampered_identity_source_completion_or_quote_rejected(self):
        for changes in ({"ticker": "OTHER"}, {"input_sha256": "0" * 64}, {"human_evidence_quotes": ["없는 문장"]},
                        {"human_selection": "up"}, {"human_reviewed": 1}, {"human_evidence_quotes": []},
                        {"reviewed_by": ""}, {"human_reason": ""}, {"completed_at": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review.validate_annotations(self.data, [{**self.annotation, **changes}])
        with self.assertRaises(ValueError):
            review.validate_annotations(self.data, [self.annotation, self.annotation])
        changed = copy.deepcopy(self.data)
        changed[0]["input_sha256"] = "new-input"
        with self.assertRaises(ValueError):
            review.validate_annotations(changed, [self.annotation])

    def test_model_predictions_cannot_be_shown_as_blind_human_input(self):
        self.inputs.write_text(json.dumps({**self.row, "ai_reference": "adopt"}), encoding="utf8")
        with self.assertRaises(ValueError):
            review.packet(self.inputs)

    def translation(self, **changes):
        return {"article_id": self.row["article_id"], "ticker": self.row["ticker"],
                "input_sha256": self.data[0]["input_sha256"], "title_ko": "한국어 읽기 보조 제목",
                "body_ko": "이 문장은 원문에 없는 번역문입니다.", "status": "complete",
                "translator": "fictional-offline-translator", "source_language": "en", "target_language": "ko",
                "warnings": [], **changes}

    def translations(self, records):
        path = self.root / "translations.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf8")
        return path

    @staticmethod
    def embedded_data(html):
        return json.JSONDecoder().raw_decode(html.split("const DATA=", 1)[1])[0]

    def test_translation_is_sibling_only_and_original_export_remains_compatible(self):
        path = self.translations([self.translation(model_revision="fictional-revision", prediction="DO_NOT_EMBED")])
        before = {p: p.read_bytes() for p in (self.inputs, path)}
        data_before = copy.deepcopy(self.data)
        out = self.root / "bilingual.html"
        review.generate(self.inputs, out, translations=path)
        html = out.read_text(encoding="utf8")
        embedded = self.embedded_data(html)
        self.assertEqual(set(embedded[0]), {"row", "input_sha256", "translation"})
        self.assertEqual(embedded[0]["row"], self.data[0]["row"])
        self.assertEqual(embedded[0]["input_sha256"], self.data[0]["input_sha256"])
        self.assertNotIn("DO_NOT_EMBED", html)
        self.assertEqual(self.data, data_before)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(review.validate_annotations(self.data, [self.annotation]),
                         review.validate_annotations(embedded, [self.annotation]))
        self.assertEqual(set(self.annotation), review.ALLOWED)
        self.assertNotIn("translation", review.ALLOWED)
        self.assertEqual(review.packet(self.inputs), data_before)

    def test_translation_quotes_cannot_become_original_evidence(self):
        translation = self.translation()
        data = review.with_translations(self.data, self.translations([translation]))
        altered = {**self.annotation, "human_evidence_quotes": [translation["body_ko"]]}
        with self.assertRaisesRegex(ValueError, "literal source quote"):
            review.validate_annotations(data, [altered])
        html = review.HTML
        self.assertIn("[d.row.title,d.row.body]", html)
        self.assertNotIn("d.translation.body_ko].some", html)
        self.assertIn("번역문이 아닌 원문", html)

    def test_translation_precedes_always_visible_original_and_uses_text_content(self):
        out = self.root / "bilingual.html"
        review.generate(self.inputs, out, self.translations([self.translation(warnings=["고유명사를 원문과 대조하세요."])]))
        html = out.read_text(encoding="utf8")
        self.assertLess(html.index('id="translation"'), html.index('id="original"'))
        self.assertIn('<section id="original">', html)
        self.assertNotIn('<details', html)
        self.assertNotIn('id="original" hidden', html)
        self.assertIn("el('title_ko').textContent=", html)
        self.assertIn("el('body_ko').textContent=", html)
        self.assertIn("el('translation_warnings').textContent=", html)
        self.assertNotIn("innerHTML", html)
        self.assertIn("scrollIntoView", html)
        self.assertIn("고유명사를 원문과 대조하세요.", html)

    def test_translation_text_and_provenance_cannot_execute_html_or_network(self):
        unsafe = '</script><img src=https://example.invalid/x onerror=alert(1)>\u2028\u2029'
        path = self.translations([self.translation(title_ko=unsafe, body_ko=unsafe, translator=unsafe, warnings=[unsafe])])
        out = self.root / "safe.html"
        review.generate(self.inputs, out, path)
        html = out.read_text(encoding="utf8")
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn("<img src=", html)
        self.assertIn("\\u003c/script>", html)
        self.assertIn("\\u2028", html)
        self.assertIn("\\u2029", html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("localStorage", html)
        self.assertIn("connect-src 'none'", html)
        embedded = self.embedded_data(html)[0]["translation"]
        self.assertEqual(embedded["body_ko"], unsafe)
        self.assertEqual(embedded["warnings"], [unsafe])

    def test_translation_identity_hash_duplicates_and_language_fail_before_output(self):
        bad = [{"article_id": "other"}, {"ticker": "OTHER"}, {"article_id": 123}, {"ticker": None},
               {"input_sha256": "0" * 64}, {"source_language": "ko"}, {"target_language": "en"}]
        for index, change in enumerate(bad):
            path = self.translations([self.translation(**change)])
            out = self.root / ("invalid-" + str(index) + ".html")
            with self.subTest(change=change), self.assertRaises(ValueError):
                review.generate(self.inputs, out, path)
            self.assertFalse(out.exists())
        for records in ([self.translation(), self.translation()], [self.translation(), self.translation(status="pending")]):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                review.with_translations(self.data, self.translations(records))

    def test_complete_translation_requires_both_texts_and_valid_metadata(self):
        bad = [{"title_ko": ""}, {"title_ko": "  "}, {"body_ko": ""}, {"body_ko": None},
               {"body_ko": ["nested"]}, {"warnings": "warning"}, {"warnings": [False]},
               {"status": ""}, {"translator": {}}, {"translator": ""}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                review.with_translations(self.data, self.translations([self.translation(**change)]))
        for field in ("title_ko", "body_ko", "warnings", "translator", "status"):
            row = self.translation()
            del row[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                review.with_translations(self.data, self.translations([row]))

    def test_incomplete_translation_is_explicit_and_partial_text_is_not_embedded(self):
        for index, status in enumerate(("incomplete", "failed", "pending")):
            row = self.translation(status=status, title_ko="PARTIAL_TITLE_SECRET", body_ko="PARTIAL_BODY_SECRET",
                                   warnings=["번역 출력이 중단됐습니다."])
            out = self.root / ("partial-" + str(index) + ".html")
            review.generate(self.inputs, out, self.translations([row]))
            html = out.read_text(encoding="utf8")
            embedded = self.embedded_data(html)[0]
            self.assertEqual(embedded["translation"]["status"], status)
            self.assertNotIn("PARTIAL_", html)
            self.assertNotIn("title_ko", embedded["translation"])
            self.assertNotIn("body_ko", embedded["translation"])
            self.assertIn("번역 미완료", html)
            self.assertIn("원문으로 검수하세요.", html)
            self.assertEqual(embedded["row"], self.row)
        pending = self.translation(status="pending")
        pending.pop("title_ko")
        pending.pop("body_ko")
        self.assertEqual(review.with_translations(self.data, self.translations([pending]))[0]["translation"]["status"], "pending")

    def test_partial_translation_coverage_never_drops_or_reorders_source_articles(self):
        second = {**self.row, "article_id": "fiction2", "ticker": "FICTION2", "title": "Second source title"}
        self.inputs.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in (self.row, second)), encoding="utf8")
        data = review.packet(self.inputs)
        translated = review.with_translations(data, self.translations([self.translation()]))
        self.assertEqual(len(translated), 2)
        self.assertEqual([x["row"] for x in translated], [x["row"] for x in data])
        self.assertIn("translation", translated[0])
        self.assertNotIn("translation", translated[1])
        self.assertEqual(review.with_translations(data, self.translations([])), data)

    def test_existing_form_cannot_be_overwritten_when_translations_are_added(self):
        out = self.root / "original.html"
        review.generate(self.inputs, out)
        before = out.read_bytes()
        self.assertNotIn("translation", self.embedded_data(before.decode("utf8"))[0])
        with self.assertRaises(FileExistsError):
            review.generate(self.inputs, out, self.translations([self.translation()]))
        self.assertEqual(out.read_bytes(), before)

    @unittest.skipUnless(shutil.which("node"), "Node is optional for the in-memory browser-script regression")
    def test_browser_script_navigation_and_existing_import_keep_original_quote_contract(self):
        second = {**self.row, "article_id": "fiction2", "ticker": "FICTION2", "title": "Second source title"}
        self.inputs.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in (self.row, second)), encoding="utf8")
        out = self.root / "interactive-test.html"
        review.generate(self.inputs, out, self.translations([self.translation()]))
        script = out.read_text(encoding="utf8").split("<script>", 1)[1].split("</script>", 1)[0]
        # Minimal DOM doubles run the actual generated JavaScript. No browser,
        # network, model, storage, actual inputs or annotation artifacts are used.
        setup = """
const assert=require('node:assert/strict');
const nodes={};
const document={getElementById(id){return nodes[id]||(nodes[id]={value:'',textContent:'',hidden:false,scrollIntoView(){}});}};
"""
        checks = """
assert.equal(el('translation').hidden,false);
assert.equal(el('title_ko').textContent,DATA[0].translation.title_ko);
assert.equal(el('body').textContent,DATA[0].row.body);
assert.equal(Object.hasOwn(answers[0],'translation'),false);
assert.throws(()=>check({...answers[0],human_evidence_quotes:[DATA[0].translation.body_ko]},DATA[0],false));
el('next').onclick();
assert.equal(el('translation').hidden,true);
assert.equal(el('title_ko').textContent,'');
assert.equal(el('body_ko').textContent,'');
assert.equal(el('title').textContent,DATA[1].row.title);
el('prev').onclick();
assert.equal(el('body_ko').textContent,DATA[0].translation.body_ko);
(async()=>{
const originalAnnotation=__ANNOTATION__;
await el('import').onchange({target:{files:[{text:async()=>JSON.stringify(originalAnnotation)+'\\n'}]}});
assert.equal(answers[0].human_reviewed,true);
assert.deepEqual(answers[0],originalAnnotation);
assert.equal(answers[0].input_sha256,DATA[0].input_sha256);
assert.equal(answers[1].human_reviewed,false);
assert.equal(el('quotes').value,originalAnnotation.human_evidence_quotes.join('\\n---\\n'));
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("__ANNOTATION__", json.dumps(self.annotation, ensure_ascii=False))
        process = subprocess.run([shutil.which("node"), "-"], input=setup + script + checks,
                                 capture_output=True, text=True, encoding="utf8", timeout=10)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
