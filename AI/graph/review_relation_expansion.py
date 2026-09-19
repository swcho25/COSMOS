"""Export full source review packets and fail-closed merge of relation reviews.

No inferred approvals: every new assertion requires an explicit decision bound
to its evidence payload and complete original article. Historical baseline
decisions are copied unchanged. AI development review is not human-gold accuracy.
"""
from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path
import json

from expand_grounded_relations_v3 import sha, digest, load, canonical, readonly


def rows(path):
    with path.open(encoding='utf8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def inputs(directory):
    manifest=load(directory/'manifest.json')
    assert manifest['artifact_complete'] is True
    result={}
    for key in ('relationship_evidence','new_relationship_evidence'):
        spec=manifest['files'][key]; path=directory/spec['path']
        assert sha(path)==spec['sha256']
        result[key]=rows(path); assert len(result[key])==spec['rows']
    return manifest,result


def prepare(directory,out,shards=3):
    directory,out=directory.resolve(),out.resolve(); assert shards>0
    manifest,data=inputs(directory)
    parent=Path(manifest['parent_kg']['directory'])
    assert sha(parent/'manifest.json')==manifest['parent_kg']['manifest_sha256']
    out.mkdir(parents=True,exist_ok=False)
    con=readonly(parent/'state.sqlite3')
    packets=[[] for _ in range(shards)]
    # Group by article so one whole source is reviewed by one reviewer.
    grouped={}
    for edge in data['new_relationship_evidence']: grouped.setdefault(edge['article_id'],[]).append(edge)
    try:
        for i,(aid,edges) in enumerate(sorted(grouped.items())):
            text=con.execute('SELECT payload FROM articles WHERE article_id=?',(aid,)).fetchone()[0]
            article=json.loads(text)
            source={k:article[k] for k in ('article_id','title','body','published_at','source_url')}
            assert all(digest(source)==e['source_article_sha256'] for e in edges)
            packets[i%shards].append({'article_id':aid,'source':source,'source_sha256':digest(source),
                'edges':[{'evidence_id':e['edge_id'],'edge_sha256':digest(e),'edge':e} for e in edges]})
    finally: con.close()
    files={}
    for i,items in enumerate(packets):
        path=out/f'packet_{i}.jsonl'
        with path.open('x',encoding='utf8',newline='\n') as stream:
            for item in items: stream.write(canonical(item)+'\n')
        files[path.name]={'sha256':sha(path),'articles':len(items),'edges':sum(len(x['edges']) for x in items)}
    receipt={'schema':'cosmos-v5-full-context-review-packets-1','source_manifest_sha256':sha(directory/'manifest.json'),'files':files}
    (out/'packets.json').write_text(canonical(receipt)+'\n',encoding='utf8')
    print(canonical(receipt)); return receipt


def merge(directory,baseline_policy,reviews,out):
    directory,out=directory.resolve(),out.resolve()
    assert not out.exists()
    manifest,data=inputs(directory)
    protocol=load(directory/'protocol.json')
    old=load(baseline_policy)
    assert old['source_manifest_sha256']==protocol['hashes']['baseline_manifest']
    all_edges={e['edge_id']:e for e in data['relationship_evidence']}
    new={e['edge_id']:e for e in data['new_relationship_evidence']}
    decisions={x['evidence_id']:x for x in old['decisions']}
    assert len(decisions)==len(old['decisions']) and set(decisions)==set(all_edges)-set(new)
    audit_files={}
    for path in reviews:
        audit_files[str(path.resolve())]=sha(path)
        for row in rows(path):
            eid=row['evidence_id']; assert eid in new and eid not in decisions
            assert row['decision'] in ('keep','review','exclude') and row['reason'].strip()
            assert row['edge_sha256']==digest(new[eid])
            assert row['source_sha256']==new[eid]['source_article_sha256']
            assert row['full_article_reviewed'] is True
            decisions[eid]=row
    assert set(decisions)==set(all_edges), 'Missing explicit contextual review; do not default to acceptance'
    merged={**old,'source_manifest_sha256':sha(directory/'manifest.json'),
        'reviewer_type':'AI_assisted_full_article_development_review','human_gold':False,
        'semantic_accuracy_estimated':False,'baseline_policy_sha256':sha(baseline_policy),
        'new_audits':audit_files,'counts':dict(Counter(x['decision'] for x in decisions.values())),
        'decisions':[decisions[k] for k in sorted(decisions)]}
    out.write_text(json.dumps(merged,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(canonical({'path':str(out),'sha256':sha(out),'counts':merged['counts']})); return merged


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='mode',required=True)
    prep=sub.add_parser('prepare'); prep.add_argument('--directory',type=Path,required=True); prep.add_argument('--out',type=Path,required=True); prep.add_argument('--shards',type=int,default=3)
    finish=sub.add_parser('merge'); finish.add_argument('--directory',type=Path,required=True); finish.add_argument('--baseline-policy',type=Path,required=True); finish.add_argument('--reviews',type=Path,nargs='+',required=True); finish.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.mode=='prepare': prepare(a.directory,a.out,a.shards)
    else: merge(a.directory,a.baseline_policy,a.reviews,a.out)
