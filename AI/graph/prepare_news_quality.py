"""Build a versioned, outcome-blind quality candidate set and a review package.

Preserve all original article fields in legacy_enriched.jsonl. Timestamp repairs
are new provenance fields, never overwrites of available_at or price labels.
New train-period candidates must survive known feed guards, a literal company
recheck, bounded financial-event evidence, and holdout/body/URL deduplication.
These are weakly selected research candidates, not independently reviewed gold.
No fitting, price labels, graph rebuilding or deployment occurs in this command.
"""
import argparse
import collections
import json
from pathlib import Path
from news_quality import audit_timestamp,text_digest,financial_evidence
from target_news_features import clean_target_article,valid_target_hit,CompanyMatcher
from news_impact_data import availability
from verify_news_predictive_value import sha,dump

HERE=Path(__file__).resolve().parent
TIME_FIELDS=('published_at_raw','published_at','published_timezone','published_date','date_normalization_method','collected_at')


def read_jsonl(path):
    with path.open(encoding='utf8') as f:
        for line in f:yield json.loads(line)


def keys(row):
    clean=clean_target_article(row)[0]
    return {text_digest(row.get('body')),text_digest(clean.get('body'))}-{text_digest('')}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--export',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);args=ap.parse_args()
    source=args.export.resolve();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    legacy=HERE/'artifacts/news_impact_v2/articles.jsonl';aliases=HERE/'artifacts/news_impact_v2/aliases.csv'
    dependencies=[legacy,aliases]+[legacy.parent/n for n in ('pairs.parquet','label_config.npz','predictions_stage3.npz')]+[p for p in source.iterdir() if p.is_file()]
    hashes={str(p):sha(p) for p in dependencies};dump(out/'protocol.json',dict(source_hashes=hashes,
        code_hashes={n:sha(HERE/n) for n in ('prepare_news_quality.py','news_quality.py','target_news_features.py','news_content.py')},
        purpose='training candidate expansion and provenance recovery',labels_used=False,human_reviewed=False,
        allowed_model_text_fields=['title','body'],source_payload_labels='unverified provenance only; never training targets or text inputs',
        original_validation_population=4813,primary_available_at_unchanged=True,review_sample='5 retained + 2 rejected per region/year, deterministic hash order'))
    metadata={r['record_id']:r for r in read_jsonl(source/'legacy_metadata.jsonl')}
    docs=list(read_jsonl(legacy));held_ids=set();held_bodies=set();held_urls=set();old_train_ids=set();old_train_bodies=set();old_train_urls=set();time_counts=collections.Counter();enriched=[]
    for d in docs:
        m=metadata.get(d['record_id']);assert m is not None,d['record_id']
        assert str(m['published_date'])[:10]==d['published_date'],d['record_id']
        timing=audit_timestamp(m);time_counts[(d['split'],d['region'],timing['timestamp_status'])]+=1
        enriched.append(dict(d,source_metadata=m,publication_time_audit=timing))
        if d['split'] in ('val','test'):
            held_ids.add(d['record_id']);held_bodies.update(keys(d))
            if m.get('canonical_url'):held_urls.add(m['canonical_url'])
        elif d['split']=='train':
            old_train_ids.add(d['record_id']);old_train_bodies.update(keys(d))
            if m.get('canonical_url'):old_train_urls.add(m['canonical_url'])
    with (out/'legacy_enriched.jsonl').open('x',encoding='utf8') as f:
        for d in enriched:f.write(json.dumps(d,ensure_ascii=False)+'\n')
    dump(out/'legacy_timestamp_counts.json',[dict(split=s,region=r,status=t,count=n) for (s,r,t),n in sorted(time_counts.items())])
    matcher=CompanyMatcher.from_csv(aliases);seen_ids=set();seen_bodies=set();seen_urls=set();counts=collections.Counter();retained=[];rejected=[];candidate_times=collections.Counter()
    # Choose duplicate representatives deterministically, independent of Spark order.
    candidates=sorted(read_jsonl(source/'training_candidates.jsonl'),key=lambda r:text_digest('quality-order:'+r['record_id']))
    for ordinal,raw in enumerate(candidates):
        if ordinal%5000==0:print(f'quality candidates {ordinal}/{len(candidates)}, retained {len(retained)}',flush=True)
        rid=raw['record_id'];year=str(raw['published_date'])[:4];region=raw['region'];reason=None
        if not ('2017'<=year<='2022'):reason='outside_training_period'
        k=keys(raw);url=raw.get('canonical_url')
        if rid in held_ids or k & held_bodies or (url and url in held_urls):reason='holdout_overlap'
        if not reason and (rid in seen_ids or k & seen_bodies or (url and url in seen_urls)):reason='candidate_duplicate'
        clean,flags,quarantine=clean_target_article(raw)
        if not reason and quarantine:reason='feed_quarantine'
        if not reason and len((clean.get('body') or '').strip())<200:reason='short_clean_body'
        evidence=[];hits=[]
        if not reason:
            allowed={m['ticker'] for m in raw['companies']};full=clean['title']+'\n'+clean['body']
            hits=[dict(ticker=m.ticker,start=m.start,end=m.end) for m in matcher.match(clean['title'],clean['body']).mentions
                  if m.confidence>=1 and m.ticker in allowed and valid_target_hit(full,m.ticker,m.start,m.end)]
            if not hits:reason='no_verified_company'
            else:
                evidence=financial_evidence(clean['title'],clean['body'],hits)
                if not evidence:reason='no_same_sentence_event'
        if reason:
            counts[(region,year,reason)]+=1
            rejected.append(dict(record_id=rid,region=region,year=year,title=raw['title'],url=raw.get('url'),reason=reason,body=clean.get('body','')[:1800],human_review_status='pending',
                **{k:raw.get(k) for k in TIME_FIELDS}))
            continue
        seen_ids.add(rid);seen_bodies.update(k)
        if url:seen_urls.add(url)
        timing=audit_timestamp(raw);candidate_times[(region,year,timing['timestamp_status'])]+=1
        event_tickers={e['ticker'] for e in evidence};verified_tickers={h['ticker'] for h in hits}
        verified_companies=[]
        for ticker in sorted(verified_tickers):
            own=[h for h in hits if h['ticker']==ticker];first=min(h['start'] for h in own)
            verified_companies.append(dict(ticker=ticker,n_mentions=len(own),first_pos='title' if first<len(clean['title']) else ('lead' if first<len(clean['title'])+201 else 'body'),confidence=1.,method='quality_verified_dictionary'))
        candidate=dict(clean,source_companies=raw['companies'],verified_tickers=sorted(verified_tickers),event_tickers=sorted(event_tickers),
            companies=verified_companies,
            literal_mentions=hits,event_evidence=evidence,quality_flags=flags,publication_time_audit=timing,
            available_at=availability(raw['published_date'],region).isoformat(),split='train',
            already_in_legacy_train=rid in old_train_ids or bool(k & old_train_bodies) or bool(url and url in old_train_urls),human_review_status='pending',
            label_status='unlabeled',dataset_role='weak_financial_event_training_candidate')
        retained.append(candidate);counts[(region,year,'retained')]+=1
    with (out/'training_candidates_clean.jsonl').open('x',encoding='utf8') as f:
        for row in retained:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    with (out/'rejected_candidates.jsonl').open('x',encoding='utf8') as f:
        for row in rejected:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    reviews=[]
    for region in ('domestic','overseas'):
        for year in map(str,range(2017,2023)):
            yes=[r for r in retained if r['region']==region and str(r['published_date'])[:4]==year]
            no=[r for r in rejected if r['region']==region and r['year']==year]
            for label,group,limit in [('retained',yes,5),('rejected',no,2)]:
                for r in sorted(group,key=lambda r:text_digest('review-quality-v1:'+r['record_id']))[:limit]:
                    reviews.append(dict(review_id=len(reviews)+1,record_id=r['record_id'],region=region,year=year,stratum=label,
                        title=r['title'],url=r.get('url'),evidence=r.get('event_evidence',[]),excerpt=r.get('body','')[:1800],
                        source_time={k:r.get(k) for k in TIME_FIELDS},publication_time_audit=audit_timestamp(r),
                        selection_reason=r.get('reason','weak_event_evidence'),reviewer='',company_correct=None,event_correct=None,body_clean=None,time_correct=None,notes=''))
    dump(out/'human_review.json',reviews)
    lines=['# 금융 사건 후보 검수','', '가격 정답과 모델 예측을 포함하지 않은 검수 자료다. 아직 사람 검수를 수행하지 않았다.',
           '동일 폴더의 human_review.json에 검수자와 판정을 기입한다. retained/rejected는 자동 규칙의 결과이며 정답이 아니다.',
           '이 문서는 본문 앞 1,800자와 근거 최대 5개를 표시한다. 전체 원문/정제문은 record_id로 원천 및 training_candidates_clean.jsonl을 확인한다.','']
    for r in reviews:
        lines += [f"## {r['review_id']}. {r['title']}",f"{r['region']} / {r['year']} / {r['stratum']} / {r['record_id']}",'',str(r.get('url') or 'URL 없음'),'',r['excerpt'].replace('\n','\n\n'),'']
        lines += [f"- 자동 후보 {e['ticker']} / {e['event_type']}: {e['quote']}" for e in r['evidence'][:5]]
        lines+=['','검수: 기업 일치 / 실제 기업 사건 / 본문 오염 / 발행시각 근거를 확인한다.','']
        lines+=['시각 원천: '+json.dumps(r['source_time'],ensure_ascii=False),'시각 판정: '+json.dumps(r['publication_time_audit'],ensure_ascii=False),'']
    (out/'HUMAN_REVIEW.md').write_text('\n'.join(lines),encoding='utf8')
    for path,digest in hashes.items():assert sha(Path(path))==digest,path
    result=dict(legacy_articles=len(docs),legacy_fields_unchanged=True,raw_candidates=len(candidates),retained=len(retained),rejected=len(rejected),
        additional_to_legacy_train=sum(not r['already_in_legacy_train'] for r in retained),human_review_rows=len(reviews),human_review_completed=0,
        counts=[dict(region=r,year=y,reason=s,count=n) for (r,y,s),n in sorted(counts.items())],
        candidate_time_counts=[dict(region=r,year=y,status=s,count=n) for (r,y,s),n in sorted(candidate_times.items())],
        source_unchanged=True,trained=False,accuracy_claim=None)
    dump(out/'completion.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('counts','candidate_time_counts')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
