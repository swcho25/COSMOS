"""Offline AI-assisted selection: AI evidence and human choices stay separate.

This new format does not create independent human gold or populate the older
blind-review completion flag. Existing validated annotations can be carried
forward intact as prior_annotation. Nothing is sent over a network or autosaved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import build_relevance_human_review as strict
import local_news_relevance as base

SCHEMA = "cosmos-assisted-selection-1"
MODE = "ai_assisted_selection"
SELECTIONS = ("채택", "제외", "보류")
AI_REQUIRED = {"article_id", "ticker", "input_sha256", "ai_selection", "ai_label", "ai_time_status",
               "ai_reason", "ai_evidence_quotes", "ai_source", "human_gold"}
RESULT_KEYS = {"schema", "review_mode", "independent_human_gold", "article_id", "ticker", "input_sha256",
               "ai_assessment", "ai_assessment_sha256", "human_selection", "human_selected_at", "prior_annotation"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def assessment_canonical(value):
    """Keep JSON number values stable across browser export (1.0 becomes 1)."""
    def normalize(item):
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if type(item) is float and item.is_integer():
            return int(item)
        return item
    return canonical(normalize(value))


def records(path):
    # Only actual newlines delimit JSONL; Unicode paragraph separators are text.
    with Path(path).open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def assisted_packet(inputs, assessments, translations=None):
    data = strict.packet(inputs)
    if translations is not None:
        data = strict.with_translations(data, translations)
    source = {base.pair_key(item["row"]): item for item in data}
    found = {}
    for value in records(assessments):
        if not isinstance(value, dict) or not AI_REQUIRED.issubset(value):
            raise ValueError("Missing AI assessment fields")
        for field in ("article_id", "ticker", "input_sha256", "ai_reason", "ai_source"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError("Invalid AI assessment text: " + field)
        key = base.pair_key(value)
        if key not in source or key in found:
            raise ValueError("Unknown or duplicate AI assessment")
        item = source[key]
        if value["input_sha256"] != item["input_sha256"]:
            raise ValueError("AI assessment source hash differs")
        if value["human_gold"] is not False:
            raise ValueError("AI assessment is not human gold")
        for field, choices in (("ai_selection", SELECTIONS), ("ai_label", strict.CHOICES["human_label"]),
                               ("ai_time_status", strict.CHOICES["human_time_status"])):
            if not isinstance(value[field], str) or value[field] not in choices:
                raise ValueError("Invalid AI assessment choice: " + field)
        quotes = value["ai_evidence_quotes"]
        if not isinstance(quotes, list) or any(not isinstance(q, str) or not q or
                not any(q in item["row"][f] for f in ("title", "body")) for q in quotes):
            raise ValueError("AI evidence is not a literal original quote")
        if value["ai_selection"] in ("채택", "제외") and not quotes:
            raise ValueError("Decisive AI selection requires original evidence")
        digest = hashlib.sha256(assessment_canonical(value).encode("utf8")).hexdigest()
        found[key] = (value, digest)
    if set(found) != set(source):
        raise ValueError("Every source pair requires exactly one AI assessment")
    return [{**item, "ai_assessment": found[base.pair_key(item["row"])][0],
             "ai_assessment_sha256": found[base.pair_key(item["row"])][1]} for item in data]


def blank(item):
    return {"schema": SCHEMA, "review_mode": MODE, "independent_human_gold": False,
            "article_id": item["row"]["article_id"], "ticker": item["row"]["ticker"],
            "input_sha256": item["input_sha256"], "ai_assessment": item["ai_assessment"],
            "ai_assessment_sha256": item["ai_assessment_sha256"], "human_selection": None,
            "human_selected_at": None, "prior_annotation": None}


def _validate_selection(item, value):
    if not isinstance(value, dict) or set(value) != RESULT_KEYS:
        raise ValueError("Wrong assisted-selection keys")
    if (value["schema"] != SCHEMA or value["review_mode"] != MODE
            or value["independent_human_gold"] is not False):
        raise ValueError("Wrong assisted-selection provenance")
    if any(value[k] != blank(item)[k] for k in ("article_id", "ticker", "input_sha256")):
        raise ValueError("Selection source identity or hash differs")
    if (value["ai_assessment_sha256"] != item["ai_assessment_sha256"]
            or assessment_canonical(value["ai_assessment"]) != assessment_canonical(item["ai_assessment"])):
        raise ValueError("AI assessment or its hash differs")
    selection, selected_at = value["human_selection"], value["human_selected_at"]
    if selection is not None and (not isinstance(selection, str) or selection not in SELECTIONS):
        raise ValueError("Invalid human selection")
    if (selection is None and selected_at is not None) or (selection is not None and
            (not isinstance(selected_at, str) or not selected_at.strip())):
        raise ValueError("Human selection and timestamp disagree")
    prior = value["prior_annotation"]
    if prior is not None:
        strict.validate_annotations([item], [prior])


def import_records(data, values):
    """Return one new-format row per source; preserve verified legacy originals."""
    lookup = {base.pair_key(item["row"]): (i, item) for i, item in enumerate(data)}
    result, seen = [blank(item) for item in data], set()
    for value in values:
        if not isinstance(value, dict) or not all(k in value for k in ("article_id", "ticker")):
            raise ValueError("Missing selection identity")
        key = base.pair_key(value)
        if key in seen or key not in lookup:
            raise ValueError("Duplicate or unknown selection")
        seen.add(key)
        i, item = lookup[key]
        if set(value) == strict.ALLOWED:
            strict.validate_annotations([item], [value])
            result[i]["prior_annotation"] = value
            if value["human_reviewed"]:
                result[i]["human_selection"] = value["human_selection"]
                result[i]["human_selected_at"] = value["completed_at"]
        else:
            _validate_selection(item, value)
            result[i] = value
    return result


def validate_selections(data, values):
    result = import_records(data, values)
    selected = sum(row["human_selection"] is not None for row in result)
    return {"schema": SCHEMA, "review_mode": MODE, "source_pairs": len(data), "imported_records": len(values),
            "human_selected": selected, "pending": len(data) - selected, "independent_human_gold": False,
            "judgment_correctness_verified": False}


HTML = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'">
<title>Cosmos 뉴스 간단 선택</title><style>
body{font:16px/1.7 system-ui,sans-serif;margin:24px auto;max-width:1120px;padding:0 18px;background:#f5f7fb;color:#1f2937}
header,article,.toolbar{background:white;border:1px solid #d7dfeb;border-radius:12px;padding:20px;margin:16px 0}
h1{font-size:25px}h2{font-size:23px;margin:10px 0}h3{font-size:18px}p{margin:10px 0}.muted{color:#55647b;font-size:14px}
button,input{font:inherit;padding:10px;border:1px solid #bdc9dc;border-radius:7px;background:#f7f9fd}button{cursor:pointer;margin:4px}
.decisions{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.decisions button{flex:1;min-width:160px;padding:15px;font-size:18px;font-weight:700}
.decisions button[aria-pressed=true]{outline:3px solid #244ec2;background:#e6efff}#adopt{color:#17603b}#exclude{color:#973838}
.recommendation{border-left:5px solid #7597df;background:#eff5ff;padding:14px 18px;margin:18px 0}pre{font:16px/1.85 system-ui;white-space:pre-wrap;overflow-wrap:anywhere}
summary{cursor:pointer;font-weight:600}details{border-top:1px solid #dce3ed;padding:12px 0}blockquote{margin:10px 0;padding:12px;background:#f4f6fa;white-space:pre-wrap;overflow-wrap:anywhere}
label{display:block;margin:10px 0}#status{white-space:pre-wrap;color:#405472}#translation_warning{white-space:pre-wrap;color:#8a4210}.help{font-size:15px}
</style>
<h1>뉴스를 읽고 세 가지 중 하나만 선택하세요</h1>
<p class="help">실제 주가가 올랐는지·내렸는지를 맞히는 작업이 아닙니다. 대상 기업의 경제적 사건을 다룬 뉴스인지 보고 선택하면 됩니다. AI 추천은 참고용이며 틀릴 수 있습니다. 이유·인용·이름은 입력하지 않아도 됩니다.</p>
<div class="toolbar"><strong id="progress"></strong><br><button id="prev">이전 기사</button><button id="next">다음 기사</button>
<button id="undo">직전 선택 되돌리기</button><button id="clear">이 기사 선택 지우기</button>
<label><input type="checkbox" id="auto_next"> 선택 후 다음 기사로 이동</label>
<button id="export">내 선택 파일로 저장</button><label>이전 선택·검수 파일 불러오기 <input type="file" id="import" accept=".jsonl,.json"></label>
<p class="muted">선택은 이 탭의 메모리에만 남습니다. 창을 닫기 전에 파일로 저장하세요. 서버 전송·자동 저장은 없습니다. AI를 참고한 선택이며 독립적인 사람 정답으로 취급하지 않습니다.</p></div>
<header><div id="meta" class="muted"></div><h2 id="headline"></h2>
<div class="recommendation"><strong id="recommendation"></strong><p id="reason"></p><div id="ai_meta" class="muted"></div>
<details><summary>AI가 고른 원문 근거 보기</summary><div id="evidence"></div></details></div>
<p>내 선택</p><div class="decisions"><button id="adopt" aria-pressed="false">채택</button><button id="exclude" aria-pressed="false">제외</button><button id="hold" aria-pressed="false">잘 모르겠음</button></div>
<div id="status" role="status"></div></header>
<article><section id="translated" hidden><h3>한국어 번역 · AI 읽기 보조</h3><p class="muted">번역에는 오역·누락이 있을 수 있습니다. 아래 원문과 대조할 수 있습니다.</p><p id="translation_warning"></p><h3 id="translated_title"></h3><pre id="translated_body"></pre></section>
<section id="original"><h3>원문</h3><h3 id="source_title"></h3><pre id="source_body"></pre></section></article>
<script>
const DATA=__DATA__;
const SCHEMA='cosmos-assisted-selection-1', MODE='ai_assisted_selection';
const el=id=>document.getElementById(id);
const stable=x=>JSON.stringify(sortValue(x));
function sortValue(x){if(Array.isArray(x))return x.map(sortValue);if(x!==null&&typeof x==='object')return Object.fromEntries(Object.keys(x).sort().map(k=>[k,sortValue(x[k])]));return x;}
const blank=d=>({schema:SCHEMA,review_mode:MODE,independent_human_gold:false,article_id:d.row.article_id,ticker:d.row.ticker,input_sha256:d.input_sha256,ai_assessment:d.ai_assessment,ai_assessment_sha256:d.ai_assessment_sha256,human_selection:null,human_selected_at:null,prior_annotation:null});
let answers=DATA.map(blank),index=0,history=[];
const selections=['채택','제외','보류'];
function identity(a,d){if(a.article_id!==d.row.article_id||a.ticker!==d.row.ticker||a.input_sha256!==d.input_sha256)throw Error('원문 기사·기업·해시가 현재 자료와 다릅니다.');}
const legacyKeys=['article_id','ticker','input_sha256','human_selection','human_label','human_time_status','human_evidence_quotes','human_reason','reviewed_by','human_reviewed','completed_at'].sort();
function checkLegacy(a,d){if(!a||stable(Object.keys(a).sort())!==stable(legacyKeys))throw Error('이전 검수 파일의 필드가 다릅니다.');identity(a,d);
if(typeof a.human_reviewed!=='boolean'||typeof a.human_reason!=='string'||typeof a.reviewed_by!=='string')throw Error('이전 검수 형식 오류');
if(!Array.isArray(a.human_evidence_quotes)||a.human_evidence_quotes.some(q=>typeof q!=='string'||!q||![d.row.title,d.row.body].some(t=>t.includes(q))))throw Error('이전 검수 인용이 원문과 다릅니다.');
const choices={human_selection:selections,human_label:['직접관련','간접관련','단순언급','판단보류'],human_time_status:['신규','과거','불명']};
for(const [k,v] of Object.entries(choices))if(a[k]!==null&&!v.includes(a[k]))throw Error('이전 검수 선택값 오류');
if(a.human_reviewed){if(Object.entries(choices).some(([k,v])=>!v.includes(a[k]))||!a.human_reason.trim()||!a.reviewed_by.trim())throw Error('이전 완료 검수 필수값 누락');
if(['채택','제외'].includes(a.human_selection)&&!a.human_evidence_quotes.length)throw Error('이전 완료 검수 원문 인용 누락');
if(typeof a.completed_at!=='string'||!a.completed_at)throw Error('이전 완료 검수 시각 오류');}else if(a.completed_at!==null)throw Error('미완료 검수의 시각 오류');}
function check(a,d){if(!a||stable(Object.keys(a).sort())!==stable(Object.keys(blank(d)).sort()))throw Error('간단 선택 파일의 필드가 다릅니다.');identity(a,d);
if(a.schema!==SCHEMA||a.review_mode!==MODE||a.independent_human_gold!==false)throw Error('AI 보조 선택 형식이 아닙니다.');
if(a.ai_assessment_sha256!==d.ai_assessment_sha256||stable(a.ai_assessment)!==stable(d.ai_assessment))throw Error('AI 추천 내용·해시가 현재 자료와 다릅니다.');
if(a.human_selection!==null&&!selections.includes(a.human_selection))throw Error('선택값 오류');
if(a.human_selection===null?a.human_selected_at!==null:typeof a.human_selected_at!=='string'||!a.human_selected_at.trim())throw Error('선택과 시각이 맞지 않습니다.');
if(a.prior_annotation!==null)checkLegacy(a.prior_annotation,d);}
function render(){const d=DATA[index],a=answers[index],ai=d.ai_assessment,t=d.translation,complete=t&&t.status==='complete';
el('meta').textContent=`${d.row.company_name} (${d.row.ticker}) · ${d.row.published_at}`;el('headline').textContent=complete?t.title_ko:d.row.title;
el('recommendation').textContent='AI 추천: '+(ai.ai_selection==='보류'?'잘 모르겠음 (보류)':ai.ai_selection);el('reason').textContent=ai.ai_reason;
el('ai_meta').textContent=`${ai.ai_label} · ${ai.ai_time_status} · ${ai.ai_source} · 사람 정답 아님`;
el('evidence').replaceChildren();for(const quote of ai.ai_evidence_quotes){const node=document.createElement('blockquote');node.textContent=quote;el('evidence').appendChild(node);}if(!ai.ai_evidence_quotes.length)el('evidence').textContent='AI가 제시한 원문 근거가 없습니다.';
el('translated').hidden=!t;el('translated_title').textContent=complete?t.title_ko:'';el('translated_body').textContent=complete?t.body_ko:'';
el('translation_warning').textContent=t?[(complete?'':'번역 미완료: 원문으로 확인하세요.'),...t.warnings].filter(Boolean).join('\n'):'';
el('source_title').textContent=d.row.title;el('source_body').textContent=d.row.body;
for(const [id,value] of [['adopt','채택'],['exclude','제외'],['hold','보류']])el(id).setAttribute('aria-pressed',String(a.human_selection===value));
el('progress').textContent=`${index+1} / ${DATA.length} · 선택 ${answers.filter(x=>x.human_selection!==null).length}건`;
el('status').textContent=a.human_selection===null?'아직 선택하지 않았습니다.':`내 선택: ${a.human_selection==='보류'?'잘 모르겠음 (보류)':a.human_selection}`;
el('undo').disabled=!history.length;}
function choose(value){history.push({index,answer:JSON.parse(JSON.stringify(answers[index]))});answers[index].human_selection=value;answers[index].human_selected_at=value===null?null:new Date().toISOString();if(value!==null&&el('auto_next').checked)index=Math.min(DATA.length-1,index+1);render();}
el('adopt').onclick=()=>choose('채택');el('exclude').onclick=()=>choose('제외');el('hold').onclick=()=>choose('보류');el('clear').onclick=()=>choose(null);
el('undo').onclick=()=>{const prior=history.pop();if(prior){index=prior.index;answers[index]=prior.answer;render();}};
el('prev').onclick=()=>{index=Math.max(0,index-1);render();};el('next').onclick=()=>{index=Math.min(DATA.length-1,index+1);render();};
el('export').onclick=()=>{try{answers.forEach((a,i)=>check(a,DATA[i]));const blob=new Blob([answers.map(a=>JSON.stringify(a)).join('\n')+'\n'],{type:'application/x-ndjson'});
const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='cosmos-assisted-selection.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){el('status').textContent=e.message;}};
el('import').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;const rows=(await file.text()).split(/\r?\n/).filter(x=>x.trim()).map(JSON.parse),next=DATA.map(blank),seen=new Set();
for(const a of rows){if(!a||typeof a!=='object')throw Error('잘못된 파일 형식입니다.');const i=DATA.findIndex(d=>d.row.article_id===a.article_id&&d.row.ticker===a.ticker);if(i<0||seen.has(i))throw Error('중복 또는 다른 기사의 선택입니다.');
if(stable(Object.keys(a).sort())===stable(legacyKeys)){checkLegacy(a,DATA[i]);next[i].prior_annotation=a;if(a.human_reviewed){next[i].human_selection=a.human_selection;next[i].human_selected_at=a.completed_at;}}else{check(a,DATA[i]);next[i]=a;}seen.add(i);}
answers=next;history=[];render();}catch(e){el('status').textContent=e.message;}};
render();
</script></html>'''


def generate(inputs, out, translations, assessments):
    out = Path(out)
    if out.exists():
        raise FileExistsError("Do not overwrite a review form")
    data = assisted_packet(inputs, assessments, translations)
    payload = canonical(data).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf8") as stream:
        stream.write(HTML.replace("__DATA__", payload))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--translations", type=Path)
    parser.add_argument("--assessments", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path)
    action.add_argument("--validate", type=Path)
    args = parser.parse_args()
    if args.out:
        generate(args.inputs, args.out, args.translations, args.assessments)
        print(canonical({"form": str(args.out), "schema": SCHEMA, "human_selected": 0, "independent_human_gold": False}))
    else:
        print(canonical(validate_selections(assisted_packet(args.inputs, args.assessments, args.translations), records(args.validate))))
