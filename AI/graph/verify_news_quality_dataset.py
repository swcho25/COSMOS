"""Audit full output invariants, holdout isolation, literal evidence and source hashes."""
import argparse
import json
from pathlib import Path
from news_quality import text_digest,audit_timestamp
from target_news_features import clean_target_article
from verify_news_predictive_value import sha,dump

HERE=Path(__file__).resolve().parent


def rows(path):
    with path.open(encoding='utf8') as f:
        for line in f:yield json.loads(line)


def fingerprints(r):
    return {text_digest(r.get('body')),text_digest(clean_target_article(r)[0].get('body'))}-{text_digest('')}


def main(out):
    protocol=json.loads((out/'protocol.json').read_text(encoding='utf8'));done=json.loads((out/'completion.json').read_text(encoding='utf8'))
    for path,digest in protocol['source_hashes'].items():assert sha(Path(path))==digest,path
    for name,digest in protocol['code_hashes'].items():assert sha(HERE/name)==digest,name
    original={r['record_id']:r for r in rows(HERE/'artifacts/news_impact_v2/articles.jsonl')};enriched=list(rows(out/'legacy_enriched.jsonl'))
    assert len(enriched)==len(original)==done['legacy_articles'];held_ids=set();held_bodies=set();held_urls=set();times=0
    old_ids=set();old_bodies=set();old_urls=set()
    source_candidates=[Path(p) for p in protocol['source_hashes'] if Path(p).name=='training_candidates.jsonl']
    assert len(source_candidates)==1
    raw_keys={r['record_id']:text_digest(r.get('body')) for r in rows(source_candidates[0])}
    for r in enriched:
        base={k:v for k,v in r.items() if k not in ('source_metadata','publication_time_audit')};assert base==original[r['record_id']]
        assert r['publication_time_audit']==audit_timestamp(r['source_metadata'])
        times+=r['publication_time_audit']['timestamp_status']=='explicit_source_time'
        if r['split'] in ('val','test'):
            held_ids.add(r['record_id']);held_bodies.update(fingerprints(r));u=r['source_metadata'].get('canonical_url')
            if u:held_urls.add(u)
        elif r['split']=='train':
            old_ids.add(r['record_id']);old_bodies.update(fingerprints(r));u=r['source_metadata'].get('canonical_url')
            if u:old_urls.add(u)
    ids=set();bodies=set();urls=set();evidence_count=0;count=0;new=0
    for r in rows(out/'training_candidates_clean.jsonl'):
        rid=r['record_id'];body_keys=fingerprints(r)|{raw_keys[rid]};body_keys.discard(text_digest(''));u=r.get('canonical_url')
        assert r['already_in_legacy_train']==(rid in old_ids or bool(body_keys & old_bodies) or bool(u and u in old_urls))
        assert r['split']=='train' and '2017'<=r['published_date'][:4]<='2022'
        assert rid not in held_ids and not body_keys & held_bodies and (not u or u not in held_urls)
        assert rid not in ids and not body_keys & bodies and (not u or u not in urls)
        assert r['label_status']=='unlabeled' and r['human_review_status']=='pending'
        assert r['publication_time_audit']==audit_timestamp(r)
        full=r['title']+'\n'+r['body'];hits=r['literal_mentions'];valid={h['ticker'] for h in hits};event_tickers=set()
        assert {c['ticker'] for c in r['companies']}==valid==set(r['verified_tickers'])
        assert valid<={c['ticker'] for c in r['source_companies']}
        for c in r['companies']:assert c['n_mentions']==sum(h['ticker']==c['ticker'] for h in hits)
        for e in r['event_evidence']:
            assert e['quote']==full[e['start']:e['end']]
            assert any(h['ticker']==e['ticker'] and e['start']<=h['start']<h['end']<=e['end'] for h in hits)
            assert e['review_status']=='pending';event_tickers.add(e['ticker']);evidence_count+=1
        assert event_tickers==set(r['event_tickers']) and event_tickers
        assert not clean_target_article(r)[2]
        ids.add(rid);bodies.update(body_keys)
        if u:urls.add(u)
        count+=1;new+=not r['already_in_legacy_train']
    assert count==done['retained'] and new==done['additional_to_legacy_train']
    rejected=list(rows(out/'rejected_candidates.jsonl'));assert len(rejected)==done['rejected'];assert count+len(rejected)==done['raw_candidates']
    review=json.loads((out/'human_review.json').read_text(encoding='utf8'));assert len(review)==done['human_review_rows']
    assert len({r['record_id'] for r in review})==len(review)
    for r in review:
        assert r['reviewer']=='' and all(r[k] is None for k in ('company_correct','event_correct','body_clean','time_correct'))
        assert r['record_id'] in (ids if r['stratum']=='retained' else {x['record_id'] for x in rejected})
    result=dict(source_unchanged=True,legacy_fields_unchanged=True,legacy_articles=len(enriched),explicit_time_candidates=times,
        retained=count,additional_to_legacy_train=new,evidence_spans_checked=evidence_count,holdout_overlap=0,
        retained_duplicates=0,human_review_completed=0,review_rows=len(review),trained=False,
        artifact_hashes={p.name:sha(p) for p in out.iterdir() if p.is_file() and p.name!='verification.json'})
    dump(out/'verification.json',result);print(json.dumps({k:v for k,v in result.items() if k!='artifact_hashes'}),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--dataset',type=Path,required=True);main(ap.parse_args().dataset.resolve())
