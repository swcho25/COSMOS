"""Server-only bounded sentiment and current-window features for a news backfill.

The entire news history is analyzed and retained. Aggregates remain 7/30/90-day
as_of windows, not all-time sums and not snapshots backdated to each old month.
Existing audited recent DART/SEC/adjusted-price inputs supply current features.
No GNN retraining or GNN price-impact inference is performed by this job.
"""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from finalize_lake_recent import sha, schema, empty_schema
from lake_sentiment import run as run_sentiment

def atomic(path,obj):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);temp=p.with_name(p.name+'.tmp-'+uuid.uuid4().hex)
    with temp.open('x',encoding='utf8') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,p)

def rows(path):
    with Path(path).open(encoding='utf8') as f:
        for line in f:
            if line.strip():yield json.loads(line)

def verify_files(base,manifest):
    for d in manifest['datasets']:
        for part in d['files']:
            if sha(base/part['local_file'])!=part['sha256']:raise ValueError('Analysis file changed '+part['local_file'])

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-dir',type=Path,required=True)
    ap.add_argument('--bundle',type=Path,required=True);ap.add_argument('--companies',type=Path,required=True)
    ap.add_argument('--run-id',required=True);ap.add_argument('--as-of',required=True)
    args=ap.parse_args();root=args.run_dir.resolve();base=root/'analysis'
    manifest=json.loads((base/'analysis_manifest.json').read_text());verify_files(base,manifest)
    datasets={d['name']:d for d in manifest['datasets']}
    sentiment_dir=root/'sentiment';sentiment_dir.mkdir(exist_ok=True)
    sentiment_files=[];counts=collections.Counter()
    for i,part in enumerate(datasets['sentiment_inputs']['files']):
        source=base/part['local_file'];attempt_root=sentiment_dir/f'shard-{i:06d}'
        receipt=attempt_root/'done.json';attempt_root.mkdir(exist_ok=True)
        if receipt.exists():
            info=json.loads(receipt.read_text());output=root/info['local_file']
            if info['input_sha256']!=sha(source) or sha(output)!=info['sha256']:raise ValueError('Sentiment checkpoint changed')
        else:
            attempt=attempt_root/uuid.uuid4().hex;attempt.mkdir()
            output=attempt/'sentiment.jsonl'
            report=run_sentiment(source,output,args.bundle,device='cpu',batch_size=16)
            samples=list(rows(output)) # at most one bounded analysis shard
            info={'local_file':str(output.relative_to(root)),'sha256':sha(output),'bytes':output.stat().st_size,
                  'row_count':report['output_rows'],'input_sha256':report['input_sha256'],
                  'status_counts':report['status_counts'],'schema':schema(samples) if samples else None}
            atomic(receipt,info)
        sentiment_files.append(info);counts.update(info['status_counts'])
        atomic(root/'finish_progress.json',{'counts':{'sentiment_shards':i+1,**counts},'message':'CPU sentiment, no local PC dependency'})
    usable=next((x['schema'] for x in sentiment_files if x['row_count']),None)
    if usable is None:raise ValueError('No mentioned article sentiment contract available')
    sentiment_dataset={'name':'news_sentiment','target_root':'/data-lake/analyzed/news/sentiment','dataset_path':'data',
                       'schema':usable,'key_columns':['document_id'],'row_count':sum(x['row_count'] for x in sentiment_files),
                       'files':sentiment_files}
    # Reconstruct only the current 90-day material for the bounded, tested finalizer.
    recent=root/'current_prepared';recent.mkdir(exist_ok=True)
    marker=recent/'_PREPARED';end=dt.datetime.fromisoformat(args.as_of.replace('Z','+00:00'));start=end-dt.timedelta(days=90)
    if not marker.exists():
        attempt=root/('current-attempt-'+uuid.uuid4().hex);attempt.mkdir()
        wanted=set();recent_counts={}
        for name in ['cleaned_news','news_mentions','news_relationships','sentiment_inputs']:
            n=0
            with (attempt/(name+'.jsonl')).open('x',encoding='utf8') as out:
                for part in datasets[name]['files']:
                    for row in rows(base/part['local_file']):
                        if name=='cleaned_news':
                            at=dt.datetime.fromisoformat(row['published_at'].replace('Z','+00:00'))
                            keep=start<=at<end
                            if keep:wanted.add(row['document_id'])
                        else:keep=row['document_id'] in wanted
                        if keep:out.write(json.dumps(row,ensure_ascii=False)+'\n');n+=1
            recent_counts[name]=n
        # Only identities of current-window documents enter the bounded finalizer.
        with (attempt/'document_id_map.jsonl').open('x',encoding='utf8') as out:
            for part in datasets['document_id_map']['files']:
                for row in rows(base/part['local_file']):
                    if row['document_id'] in wanted:out.write(json.dumps(row,ensure_ascii=False)+'\n')
        aux=root/'inputs'/'recent_aux';provenance=json.loads((aux/'manifest.json').read_text())
        if provenance['as_of']!=args.as_of:raise ValueError('Auxiliary windows differ')
        for name in ['disclosures.jsonl','disclosure_relationships.jsonl','prices.jsonl']:
            if sha(aux/name)!=provenance['files'][name]['sha256']:raise ValueError('Pinned auxiliary input changed')
            shutil.copyfile(aux/name,attempt/name)
        provenance.update(files={p.name:{'sha256':sha(p),'bytes':p.stat().st_size} for p in attempt.glob('*.jsonl')},
                          counts=recent_counts,companies_sha256=sha(args.companies))
        provenance['limitations'].append('Whole news history backfill; current aggregates reuse pinned recent disclosure/price coverage. No backdated monthly snapshots.')
        atomic(attempt/'manifest.json',provenance)
        # All remaining data is small enough for existing finalizer. Empty id-map handled below.
        for p in attempt.iterdir():
            dst=recent/p.name
            if dst.exists():
                if sha(dst)!=sha(p):raise ValueError('Partial current-window output conflicts')
            else:os.rename(p,dst)
        marker.write_text('backfill current-window subset complete\n')
    local_sentiment=root/'current_sentiment.jsonl'
    if not Path(str(local_sentiment)+'.report.json').exists():
        wanted={x['document_id'] for x in rows(recent/'sentiment_inputs.jsonl')}
        temporary=local_sentiment.with_name(local_sentiment.name+'.'+uuid.uuid4().hex)
        subset_counts=collections.Counter()
        with temporary.open('x',encoding='utf8') as out:
            for part in sentiment_files:
                for row in rows(root/part['local_file']):
                    if row['document_id'] in wanted:
                        out.write(json.dumps(row,ensure_ascii=False)+'\n');subset_counts[row['status']]+=1
        if local_sentiment.exists():
            if sha(local_sentiment)!=sha(temporary):raise ValueError('Partial current sentiment mismatch')
        else:os.rename(temporary,local_sentiment)
        atomic(str(local_sentiment)+'.report.json',{'input_sha256':sha(recent/'sentiment_inputs.jsonl'),
            'output_sha256':sha(local_sentiment),'scope':'server CPU full-history shard results, current-window subset','status_counts':dict(subset_counts)})
    final_marker=root/'current_final_path.json'
    if final_marker.exists():
        saved=json.loads(final_marker.read_text());final=Path(saved['path']).resolve()
        if not final.is_relative_to(root) or sha(final/'publication.json')!=saved['publication_sha256']:
            raise ValueError('Completed current-window publication changed')
    else:
        final=root/('current-final-'+uuid.uuid4().hex)
        subprocess.run([sys.executable,str(Path(__file__).with_name('finalize_lake_recent.py')),
                        '--prepared',str(recent),'--sentiment',str(local_sentiment),'--companies',str(args.companies),
                        '--out',str(final),'--run-id',args.run_id],check=True)
        atomic(final_marker,{'path':str(final),'publication_sha256':sha(final/'publication.json')})
    publication=json.loads((final/'publication.json').read_text())
    for dataset in publication['datasets']:
        if sha(final/dataset['local_file'])!=dataset['source_sha256']:
            raise ValueError('Completed current-window dataset changed: '+dataset['name'])
    result=[]
    for name in ['cleaned_news','news_mentions','news_relationships','document_id_map']:
        d=dict(datasets[name]);d['dataset_path']='data'
        d['files']=[{**p,'local_file':str((base/p['local_file']).relative_to(root))} for p in d['files']]
        result.append(d)
    result.append(sentiment_dataset)
    for d in publication['datasets']:
        if d['name'] in ['company_features','relationship_features','company_metrics','relationship_scores','graph_nodes','graph_edges','disclosure_mentions','disclosure_relationships']:
            p=final/d['local_file'];result.append({**d,'files':[{'local_file':str(p.relative_to(root)),'sha256':sha(p),'row_count':d['row_count'],'bytes':p.stat().st_size}]})
    output={'schema_version':'lake-backfill-publication-1','run_id':args.run_id,'as_of':args.as_of,
            'snapshot_id':publication['snapshot_id'],'datasets':result,'sentiment_status_counts':dict(counts),
            'source_manifest_sha256':sha(root/'source_manifest.json'),'identity_summary':json.loads((root/'identity_summary.json').read_text()),
            'limitations':publication['limitations']+['News mentions are literal dictionary matches. Full-history sentiment is stored separately by document_id.','GNN stock impact inference and retraining are not part of this news lake backfill.']}
    atomic(root/'backfill_publication.json',output);atomic(root/'finish.done.json',{'publication_sha256':sha(root/'backfill_publication.json'),'counts':dict(counts)})

if __name__=='__main__':main()
