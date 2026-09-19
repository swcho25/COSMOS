"""Offline human annotation form; generating it never creates human labels.

The form shows source text and optional AI translations, never model verdicts.
Translations are reading aids only; evidence must still quote the original text.
It stores edits in tab memory and exports an explicit JSONL file; no network or
browser storage is used.
The validate command checks self-attested human annotations against the immutable
source packet. It verifies provenance and quotes, not correctness of human judgment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import local_news_relevance as base

CHOICES = {"human_selection": ("채택", "제외", "보류"),
           "human_label": ("직접관련", "간접관련", "단순언급", "판단보류"),
           "human_time_status": ("신규", "과거", "불명")}
ALLOWED = {"article_id", "ticker", "input_sha256", *CHOICES, "human_evidence_quotes",
           "human_reason", "reviewed_by", "human_reviewed", "completed_at"}


def packet(path):
    result = []
    for row in base.read_rows(path):
        base.validate_input(row)
        if any(k in row for k in ("ai_reference", "prediction", "stages", "expected_selection")):
            raise ValueError("Human source packet must not contain model predictions")
        result.append({"row": row, "input_sha256": hashlib.sha256(base.canonical(row).encode()).hexdigest()})
    if not result or len({base.pair_key(x["row"]) for x in result}) != len(result):
        raise ValueError("Empty or duplicate human packet")
    return result


def with_translations(inputs, path):
    """Attach validated read aids beside, never inside, immutable input rows.

    A translation file may cover only a subset of the packet. Extra provenance
    keys are allowed but not embedded in the form. Non-complete records expose
    status/warnings only: their partial text cannot masquerade as finished text.
    """
    lookup = {base.pair_key(item["row"]): item for item in inputs}
    translated = {}
    # JSONL records end at actual newlines; U+2028/U+2029 inside valid JSON
    # strings must not be treated as line delimiters by str.splitlines().
    with Path(path).open(encoding="utf-8-sig") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    for value in records:
        if not isinstance(value, dict):
            raise ValueError("Translation must be an object")
        for field in ("article_id", "ticker", "input_sha256", "status", "translator"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise ValueError("Missing translation string: " + field)
        key = value["article_id"], value["ticker"]
        if key in translated or key not in lookup:
            raise ValueError("Duplicate or unknown translation identity")
        if value["input_sha256"] != lookup[key]["input_sha256"]:
            raise ValueError("Translation source hash differs from input")
        if value.get("source_language") != "en" or value.get("target_language") != "ko":
            raise ValueError("Translation must explicitly be en to ko")
        warnings = value.get("warnings")
        if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
            raise ValueError("Translation warnings must be a list of strings")
        for field in ("title_ko", "body_ko"):
            if field in value and not isinstance(value[field], str):
                raise ValueError("Translation text must be a string: " + field)
            if value["status"] == "complete" and not value.get(field, "").strip():
                raise ValueError("Complete translation requires title and body")
        item = {field: value[field] for field in ("status", "translator", "source_language", "target_language", "warnings")}
        if value["status"] == "complete":
            item.update(title_ko=value["title_ko"], body_ko=value["body_ko"])
        translated[key] = item
    return [{**item, "translation": translated[base.pair_key(item["row"])]}
            if base.pair_key(item["row"]) in translated else dict(item) for item in inputs]


def validate_annotations(inputs, annotations):
    lookup = {base.pair_key(x["row"]): x for x in inputs}
    seen, complete = set(), 0
    for value in annotations:
        if not isinstance(value, dict) or set(value) != ALLOWED:
            raise ValueError("Wrong annotation keys")
        key = base.pair_key(value)
        if key in seen or key not in lookup:
            raise ValueError("Duplicate or unknown annotation identity")
        seen.add(key)
        source = lookup[key]
        if value["input_sha256"] != source["input_sha256"]:
            raise ValueError("Article changed since annotation")
        if type(value["human_reviewed"]) is not bool:
            raise ValueError("Human completion must be explicit")
        for field in ("human_reason", "reviewed_by"):
            if not isinstance(value[field], str):
                raise ValueError("Invalid annotation text")
        quotes = value["human_evidence_quotes"]
        if not isinstance(quotes, list) or any(not isinstance(q, str) or not q or
                not any(q in source["row"][f] for f in ("title", "body")) for q in quotes):
            raise ValueError("Human evidence is not a literal source quote")
        for field, choices in CHOICES.items():
            if value[field] is not None and value[field] not in choices:
                raise ValueError("Invalid annotation choice")
        if value["human_reviewed"]:
            if any(value[f] not in choices for f, choices in CHOICES.items()):
                raise ValueError("Completed review is missing a decision")
            if not value["human_reason"].strip() or not value["reviewed_by"].strip():
                raise ValueError("Completed review requires a reviewer and reason")
            if value["human_selection"] in ("채택", "제외") and not quotes:
                raise ValueError("Decisive human selection needs source evidence")
            if not isinstance(value["completed_at"], str) or not value["completed_at"]:
                raise ValueError("Completed review requires a timestamp")
            complete += 1
        elif value["completed_at"] is not None:
            raise ValueError("Draft must not claim completion time")
    return {"source_pairs": len(inputs), "records": len(annotations), "completed_human_attestations": complete,
            "pending": len(inputs) - complete, "judgment_correctness_verified": False}


HTML = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'">
<title>Cosmos 뉴스 관련성 사람 검수</title><style>
body{font:16px/1.65 system-ui,sans-serif;margin:24px auto;max-width:1400px;padding:0 20px;color:#1f2937;background:#f5f7fb}
h1{font-size:24px} .bar,article,aside{background:white;border:1px solid #d7dfeb;border-radius:10px;padding:18px}
.layout{display:grid;grid-template-columns:1.4fr 1fr;gap:18px;margin:18px 0} pre{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.8 system-ui}
button,select,input,textarea{font:inherit;padding:8px;max-width:100%;box-sizing:border-box}button{cursor:pointer;margin:4px;border:1px solid #bbc7d8;border-radius:6px;background:#eef3fa}
textarea{width:100%;min-height:90px}label{display:block;margin:12px 0}#progress{font-weight:bold}#status{color:#9b1c1c;white-space:pre-wrap}
.muted{color:#526178;font-size:14px}h2{font-size:20px}.translation{background:#eef5ff;border:1px solid #c9dafa;border-radius:8px;padding:16px;margin:16px 0 24px}
.translation h3{margin-top:0}.translation-notice{font-weight:600}.translation-warnings{color:#8a4210;white-space:pre-wrap}#original{border-top:1px solid #d7dfeb;padding-top:12px}
@media(max-width:950px){.layout{grid-template-columns:1fr}}</style>
<h1>Cosmos 뉴스·기업 관련성 검수</h1>
<p>실제 사람이 원문을 읽고 판정하는 양식입니다. 모델 예측은 표시하지 않습니다. 채택은 주가 변동을 보장하지 않습니다.</p>
<div class="bar"><span id="progress"></span><br><button id="prev">이전</button><button id="next">다음</button>
<button id="export">검수 파일 내보내기</button><label>이전 검수 파일 불러오기 <input type="file" id="import" accept=".jsonl,.json"></label>
<p class="muted">편집 내용은 이 탭의 메모리에만 있습니다. 창을 닫기 전에 파일로 내보내세요. 서버 전송·자동 저장은 없습니다.</p></div>
<div class="layout"><article><div id="meta" class="muted"></div>
<section id="translation" class="translation" hidden><h3>AI 한국어 번역 · 읽기 보조</h3>
<p class="translation-notice">AI 번역에는 누락·오역이 있을 수 있습니다. 판단이 애매하면 아래 원문을 확인하세요. 근거는 번역문이 아닌 원문에서 그대로 복사해야 합니다.</p>
<p id="translation_status" class="muted"></p><p id="translation_warnings" class="translation-warnings"></p>
<button id="source_jump" type="button">아래 원문 확인·근거 복사</button><h2 id="title_ko"></h2><pre id="body_ko"></pre></section>
<section id="original"><h3>원문 · 근거 복사용</h3><h2 id="title"></h2><pre id="body"></pre></section></article>
<aside><p>① 경제적 사건인가? ② 대상 기업 귀속 근거가 있는가? ③ 신규 사건인가?<br>
과거 직장·약력·광고·추천 뉴스만의 이름은 제외합니다. 계열사·과거 사업·산업 연결은 당시 근거가 없으면 보류합니다.
기사 밖 지식이나 이후 주가를 근거로 쓰지 마세요.</p>
<label>검수자 이름/식별자 <input id="reviewed_by"></label>
<label>학습 후보 <select id="human_selection"><option value="">선택</option><option>채택</option><option>제외</option><option>보류</option></select></label>
<label>관련성 <select id="human_label"><option value="">선택</option><option>직접관련</option><option>간접관련</option><option>단순언급</option><option>판단보류</option></select></label>
<label>사건 시점 <select id="human_time_status"><option value="">선택</option><option>신규</option><option>과거</option><option>불명</option></select></label>
<label>원문 근거 그대로 복사 <textarea id="quotes" placeholder="번역문 대신 원문에서 복사하세요. 여러 인용은 한 줄에 --- 를 넣어 구분하세요."></textarea></label>
<label>판정 이유 <textarea id="human_reason"></textarea></label>
<button id="complete">이 기사를 직접 검수 완료로 표시</button><div id="status"></div></aside></div>
<script>
const DATA=__DATA__;
const fields=['human_selection','human_label','human_time_status','human_reason','reviewed_by'];
const el=id=>document.getElementById(id);let index=0;
const blank=d=>({article_id:d.row.article_id,ticker:d.row.ticker,input_sha256:d.input_sha256,human_selection:null,human_label:null,human_time_status:null,human_evidence_quotes:[],human_reason:'',reviewed_by:'',human_reviewed:false,completed_at:null});
let answers=DATA.map(blank);
function save(){const a=answers[index];for(const f of fields)a[f]=el(f).value||(['human_reason','reviewed_by'].includes(f)?'':null);
a.human_evidence_quotes=el('quotes').value.split(/\r?\n---\r?\n/).map(s=>s.trim()).filter(Boolean);}
function render(){const d=DATA[index],a=answers[index];el('meta').textContent=`${d.row.company_name} (${d.row.ticker}) · ${d.row.published_at} · ${d.row.article_id}`;
const t=d.translation;el('translation').hidden=!t;el('title_ko').textContent=t&&t.status==='complete'?t.title_ko:'';
el('body_ko').textContent=t&&t.status==='complete'?t.body_ko:'';
el('translation_status').textContent=t?(t.status==='complete'?`AI 번역 제공 · 번역기: ${t.translator}`:`번역 미완료 (${t.status}) · 원문으로 검수하세요. · 번역기: ${t.translator}`):'';
el('translation_warnings').textContent=t?t.warnings.map(w=>'번역 주의: '+w).join('\n'):'';
el('title').textContent=d.row.title;el('body').textContent=d.row.body;for(const f of fields)el(f).value=a[f]||'';
el('quotes').value=a.human_evidence_quotes.join('\n---\n');el('progress').textContent=`${index+1} / ${DATA.length} · 완료 ${answers.filter(a=>a.human_reviewed).length}건`;
el('status').textContent=a.human_reviewed?'직접 검수 완료로 표시됨':'미완료';}
function check(a,d,completed){if(a.article_id!==d.row.article_id||a.ticker!==d.row.ticker||a.input_sha256!==d.input_sha256)throw Error('입력 기사와 검수 파일이 다릅니다.');
const choices={human_selection:['채택','제외','보류'],human_label:['직접관련','간접관련','단순언급','판단보류'],human_time_status:['신규','과거','불명']};
if(typeof a.human_reviewed!=='boolean'||typeof a.human_reason!=='string'||typeof a.reviewed_by!=='string')throw Error('검수 필드 형식 오류');
if(!Array.isArray(a.human_evidence_quotes)||a.human_evidence_quotes.some(q=>typeof q!=='string'||!q||!([d.row.title,d.row.body].some(t=>t.includes(q)))))throw Error('근거는 제목/본문에 있는 원문을 그대로 복사해야 합니다.');
for(const [f,c] of Object.entries(choices))if(a[f]!==null&&!c.includes(a[f]))throw Error('판정 선택값 오류');
if(completed){if(Object.keys(choices).some(f=>!choices[f].includes(a[f]))||!a.human_reason.trim()||!a.reviewed_by.trim())throw Error('판정·관련성·시점·검수자·이유를 입력하세요.');
if(['채택','제외'].includes(a.human_selection)&&!a.human_evidence_quotes.length)throw Error('채택/제외에는 원문 근거가 필요합니다.');}}
for(const f of [...fields,'quotes'])el(f).oninput=()=>{answers[index].human_reviewed=false;answers[index].completed_at=null;el('status').textContent='수정됨: 완료 표시를 다시 눌러주세요.';};
el('source_jump').onclick=()=>el('original').scrollIntoView({block:'start',behavior:'smooth'});
el('prev').onclick=()=>{save();index=Math.max(0,index-1);render();};el('next').onclick=()=>{save();index=Math.min(DATA.length-1,index+1);render();};
el('complete').onclick=()=>{try{save();check(answers[index],DATA[index],true);answers[index].human_reviewed=true;answers[index].completed_at=new Date().toISOString();render();}catch(e){el('status').textContent=e.message;}};
el('export').onclick=()=>{try{save();answers.forEach((a,i)=>check(a,DATA[i],a.human_reviewed));const blob=new Blob([answers.map(a=>JSON.stringify(a)).join('\n')+'\n'],{type:'application/x-ndjson'});
const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='cosmos-human-relevance-review.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){el('status').textContent=e.message;}};
el('import').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;const rows=(await file.text()).split(/\r?\n/).filter(x=>x.trim()).map(x=>JSON.parse(x));
const next=DATA.map(blank),seen=new Set();for(const a of rows){const i=DATA.findIndex(d=>d.row.article_id===a.article_id&&d.row.ticker===a.ticker);if(i<0||seen.has(i))throw Error('중복 또는 다른 기사의 검수 파일입니다.');
if(JSON.stringify(Object.keys(a).sort())!==JSON.stringify(Object.keys(next[i]).sort()))throw Error('검수 필드가 다릅니다.');
check(a,DATA[i],a.human_reviewed);if(a.human_reviewed?typeof a.completed_at!=='string'||!a.completed_at:a.completed_at!==null)throw Error('검수 완료 시각 오류');seen.add(i);next[i]=a;}answers=next;render();}catch(e){el('status').textContent=e.message;}};
render();
</script></html>'''


def generate(inputs, out, translations=None):
    if out.exists():
        raise FileExistsError("Do not overwrite a human review form")
    data = packet(inputs)
    if translations is not None:
        data = with_translations(data, translations)
    payload = base.canonical(data).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf8") as stream:
        stream.write(HTML.replace("__DATA__", payload))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--translations", type=Path, help="Optional en-to-ko JSONL reading aids; original-source evidence remains required")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--validate", type=Path)
    args = parser.parse_args()
    if args.translations is not None and not args.out:
        parser.error("--translations is only used when generating --out")
    if args.out:
        generate(args.inputs, args.out, args.translations)
        print(base.canonical({"form": str(args.out), "human_review_completed": 0}))
    else:
        print(base.canonical(validate_annotations(packet(args.inputs), base.read_rows(args.validate))))
