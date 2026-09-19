"""Compare a frozen rule gate with independent AI semantic review, without tuning.

The review is a qualitative proxy on 60 previously sampled, stratified training
articles, covering 78 original weak article/ticker proposals. It is neither human
gold nor a general precision/recall estimate. Human review stays pending. Current
events outside the implemented action templates (management departures, fines,
etc.) are kept as current events in the comparison, never recoded as event absence.
All review/gate quotes are checked against source strings. Only local text/review
artifacts and the rule gate are read; no prices, return labels or model outputs.
"""

import argparse
import collections
import hashlib
import json
from pathlib import Path

from current_event_gate import evaluate_article, LEAD_BODY_CHARS, RULE_VERSION


HERE = Path(__file__).resolve().parent
QUALITY = HERE / 'artifacts/news_quality_dataset_20260915_v2'
REVIEW = HERE / 'artifacts/current_event_review_20260915/ai_review.json'
SUPPORTED = 'supported_current_event'
DECISIONS = (SUPPORTED, 'not_supported_current_event', 'uncertain')
# Broad semantic categories that the reviewer considers current, but for which
# v1 does not have a corresponding action template. This is interpretation of
# documented coverage, not a modification of gate decisions or review records.
OUTSIDE_TEMPLATES = {
    'executive_departure', 'regulatory_fine', 'capital_expenditure',
    'wage_policy_change', 'contract_termination', 'scheduled_merger_vote',
    'merger_labor_opposition', 'customer_spend_withdrawal',
    'equity_disposal_agreement', 'stock_split_dividend',
    'earnings_guidance_cut', 'earnings_preannouncement',
}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    with Path(path).open('x', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')


def quote_in_full(review, article):
    field = review['quote_field']
    assert field in ('title', 'body'), field
    start, end = review['quote_start'], review['quote_end']
    source = article[field]
    assert isinstance(start, int) and isinstance(end, int)
    assert 0 <= start < end <= len(source)
    assert source[start:end] == review['quote']
    offset = len(article['title']) + 1 if field == 'body' else 0
    return dict(start=start + offset, end=end + offset, quote=review['quote'],
                source_field=field, source_start=start, source_end=end)


def audit_gate_spans(gate, article):
    full = article['title'] + '\n' + article['body']
    count = 0
    for decision in gate['decisions']:
        for evidence in decision['evidence'] + decision['excluded_evidence']:
            assert full[evidence['start']:evidence['end']] == evidence['quote']
            assert evidence['start'] <= evidence['mention_start'] < evidence['mention_end'] <= evidence['end']
            if 'event_start' in evidence:
                assert full[evidence['event_start']:evidence['event_end']] == evidence['event_quote']
            count += 1
    return count


def markdown(report):
    s = report['summary']
    lines = [
        '# 동결 사건 규칙과 독립 AI 검토 비교', '',
        '사람 정답이 아닌 AI 보조 의미 검토와의 비교다. 학습 기간에서 이미 뽑은 60기사의 '
        '기존 자동 제안 78쌍만 대상으로 하며 전체 데이터의 정밀도·재현율 또는 모델 예측력을 뜻하지 않는다. '
        '검토자는 규칙 구현·가격 정답·모델 예측을 읽지 않았다고 기록했다. '
        '이 감사 실행 도중에는 규칙을 수정하지 않았다.', '',
        ('이 표본은 규칙 수정에 사용된 개발 자료다. 수정한 규칙의 독립 검증으로 소개하지 않는다.'
         if report['protocol']['review_role'] == 'development' else
         '이 비교는 해당 규칙 버전 수정에 사용되지 않은 별도 AI 검토 자료를 대상으로 한다.'), '',
        '| 독립 AI 의미 판단 | 규칙 후보 | 규칙 보류 |', '|---|---:|---:|',
    ]
    labels = {SUPPORTED: '현재 사건 지지', 'not_supported_current_event': '현재 사건 미지지', 'uncertain': '불확실'}
    for status in DECISIONS:
        lines.append(f"| {labels[status]} | {s['confusion_counts'][status]['candidate']} | {s['confusion_counts'][status]['abstain']} |")
    lines += ['',
        f"전체 60기사 중 규칙이 한 기업이라도 유지한 기사는 {s['gate_candidate_articles']}건이다. "
        f"기존 검토 78쌍 중 {s['reviewed_gate_candidate_pairs']}쌍을 유지했고, "
        f"검토 대상 밖에서 새로 선택한 기업은 {s['unreviewed_gate_candidate_pairs']}쌍이다.", '',
        f"AI가 현재 사건으로 지지했지만 규칙이 보류한 {s['supported_current_but_abstained']}쌍에는 "
        f"미구현 사건 표현 {s['supported_abstention_outside_templates']}쌍, "
        f"검토 인용문이 본문 첫 {LEAD_BODY_CHARS}자 뒤에 시작하는 {s['supported_abstention_review_quote_beyond_lead']}쌍이 있다. "
        '두 수치는 겹칠 수 있으며 보류 원인이 하나로 확정됐다는 뜻이 아니다. 임원 퇴사·최근 벌금을 '
        '지원하지 못한 경우에도 현재 사건 부재로 바꾸어 세지 않았다.', '',
        '## 충돌과 보류 근거', '',
        '아래는 AI 검토와 규칙이 다르거나, AI가 불확실하다고 판단한 사례다. '
        '원문 전체 좌표와 모든 규칙 진단은 gate_review_comparison.json에 있다.', '',
    ]
    for pair in report['comparisons']:
        if pair['comparison'] in ('supported_current_candidate', 'not_supported_abstained'):
            continue
        lines += [
            f"### 검토 {pair['review_id']} · {pair['ticker']} · {pair['comparison']}", '',
            f"AI: {labels[pair['review_decision']]} / {pair['observed_event_type']}; 규칙: {pair['gate_status']}.", '',
            '> ' + pair['review_quote']['quote'].replace('\n', ' '), '',
            pair['review_explanation'], '',
            '규칙 사유: ' + ', '.join(pair['gate_reasons']) + '.', '',
        ]
        if pair['gate_evidence']:
            lines += ['규칙이 유지한 별도 근거:', '', '> ' + pair['gate_evidence'][0]['quote'].replace('\n', ' '), '']
        if pair['current_event_outside_templates']:
            lines += ['이 현재 사건 유형의 직접 동작 표현은 v1 규칙의 구현 범위 밖이다.', '']
        if pair['review_quote_beyond_lead']:
            lines += [f'AI 검토 인용문의 시작은 본문 첫 {LEAD_BODY_CHARS}자 뒤다.', '']
    lines += ['사람 검수 완료 0건을 유지한다. 이 비교를 바탕으로 현재 학습 실행의 규칙·검증 모집단을 바꾸지 않는다.', '']
    return '\n'.join(lines)


def run(review_path, quality, out, review_role='development'):
    assert not out.exists(), out
    paths = [review_path, quality / 'human_review.json', quality / 'training_candidates_clean.jsonl',
             HERE / 'current_event_gate.py', Path(__file__).resolve()]
    hashes = {str(p.resolve()): sha(p) for p in paths}
    review = json.loads(review_path.read_text(encoding='utf8'))
    # V2 was explicitly corrected after reading this original review. A reviewer
    # having been blind to v1 does not make the same sample independent for v2.
    if RULE_VERSION != 'current-event-subject-v1' and review_path.resolve() == REVIEW.resolve():
        assert review_role == 'development', 'Original 60-article review was used to develop gate v2'
    assert review['human_gold'] is False and review['human_review_completed'] == 0
    assert review['outcome_data_accessed'] is False and review['gate_implementation_inspected'] is False
    records = review['records']
    human = json.loads((quality / 'human_review.json').read_text(encoding='utf8'))
    sampled = {r['record_id']: r for r in human if r['stratum'] == 'retained'}
    assert len(records) == len(sampled) == 60
    assert {r['record_id'] for r in records} == set(sampled)
    for name in ('human_review.json', 'training_candidates_clean.jsonl'):
        assert review['source_files'][name]['sha256'] == sha(quality / name)
    articles = {}
    with (quality / 'training_candidates_clean.jsonl').open(encoding='utf8') as f:
        for line in f:
            row = json.loads(line)
            if row['record_id'] in sampled:
                articles[row['record_id']] = row
    assert set(articles) == set(sampled)
    comparisons, article_reports, unreviewed = [], [], []
    confusion = {status: dict(candidate=0, abstain=0) for status in DECISIONS}
    reason_counts, strata = collections.Counter(), collections.defaultdict(collections.Counter)
    quote_count = 0
    for record in sorted(records, key=lambda r: r['review_id']):
        article = articles[record['record_id']]
        assert article['split'] == 'train' and '2017-01-01' <= article['published_date'] <= '2022-12-31'
        assert record['review_id'] == sampled[record['record_id']]['review_id']
        reviews = record['ticker_reviews']
        expected_tickers = {e['ticker'] for e in sampled[record['record_id']]['evidence']}
        assert {r['ticker'] for r in reviews} == expected_tickers
        assert len(reviews) == len(expected_tickers)
        gate = evaluate_article(article)
        quote_count += audit_gate_spans(gate, article)
        by_ticker = {d['ticker']: d for d in gate['decisions']}
        for item in reviews:
            ticker, decision = item['ticker'], item['decision']
            assert decision in DECISIONS
            gd = by_ticker[ticker]
            semantic_quote = quote_in_full(item, article)
            status = gd['status']
            confusion[decision][status] += 1
            if decision == SUPPORTED:
                comparison = 'supported_current_candidate' if status == 'candidate' else 'supported_current_abstained'
            elif decision == 'not_supported_current_event':
                comparison = 'not_supported_candidate_conflict' if status == 'candidate' else 'not_supported_abstained'
            else:
                comparison = 'uncertain_candidate' if status == 'candidate' else 'uncertain_abstained'
            late = item['quote_field'] == 'body' and item['quote_start'] > LEAD_BODY_CHARS
            pair = dict(review_id=record['review_id'], record_id=record['record_id'], title=article['title'],
                ticker=ticker, review_decision=decision, observed_event_type=item['observed_event_type'],
                review_quote=semantic_quote, review_explanation=item['explanation'], review_issue_tags=item['issue_tags'],
                automatic_evidence_assessment=item['automatic_evidence_assessment'],
                gate_status=status, gate_reasons=gd['reasons'], gate_evidence=gd['evidence'],
                gate_excluded_evidence=gd['excluded_evidence'], comparison=comparison,
                current_event_outside_templates=decision == SUPPORTED and item['observed_event_type'] in OUTSIDE_TEMPLATES,
                review_quote_beyond_lead=late, human_review_status='pending')
            comparisons.append(pair)
            if status == 'abstain':
                reason_counts.update(gd['reasons'])
            strata[(record['region'], record['year'])][comparison] += 1
        for ticker in set(gate['accepted_tickers']) - expected_tickers:
            unreviewed.append(dict(review_id=record['review_id'], record_id=record['record_id'],
                ticker=ticker, reason='new_gate_subject_not_in_original_78_pair_semantic_review', gate_decision=by_ticker[ticker]))
        article_reports.append(dict(review_id=record['review_id'], record_id=record['record_id'],
            region=record['region'], year=record['year'], title=article['title'],
            ai_supported_tickers=[i['ticker'] for i in reviews if i['decision'] == SUPPORTED],
            ai_uncertain_tickers=[i['ticker'] for i in reviews if i['decision'] == 'uncertain'],
            gate_accepted_tickers=gate['accepted_tickers'], human_review_status='pending'))
    assert len(comparisons) == 78
    abstentions = [p for p in comparisons if p['comparison'] == 'supported_current_abstained']
    summary = dict(articles=60, reviewed_pairs=78, confusion_counts=confusion,
        gate_candidate_articles=sum(bool(a['gate_accepted_tickers']) for a in article_reports),
        reviewed_gate_candidate_pairs=sum(p['gate_status'] == 'candidate' for p in comparisons),
        unreviewed_gate_candidate_pairs=len(unreviewed), supported_current_but_abstained=len(abstentions),
        supported_abstention_outside_templates=sum(p['current_event_outside_templates'] for p in abstentions),
        supported_abstention_review_quote_beyond_lead=sum(p['review_quote_beyond_lead'] for p in abstentions),
        abstention_reason_pair_counts=dict(reason_counts),
        strata=[dict(region=r, year=y, counts=dict(c)) for (r, y), c in sorted(strata.items())])
    for path, digest in hashes.items():
        assert sha(path) == digest, path
    report = dict(protocol=dict(rule_version=RULE_VERSION, gate_unchanged_during_audit=True,
        review_role=review_role, independent_ai_semantic_proxy=review_role == 'independent',
        ai_semantic_proxy=True, human_gold=False, human_review_completed=0,
        general_precision_or_recall_claim=None, accuracy_improvement_claim=None,
        data_scope='60 stratified retained train articles, 78 original article/ticker proposals',
        supported_scope_mismatch='Review includes events outside gate templates; current-event abstentions remain current in review',
        uncertainty_policy='uncertain is a separate third row, never coerced to negative',
        lead_interpretation='Review quote start beyond 2400 is a diagnostic; it does not prove no equivalent evidence earlier'),
        source_hashes=hashes, summary=summary, articles=article_reports, comparisons=comparisons,
        unreviewed_gate_candidates=unreviewed,
        verification=dict(review_quotes_exact=len(comparisons), gate_diagnostic_spans_exact=quote_count,
            all_60_retained_articles_covered=True, all_78_proposed_pairs_covered=True,
            gate_and_input_hashes_unchanged=True, source_human_review_unmodified=True))
    out.mkdir(parents=True)
    dump(out / 'gate_review_comparison.json', report)
    (out / 'GATE_REVIEW_AUDIT.md').write_text(markdown(report), encoding='utf8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--review', type=Path, default=REVIEW)
    parser.add_argument('--quality', type=Path, default=QUALITY)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--review-role', choices=('development', 'independent'), default='development')
    args = parser.parse_args()
    run(args.review.resolve(), args.quality.resolve(), args.out.resolve(), args.review_role)
