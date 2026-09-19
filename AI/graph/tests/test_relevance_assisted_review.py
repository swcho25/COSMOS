"""Fictional CPU/DOM fixtures for the separate assisted human-selection form."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_relevance_assisted_review as review


class AssistedReviewTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".assisted-review-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.inputs, self.assessments, self.translations = [self.root / (x + ".jsonl") for x in ("inputs", "assessments", "translations")]
        self.rows = [{"article_id": "fiction-" + str(i), "ticker": "FICTION", "company_name": "Fiction Company",
            "title": "A new contract", "body": "Fiction Company announced a new contract. </script><img src=x onerror=alert(1)>",
            "published_at": "2021-05-01", "case_kind": "real", "source_split": "train", "company_context": None} for i in range(2)]
        self.write(self.inputs, self.rows)
        self.source = review.strict.packet(self.inputs)
        self.ai = [{"article_id": d["row"]["article_id"], "ticker": d["row"]["ticker"], "input_sha256": d["input_sha256"],
            "ai_selection": "채택", "ai_label": "직접관련", "ai_time_status": "신규", "ai_reason": "회사의 신규 계약이 원문에 명시됩니다.",
            "ai_evidence_quotes": ["Fiction Company announced a new contract."], "ai_source": "fictional AI review", "human_gold": False,
            "review_provenance": {"model_outputs_seen": True, "sample_number": i}} for i, d in enumerate(self.source)]
        self.write(self.assessments, self.ai)
        self.write(self.translations, [{"article_id": self.rows[0]["article_id"], "ticker": "FICTION", "input_sha256": self.source[0]["input_sha256"],
            "title_ko": "새로운 계약", "body_ko": "가상회사가 신규 계약을 발표했다.", "status": "complete", "translator": "fiction translator",
            "source_language": "en", "target_language": "ko", "warnings": []}])
        self.data = review.assisted_packet(self.inputs, self.assessments, self.translations)
        self.legacy = {"article_id": self.rows[0]["article_id"], "ticker": "FICTION", "input_sha256": self.source[0]["input_sha256"],
            "human_selection": "채택", "human_label": "직접관련", "human_time_status": "신규",
            "human_evidence_quotes": ["Fiction Company announced a new contract."], "human_reason": "새 계약", "reviewed_by": "fixture-human",
            "human_reviewed": True, "completed_at": "2026-09-17T00:00:00+09:00"}

    def tearDown(self):
        if self.root.resolve().parent != self.parent or not self.root.name.startswith(".assisted-review-test-"):
            raise RuntimeError("Refusing cleanup outside this test workspace")
        shutil.rmtree(self.root)

    @staticmethod
    def write(path, rows):
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf8")

    def completed(self, i=0, selection="채택"):
        return {**review.blank(self.data[i]), "human_selection": selection, "human_selected_at": "2026-09-17T12:00:00+09:00"}

    def generate(self):
        out = self.root / "assisted.html"
        review.generate(self.inputs, out, self.translations, self.assessments)
        return out, out.read_text(encoding="utf8")

    def test_ai_recommendation_never_initializes_a_human_choice(self):
        for item in self.data:
            value = review.blank(item)
            self.assertIsNone(value["human_selection"])
            self.assertIsNone(value["human_selected_at"])
            self.assertIsNone(value["prior_annotation"])
            self.assertEqual(value["schema"], "cosmos-assisted-selection-1")
            self.assertEqual(value["review_mode"], "ai_assisted_selection")
            self.assertIs(value["independent_human_gold"], False)
            self.assertNotIn("human_reviewed", value)
        summary = review.validate_selections(self.data, [])
        self.assertEqual((summary["human_selected"], summary["pending"]), (0, 2))
        self.assertFalse(summary["independent_human_gold"])

    def test_assessment_keys_hashes_complete_coverage_and_source_quotes_are_checked(self):
        changes = [{"article_id": "unknown"}, {"ticker": "other"}, {"input_sha256": "0" * 64}, {"human_gold": True},
            {"human_gold": 0}, {"ai_selection": "up"}, {"ai_label": "positive"}, {"ai_time_status": "future-return"},
            {"ai_reason": " "}, {"ai_source": None}, {"ai_evidence_quotes": ["가상회사가 신규 계약을 발표했다."]},
            {"ai_evidence_quotes": "not a list"}, {"ai_evidence_quotes": [None]}]
        for change in changes:
            records = copy.deepcopy(self.ai)
            records[0].update(change)
            self.write(self.assessments, records)
            with self.subTest(change=change), self.assertRaises(ValueError):
                review.assisted_packet(self.inputs, self.assessments)
        for records in (self.ai[:1], self.ai + [self.ai[0]], [dict(self.ai[0], article_id="extra"), self.ai[1]]):
            self.write(self.assessments, records)
            with self.assertRaises(ValueError):
                review.assisted_packet(self.inputs, self.assessments)
        missing = copy.deepcopy(self.ai)
        missing[0].pop("ai_reason")
        self.write(self.assessments, missing)
        with self.assertRaises(ValueError):
            review.assisted_packet(self.inputs, self.assessments)

    def test_additional_ai_provenance_is_preserved_and_bound_by_hash(self):
        self.assertEqual(self.data[0]["ai_assessment"], self.ai[0])
        modified = copy.deepcopy(self.completed())
        modified["ai_assessment"]["review_provenance"]["model_outputs_seen"] = False
        with self.assertRaisesRegex(ValueError, "AI assessment"):
            review.import_records(self.data, [modified])

    def test_decisive_ai_choices_require_source_evidence_but_hold_may_abstain(self):
        for selection in ("채택", "제외", "보류"):
            records = copy.deepcopy(self.ai)
            records[0].update(ai_selection=selection, ai_evidence_quotes=[])
            self.write(self.assessments, records)
            with self.subTest(selection=selection):
                if selection == "보류":
                    data = review.assisted_packet(self.inputs, self.assessments)
                    self.assertEqual(data[0]["ai_assessment"]["ai_evidence_quotes"], [])
                    self.assertIsNone(review.blank(data[0])["human_selection"])
                else:
                    with self.assertRaisesRegex(ValueError, "requires original evidence"):
                        review.assisted_packet(self.inputs, self.assessments)

    def test_json_number_roundtrip_preserves_values_without_equating_boolean_to_zero(self):
        ai = copy.deepcopy(self.ai)
        ai[0]["review_provenance"]["diagnostic_number"] = 1.0
        self.write(self.assessments, ai)
        data = review.assisted_packet(self.inputs, self.assessments)
        exported = copy.deepcopy(review.blank(data[0]))
        exported["ai_assessment"]["review_provenance"]["diagnostic_number"] = 1
        review.import_records(data, [exported])
        exported["ai_assessment"]["human_gold"] = 0
        with self.assertRaises(ValueError):
            review.import_records(data, [exported])

    def test_human_selects_without_reason_quote_name_or_assumed_correctness(self):
        for selection in ("채택", "제외", "보류"):
            value = self.completed(selection=selection)
            result = review.import_records(self.data, [value])
            self.assertEqual(result[0]["human_selection"], selection)
            self.assertIsNone(result[1]["human_selection"])
            for field in ("human_reason", "human_evidence_quotes", "reviewed_by", "human_reviewed"):
                self.assertNotIn(field, result[0])
            report = review.validate_selections(self.data, [value])
            self.assertEqual((report["human_selected"], report["pending"]), (1, 1))
            self.assertFalse(report["judgment_correctness_verified"])

    def test_tampered_ai_values_source_hash_or_gold_claim_rejected(self):
        original = self.completed()
        changes = [{"input_sha256": "wrong"}, {"ai_assessment_sha256": "wrong"}, {"ai_assessment": {}},
                   {"schema": "old"}, {"review_mode": "blind"}, {"independent_human_gold": True},
                   {"independent_human_gold": 0}, {"human_selection": "increase"}, {"human_selected_at": None},
                   {"human_selected_at": " "}, {"human_reviewed": True}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                review.import_records(self.data, [{**original, **change}])
        with self.assertRaises(ValueError):
            review.import_records(self.data, [original, original])
        draft = {**review.blank(self.data[0]), "human_selected_at": "invented completion"}
        with self.assertRaises(ValueError):
            review.import_records(self.data, [draft])

    def test_legacy_completion_migrates_selection_and_preserves_all_original_fields(self):
        before = copy.deepcopy(self.legacy)
        result = review.import_records(self.data, [self.legacy])
        self.assertEqual(result[0]["prior_annotation"], before)
        self.assertEqual(result[0]["human_selection"], "채택")
        self.assertEqual(result[0]["human_selected_at"], self.legacy["completed_at"])
        self.assertFalse(result[0]["independent_human_gold"])
        self.assertNotIn("human_reviewed", result[0])
        self.assertEqual(self.legacy, before)
        # A later assisted choice may differ while retaining the old annotation.
        changed = {**result[0], "human_selection": "보류"}
        self.assertEqual(review.import_records(self.data, [changed])[0]["prior_annotation"], before)

    def test_incomplete_legacy_draft_never_becomes_a_human_selection(self):
        legacy = {**self.legacy, "human_reviewed": False, "completed_at": None}
        result = review.import_records(self.data, [legacy])[0]
        self.assertEqual(result["prior_annotation"], legacy)
        self.assertIsNone(result["human_selection"])
        self.assertIsNone(result["human_selected_at"])

    def test_invalid_legacy_or_hidden_prior_annotation_rejected(self):
        for change in ({"input_sha256": "wrong"}, {"human_evidence_quotes": ["translated text"]},
                       {"human_reason": ""}, {"reviewed_by": ""}, {"human_reviewed": 1}, {"completed_at": None}):
            legacy = {**self.legacy, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                review.import_records(self.data, [legacy])
            with self.assertRaises(ValueError):
                review.import_records(self.data, [{**self.completed(), "prior_annotation": legacy}])

    def test_html_preserves_originals_escapes_text_and_avoids_network_storage(self):
        before = {p: p.read_bytes() for p in (self.inputs, self.assessments, self.translations)}
        ai = copy.deepcopy(self.ai)
        ai[0]["ai_reason"] = '</script><img src=x onerror=alert(1)>\u2028\u2029'
        self.write(self.assessments, ai)
        before[self.assessments] = self.assessments.read_bytes()
        out, html = self.generate()
        embedded = json.JSONDecoder().raw_decode(html.split("const DATA=", 1)[1])[0]
        self.assertEqual([d["row"] for d in embedded], self.rows)
        self.assertEqual([d["input_sha256"] for d in embedded], [d["input_sha256"] for d in self.source])
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn("<img src=x", html)
        self.assertIn("\\u2028", html)
        self.assertIn("connect-src 'none'", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("fetch(", html)
        self.assertIn("el('reason').textContent=ai.ai_reason", html)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        prior = out.read_bytes()
        with self.assertRaises(FileExistsError):
            review.generate(self.inputs, out, self.translations, self.assessments)
        self.assertEqual(out.read_bytes(), prior)

    @unittest.skipUnless(shutil.which("node"), "Node optional for actual generated JavaScript contract tests")
    def test_actual_js_three_click_choices_undo_export_and_import_boundaries(self):
        _, html = self.generate()
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        setup = r'''
const assert=require('node:assert/strict');
function node(){return {textContent:'',hidden:false,checked:false,disabled:false,children:[],attrs:{},replaceChildren(){this.children=[];this.textContent='';},appendChild(x){this.children.push(x);},setAttribute(k,v){this.attrs[k]=v;},click(){}};}
const nodes={},document={getElementById(id){return nodes[id]||(nodes[id]=node());},createElement(){return node();}};
let exported='';const Blob=class{constructor(parts){this.text=parts.join('');}};
const URL={createObjectURL(b){exported=b.text;return 'blob:local';},revokeObjectURL(){}};
const setTimeout=fn=>fn();
'''
        checks = r'''
assert.equal(answers[0].human_selection,null);
assert.equal(el('headline').textContent,'새로운 계약');
assert.equal(el('reason').textContent,DATA[0].ai_assessment.ai_reason);
assert.equal(el('evidence').children[0].textContent,DATA[0].ai_assessment.ai_evidence_quotes[0]);
el('adopt').onclick();assert.equal(answers[0].human_selection,'채택');assert.ok(answers[0].human_selected_at);
assert.equal(Object.hasOwn(answers[0],'human_reviewed'),false);assert.equal(index,0);
el('clear').onclick();assert.equal(answers[0].human_selection,null);assert.equal(answers[0].human_selected_at,null);
el('undo').onclick();assert.equal(answers[0].human_selection,'채택');
el('auto_next').checked=true;el('exclude').onclick();assert.equal(index,1);assert.equal(answers[0].human_selection,'제외');
assert.equal(el('translated').hidden,true);assert.equal(el('translated_body').textContent,'');
el('undo').onclick();assert.equal(index,0);assert.equal(answers[0].human_selection,'채택');
el('hold').onclick();assert.equal(answers[0].human_selection,'보류');
el('export').onclick();const saved=exported.trim().split('\n').map(JSON.parse);
assert.equal(saved.length,2);assert.equal(saved[0].schema,SCHEMA);assert.equal(saved[0].independent_human_gold,false);
assert.equal(saved[1].human_selection,null);assert.deepEqual(saved[0].ai_assessment,DATA[0].ai_assessment);
const load=rows=>el('import').onchange({target:{files:[{text:async()=>rows.map(x=>JSON.stringify(x)).join('\n')+'\n'}]}});
(async()=>{
await load(saved);assert.equal(answers[0].human_selection,'보류');
const old=__LEGACY__;await load([old]);assert.equal(answers[0].human_selection,'채택');assert.deepEqual(answers[0].prior_annotation,old);
const draft={...old,human_reviewed:false,completed_at:null};await load([draft]);assert.equal(answers[0].human_selection,null);assert.deepEqual(answers[0].prior_annotation,draft);
const before=stable(answers);let bad=JSON.parse(JSON.stringify(saved));bad[0].ai_assessment.ai_reason='tampered';await load(bad);assert.equal(stable(answers),before);assert.match(el('status').textContent,/AI 추천/);
bad=JSON.parse(JSON.stringify(saved));bad[0].ai_assessment_sha256='wrong';await load(bad);assert.equal(stable(answers),before);
await load([{...old,human_evidence_quotes:['번역문']}]);assert.equal(stable(answers),before);assert.match(el('status').textContent,/인용/);
await load([saved[0],saved[0]]);assert.equal(stable(answers),before);assert.match(el('status').textContent,/중복/);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace("__LEGACY__", json.dumps(self.legacy, ensure_ascii=False))
        result = subprocess.run([shutil.which("node"), "-"], input=setup + script + checks,
                                capture_output=True, text=True, encoding="utf8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
