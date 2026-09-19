"""Read-only API/DB reconciliation for the selected grounded service version.

Run on the service host after staging and activation in the review database,
then repeat against the live application after the approved switch. No users,
authentication tokens or Redis data are accessed or created.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RUN = 'lake-grounded-20260919-v1'
SNAPSHOT = 'a70eb366-22cd-59de-88a3-fc7bceac09ca'


def require(value, message):
    if not value:
        raise RuntimeError(message)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database', required=True, choices=['cosmos', 'cosmos_grounded_review_20260919'])
    p.add_argument('--port', type=int, required=True, choices=[18081, 18082, 18083])
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    timings, checks = [], []

    def get(path, status=200):
        start = time.monotonic()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}{path}', timeout=60) as r:
                code, content = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, content = e.code, e.read()
        timings.append({'path': path, 'seconds': round(time.monotonic()-start, 3), 'status': code})
        require(code == status, f'HTTP {code}, expected {status}: {path}: {content[:200]!r}')
        result = json.loads(content)
        return result.get('data', result) if isinstance(result, dict) else result

    def db(sql):
        result = subprocess.run(['docker','exec','-i','cosmos-postgres-1','psql','-X','-qAt',
            '--set=ON_ERROR_STOP=1','-U','cosmos','-d',args.database],
            input='BEGIN READ ONLY; SET LOCAL timezone=\'UTC\';\n'+sql+'\nROLLBACK;\n',
            text=True, capture_output=True, check=True)
        return json.loads(result.stdout.strip()) if result.stdout.strip() else None

    metadata = get('/api/analysis-runs/active')
    require(metadata['runId'] == RUN and metadata['snapshotId'] == SNAPSHOT, 'Wrong active version API')
    graph = get('/api/graphs/latest')
    require(graph['snapshotId'] == SNAPSHOT, 'Graph snapshot differs from metadata')
    require(graph['nextRefreshAt'] is None, 'Static batch incorrectly promises automatic refresh')
    require(len(graph['nodes']) == 202 and len(graph['edges']) == 9, 'Default 30D graph counts differ')
    checks.append('metadata/default graph: same selected run, 202 nodes, 9 30D edges')

    scores = db("SELECT json_agg(s) FROM (SELECT * FROM cosmos_analysis.current_relationship_score_current ORDER BY window_type,relationship_id) s;")
    require(len(scores) == 67, 'Wrong current score count')
    expected = db("""SELECT coalesce(json_agg(s),'[]'::json) FROM (
      SELECT re.relationship_id,re.relationship_evidence_id AS evidence_id,
             re.document_id,d.document_type AS source_type,de.sentence_text,
             ec.available_at,ec.source_revision_id
      FROM cosmos_analysis.current_relationship_evidence re
      JOIN cosmos_analysis.current_document_evidence de USING(evidence_id)
      JOIN cosmos_analysis.current_source_document d ON d.document_id=re.document_id
      JOIN cosmos_analysis.current_evidence_context ec ON ec.evidence_id=de.evidence_id
    ) s;""")
    from datetime import datetime, timedelta
    asof = datetime.fromisoformat('2026-09-16T00:00:00+00:00')
    source_types = set()
    for score_index, score in enumerate(scores):
        relation, window = score['relationship_id'], score['window_type']
        detail = get(f'/api/relationships/{relation}?window={window}')
        for api_key, db_key in [('score','score'),('newsScore','news_score'),('disclosureScore','disclosure_score')]:
            require(detail[api_key] == score[db_key], f'{relation}/{window}: {api_key} differs')
        require(detail['confidence'] is None, 'Uncalibrated support became confidence')
        require(detail['explanationScope'] == 'HISTORICAL' and bool(detail['explanation']), 'Grounded relationship explanation missing')
        start = asof-timedelta(days=int(window[:-1]))
        wanted = {r['evidence_id']:r for r in expected if r['relationship_id'] == relation
                  and r['available_at'] and start <= datetime.fromisoformat(r['available_at']) < asof}
        received, cursor = {}, None
        while True:
            route = f'/api/relationships/{relation}/evidence?window={window}&size=20'
            if cursor:
                route += '&cursor='+urllib.parse.quote(cursor, safe='')
            page = get(route)
            for item in page['items']:
                key = item['evidenceId']
                require(key not in received, 'Duplicate evidence cursor result')
                require(key in wanted, 'Out-of-window or wrong-run evidence')
                require(item['evidenceSentence'] == wanted[key]['sentence_text'], 'Evidence quotation changed')
                require(item['documentId'] == wanted[key]['document_id'], 'Evidence document identity changed')
                require(item['sourceRevisionId'] == wanted[key]['source_revision_id'], 'Evidence original revision lost')
                require(item['confidence'] is None, 'Uncalibrated evidence confidence displayed')
                received[key] = item
                source_types.add(item['sourceType'])
            if not page['hasNext']:
                break
            require(page['nextCursor'] and page['nextCursor'] != cursor, 'Evidence cursor stalled')
            cursor = page['nextCursor']
        require(set(received) == set(wanted), f'Missing relationship evidence {relation}/{window}')
        if (score_index + 1) % 10 == 0:
            print(json.dumps({'phase':'relationship API comparison','checked':score_index+1,
                              'total':len(scores)}),flush=True)
    checks.append('all 67 relation/window responses and all paginated evidence match DB; exact UTC windows')
    require({'NEWS','DISCLOSURE'} <= source_types, 'News or disclosure evidence never returned')
    args.output.with_suffix('.relations.json').write_text(json.dumps({
        'status':'RELATION_PHASE_PASS','database':args.database,'run_id':RUN,
        'relation_windows':len(scores),'checks':checks,'requests':timings,
        'source_types':sorted(source_types)},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    sample = db("""SELECT json_agg(x) FROM (
      (SELECT DISTINCT ON (cd.sentiment) cd.document_id,cd.sentiment,cd.company_id,d.published_at,dc.published_at_raw
       FROM cosmos_analysis.current_company_document cd
       JOIN cosmos_analysis.current_source_document d USING(document_id)
       JOIN cosmos_analysis.current_document_context dc USING(document_id)
       WHERE d.document_type='NEWS' AND cd.is_service_visible=true
       ORDER BY cd.sentiment,cd.document_id)
      UNION ALL
      (SELECT cd.document_id,cd.sentiment,cd.company_id,d.published_at,dc.published_at_raw
       FROM cosmos_analysis.current_company_document cd
       JOIN cosmos_analysis.current_source_document d USING(document_id)
       JOIN cosmos_analysis.current_document_context dc USING(document_id)
       WHERE d.document_type='NEWS' AND cd.is_service_visible=true AND d.published_at IS NULL LIMIT 1)
    ) x;""")
    for item in sample:
        detail = get('/api/news/'+item['document_id'])
        require(bool(detail['evidence']), 'Selected news has no evidence')
        require(detail['sourceRevisionId'], 'News original revision missing')
        company = next(c for c in detail['relatedCompanies'] if c['companyId'] == item['company_id'])
        require(company['sentiment'] == item['sentiment'], 'Company sentiment differs')
        require(company['impactScore'] is None and company['relevanceScore'] is None, 'Uncomputed news impact/relevance fabricated')
        if item['published_at'] is None:
            require(detail['publishedAt'] is None and detail['publishedAtRaw'] == item['published_at_raw'], 'Unknown publication time was fabricated/lost')
        if item['sentiment']:
            page = get(f"/api/news?companyId={item['company_id']}&sentiment={item['sentiment']}&size=5")
            require(page['items'] and all(n['sentiment'] == item['sentiment'] for n in page['items']), 'Sentiment filter leaked other categories')
    checks.append('company sentiments including NULL, original quotations and unknown publication time preserved')
    page1 = get('/api/news?size=10')
    require(page1['nextCursor'], 'News pagination missing')
    page2 = get('/api/news?size=10&cursor='+urllib.parse.quote(page1['nextCursor'], safe=''))
    require(not ({n['newsId'] for n in page1['items']} & {n['newsId'] for n in page2['items']}), 'News pages overlap')
    legacy_id = db("""SELECT to_json(n.document_id) FROM public.news_article n
        WHERE NOT EXISTS(SELECT 1 FROM cosmos_analysis.current_news_article v WHERE v.document_id=n.document_id) LIMIT 1;""")
    if legacy_id:
        get('/api/news/'+legacy_id, status=404)
    checks.append('news cursor does not repeat records; unrelated live/legacy news cannot leak into active run')
    metrics = db("""SELECT json_agg(x) FROM (
        SELECT DISTINCT ON(window_type,(positive_count IS NULL)) *
        FROM cosmos_analysis.current_company_metric_history
        ORDER BY window_type,(positive_count IS NULL),company_id
    ) x;""")
    for metric in metrics:
        route = f"/api/companies/{metric['company_id']}/metrics?window={metric['window_type']}"
        value = get(route)
        for api_key, db_key in [('newsMentionCount','news_mention_count'),('positiveCount','positive_count'),
                               ('negativeCount','negative_count'),('sentimentScore','sentiment_score'),
                               ('relationshipCount','relationship_count')]:
            require(value[api_key] == metric[db_key], f'Metric {api_key} changed or fabricated')
    checks.append('all metric windows preserve known and NULL sentiment counts')
    result = {'status':'PASS','database':args.database,'port':args.port,'run_id':RUN,
              'checks':checks,'http_requests':len(timings),'returned_evidence_source_types':sorted(source_types),
              'maximum_request_seconds':max(t['seconds'] for t in timings),'requests':timings}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='requests'},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
