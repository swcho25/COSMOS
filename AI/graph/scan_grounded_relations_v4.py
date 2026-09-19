"""Bounded recent-date v4 candidate scan; frozen v3 payloads remain unchanged.

This is a NEW, unapproved development artifact. The recent window is a literal
publication-calendar-date interval, not a claim of a rolling UTC duration. Only
parent B-include endpoints are normally considered. An explicitly supplied,
hashed independent endpoint audit may restore an individual relation endpoint;
its exact identity/relation quotations and original B result must still match.
This NEVER promotes the original relevance or sentiment label. All new relation
rows require independent contextual review before integration or training.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import time

import kg_grounded_relations_v4 as extractor
import kg_target_aliases as alias_policy
from expand_grounded_relations_v3 import canonical, digest, sha, load, readonly, check_span, endpoint_blocked


def restored_pair(proposal, article, registry):
    """Check literal, unambiguous identities without changing the source label."""
    assert proposal['article_id'] == article['article_id']
    assert proposal['parent_news_decision_unchanged'] is True and proposal['parent_sentiment_unchanged'] is True
    raw = article['title'] + '\n' + article['body']
    for key in ('exact_identity_proof', 'relation_support'):
        ev = proposal[key]
        check_span(raw, ev['start'], ev['end'], ev['quote'])
    allowed = {x.casefold() for x in registry.get(proposal['ticker'], ())}
    assert proposal['aliases'] and all(x.casefold() in allowed for x in proposal['aliases'])
    assert proposal['upstream_decision'] in ('needs_review', 'review')
    historical = proposal['historical_identity']
    assert isinstance(historical, dict) and historical
    assert historical['issuer_stock_code'] == proposal['ticker'] and historical['issuer_corp_code']
    before = dt.datetime.fromisoformat(historical['published_at'].replace('Z','+00:00'))
    public = dt.datetime.fromisoformat(article['published_at'].replace('Z','+00:00'))
    assert before.tzinfo is not None and public.tzinfo is not None and before < public
    return {'article_id': article['article_id'], 'ticker': proposal['ticker'], 'company_name': proposal['company_name'],
            'source_input_sha256': proposal['source_input_sha256'], 'upstream_result_sha256': proposal['upstream_result_sha256'],
            'upstream_decision': proposal['upstream_decision'], 'materialization_decision': 'not_materialized_upstream_review',
            'reasons': ['explicit_relation_only_endpoint_restoration'], 'rejected_mentions': [], 'unresolved_mentions': [],
            'target_context': {'identity_scope': 'literal_canonical_or_safe_alias_with_dated_audit', 'historical_resolution': historical}}


def run(baseline, identity_revision, out, start, end, endpoint_audit=None):
    baseline, identity_revision, out = baseline.resolve(), identity_revision.resolve(), out.resolve()
    assert start < end
    out.mkdir(parents=True, exist_ok=False)
    begun = time.monotonic()
    frozen = load(baseline/'manifest.json'); old_protocol = load(identity_revision/'protocol.json')
    assert frozen['artifact_complete'] is True
    parent = Path(frozen['parent_kg']['directory']).resolve()
    assert sha(parent/'manifest.json') == frozen['parent_kg']['manifest_sha256']
    assert frozen['parent_kg'] == old_protocol['parent_kg']
    parent_protocol = load(parent/'protocol.json')
    assert sha(parent/'protocol.json') == frozen['parent_kg']['protocol_sha256']
    binding_paths = {'runner':Path(__file__), 'v4_extractor':Path(extractor.__file__),
                     'v3_extractor':Path(extractor.base.__file__), 'shared_checks':Path(__file__).with_name('expand_grounded_relations_v3.py'),
                     'alias_policy':Path(alias_policy.__file__), 'alias_dictionary':alias_policy.ALIAS_FILE,
                     'baseline_manifest':baseline/'manifest.json','identity_protocol':identity_revision/'protocol.json',
                     'parent_manifest':parent/'manifest.json'}
    if endpoint_audit: binding_paths['endpoint_audit'] = endpoint_audit.resolve()
    bindings = {name:sha(path) for name,path in binding_paths.items()}
    registry = alias_policy._read(str(alias_policy.ALIAS_FILE), bindings['alias_dictionary'])
    spec = frozen['files']['relationship_evidence']; base_path = baseline/spec['path']
    assert sha(base_path) == spec['sha256']
    originals = [json.loads(x) for x in base_path.read_text(encoding='utf8').splitlines() if x.strip()]
    assert len(originals) == spec['rows']
    merged = {r['edge_id']:r for r in originals}; assert len(merged) == len(originals)
    proposals = defaultdict(list)
    original_b = None
    if endpoint_audit:
        audit = load(endpoint_audit)
        assert audit['schema'] == 'cosmos-recent-relation-endpoint-audit-1'
        assert audit['artifact_complete'] is True and audit['parent_manifest_sha256'] == bindings['parent_manifest']
        assert audit['parent_protocol_sha256'] == frozen['parent_kg']['protocol_sha256']
        assert audit['alias_dictionary_sha256'] == bindings['alias_dictionary']
        for row in audit['safe_endpoint_restorations']: proposals[row['article_id']].append(row)
        b_dir = Path(parent_protocol['source']['directory'])
        binding_paths['original_b_receipt'] = b_dir/'partial_receipt.json'
        bindings['original_b_receipt'] = sha(binding_paths['original_b_receipt'])
        assert bindings['original_b_receipt'] == parent_protocol['source']['receipt_sha256']
        assert audit['merged_receipt_sha256'] == bindings['original_b_receipt']
        original_b = readonly((b_dir/'state.sqlite3').resolve())
    con = readonly(parent/'state.sqlite3')
    window_ids = [r[0] for r in con.execute("SELECT article_id FROM articles WHERE substr(json_extract(payload,'$.published_at'),1,10)>=? AND substr(json_extract(payload,'$.published_at'),1,10)<? ORDER BY article_id", (start,end))]
    assert set(proposals) <= set(window_ids), 'Audited restoration outside this bounded window'
    blocked = {(x['article_id'],x['ticker']) for x in old_protocol['endpoint_policy']['article_companies']}
    counts, reviews, new = Counter(), [], []
    flags = {'graph_quality_validated':False,'human_gold':False,'ready_for_gnn_training':False,
             'production_approved':False,'price_effect_established':False,'contextual_review_required':True}
    for i, aid in enumerate(window_ids, 1):
        pair_rows = con.execute('SELECT ticker,payload FROM pairs WHERE article_id=? ORDER BY ticker',(aid,)).fetchall()
        if len(pair_rows)+len(proposals.get(aid,())) < 2: continue
        counts['multi_endpoint_articles_considered'] += 1
        source_sha, cleaned_sha, raw_article = con.execute('SELECT source_sha,cleaned_sha,payload FROM articles WHERE article_id=?',(aid,)).fetchone()
        article = json.loads(raw_article)
        original = {k:article[k] for k in ('article_id','title','body','published_at','source_url')}
        assert source_sha == digest(original)
        assert cleaned_sha == digest({'title':article['cleaned_title'],'body':article['cleaned_body']})
        if not article['date_audit']['usable_day'] or article['cleaning_audit']['quarantine']:
            counts['date_or_article_boundary_blocked'] += 1; continue
        raw = article['title']+'\n'+article['body']; cleaned = ' '.join((article['cleaned_title']+'\n'+article['cleaned_body']).split())
        pairs, entities, restorations = {}, [], {}
        for ticker, raw_pair in pair_rows:
            pair = json.loads(raw_pair)
            assert pair['upstream_decision']=='include' and pair['article_id']==aid
            assert pair['source_input_sha256'] == digest({**original,'ticker':ticker,'company_name':pair['company_name']})
            if (aid,ticker) in blocked: continue
            pairs[ticker] = pair
            entities.append({'ticker':ticker,'company_name':pair['company_name'],'aliases':registry.get(ticker,())})
        for proposal in proposals.get(aid,()):
            assert proposal['source_article_sha256'] == source_sha
            ticker = proposal['ticker']; assert ticker not in pairs and (aid,ticker) not in blocked
            pair = restored_pair(proposal,article,registry)
            assert pair['source_input_sha256'] == digest({**original,'ticker':ticker,'company_name':pair['company_name']})
            found = original_b.execute('SELECT input_sha256,decision,result_sha256,result_json,input_json FROM pairs WHERE article_id=? AND ticker=?',(aid,ticker)).fetchone()
            assert found is not None and found[:3] == (proposal['source_input_sha256'],proposal['upstream_decision'],proposal['upstream_result_sha256'])
            assert digest(json.loads(found[4])) == found[0]
            assert hashlib.sha256(found[3].encode('utf8')).hexdigest() == found[2]
            pairs[ticker]=pair; restorations[ticker]=proposal
            entities.append({'ticker':ticker,'company_name':pair['company_name'],'aliases':proposal['aliases']})
            counts['relation_only_endpoint_restorations'] += 1
        result = extractor.extract_grounded_relations(article['title'],article['body'],entities)
        counts.update('review:'+r['reason'] for r in result['review_candidates'])
        for edge in result['relationships']:
            check_span(raw,edge['sentence_start'],edge['sentence_end'],edge['sentence'])
            normalized = ' '.join(edge['sentence'].split())
            edge_id = 'relationship:'+digest([aid,edge['src_ticker'],edge['dst_ticker'],edge['rel_type'],normalized])
            if edge_id in merged: continue
            endpoint_validation, reasons = {}, []
            for prefix,ticker in [('source',edge['src_ticker']),('target',edge['dst_ticker'])]:
                pair = pairs[ticker]
                check_span(raw,edge[prefix+'_start'],edge[prefix+'_end'],edge[prefix+'_quote'])
                assert edge['sentence_start'] <= edge[prefix+'_start'] < edge[prefix+'_end'] <= edge['sentence_end']
                reason = endpoint_blocked(pair,prefix,edge,article)
                if reason: reasons.append(reason)
                if ticker in restorations:
                    support = restorations[ticker]['relation_support']
                    assert (support['src_ticker'],support['dst_ticker'],support['rel_type']) == (edge['src_ticker'],edge['dst_ticker'],edge['rel_type'])
                    assert (support['start'],support['end'],support['quote']) == (edge['sentence_start'],edge['sentence_end'],edge['sentence'])
                endpoint_validation[prefix] = {'ticker':ticker,'source_input_sha256':pair['source_input_sha256'],
                    'upstream_result_sha256':pair['upstream_result_sha256'],'parent_decision':pair['materialization_decision'],
                    'parent_reasons':pair['reasons'],'parent_news_decision_unchanged':True,'parent_sentiment_unchanged':True,
                    'identity_scope':pair['target_context']['identity_scope'],'historical_resolution':pair['target_context']['historical_resolution'],
                    'alias_dictionary_sha256':bindings['alias_dictionary'],
                    'relation_role_proof':'explicit_literal_grammatical_relation',
                    'restoration_audit_sha256':bindings.get('endpoint_audit') if ticker in restorations else None}
            if normalized not in cleaned: reasons.append('relation_sentence_not_retained_in_cleaned_article')
            if reasons:
                reviews.append({'article_id':aid,'edge':edge,'reasons':reasons});counts.update('blocked:'+r for r in reasons);continue
            payload = {**edge,'edge_id':edge_id,'article_id':aid,'news_node':'news:'+aid,
                       'source_node':'company:'+edge['src_ticker'],'target_node':'company:'+edge['dst_ticker'],
                       'published_at':article['published_at'],'source_url':article['source_url'],
                       'source_article_sha256':source_sha,'source_cleaned_sha256':cleaned_sha,
                       'endpoint_validation':endpoint_validation,'event_time':None,'event_time_status':'not_extracted',
                       'offset_basis':'original_title_newline_body','duplicate_evidence_spans':[],
                       'scope':'recent_additive_v4_development_candidates','extraction_version':extractor.POLICY_VERSION,**flags}
            new.append(payload);merged[edge_id]=payload;counts['new:'+edge['rel_type']] += 1
        if i%1000==0: print(canonical({'window_articles_processed':i,'window_articles':len(window_ids),'new_edges':len(new)}),flush=True)
    con.close()
    if original_b is not None: original_b.close()
    for name,path in binding_paths.items(): assert sha(path)==bindings[name], 'Input changed: '+name
    grouped = defaultdict(list)
    for edge in merged.values():grouped[edge['src_ticker'],edge['dst_ticker'],edge['rel_type'],edge['directed']].append(edge)
    aggregates = [{'src_ticker':s,'dst_ticker':t,'source_node':'company:'+s,'target_node':'company:'+t,'rel_type':k,'directed':d,
                   'evidence_count':len(g),'news_count':len({x['article_id'] for x in g}),
                   'evidence_count_unit':'article_specific_normalized_statements_not_independent_events',**flags}
                  for (s,t,k,d),g in sorted(grouped.items())]
    files = {}
    for name, values in [('relationship_evidence',sorted(merged.values(),key=lambda r:r['edge_id'])),
                         ('new_relationship_evidence',new),('company_relationships',aggregates),('blocked_candidates',reviews)]:
        path=out/(name+'.jsonl')
        with path.open('x',encoding='utf8',newline='\n') as stream:
            for row in values:stream.write(canonical(row)+'\n')
        files[name]={'path':path.name,'rows':len(values),'bytes':path.stat().st_size,'sha256':sha(path)}
    snapshots=out/'source_snapshots';snapshots.mkdir()
    for name,path in binding_paths.items():
        if path.suffix=='.py':(snapshots/path.name).write_bytes(path.read_bytes())
    manifest={'schema':'cosmos-grounded-relations-v4-additive-candidates-1','artifact_complete':True,'full_population_complete':False,
              'scope':'recent_additive_v4_development_candidates','parent_kg':frozen['parent_kg'],'files':files,
              'bindings':bindings,'baseline_directory':str(baseline),'baseline_evidence':len(originals),'new_evidence':len(new),
              'combined_evidence':len(merged),'window':{'start_inclusive':start,'end_exclusive':end,'basis':'literal_published_at_calendar_date'},
              'window_articles':len(window_ids),'counts':dict(counts),'elapsed_seconds':time.monotonic()-begun,**flags,
              'limitations':['No accuracy/recall estimate; purposive development grammar.',
                             'Baseline payloads preserved, including prior flags requiring separate review policy.',
                             'New candidates require independent whole-article review before integration.',
                             'Restoration is relation-only and restricted to the separately hashed endpoint audit.']}
    (out/'manifest.json').write_text(canonical(manifest)+'\n',encoding='utf8')
    print(canonical(manifest),flush=True);return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('baseline','identity-revision','out'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--start',default='2026-06-18');p.add_argument('--end',default='2026-09-16')
    p.add_argument('--endpoint-audit',type=Path)
    args=p.parse_args();run(args.baseline,args.identity_revision,args.out,args.start,args.end,args.endpoint_audit)
