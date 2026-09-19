"""Explicit pre-fit quality-quarantine derivation; immutable parent features.

The parent price labels, feature entries and encoded bank are not rewritten.
Approved train articles leave training/prior populations. Approved test articles
retain every target and label in the evaluation denominator but have zero input
and loss masks, so predictions are explicit abstentions. No validation article or
legacy validation row may be quarantined by this policy.

The old encoding protocol describes the PARENT prepared snapshot. It is copied
unchanged and is never claimed to attest the derived masks. A separate derivation
protocol/receipt binds parent, approved review, current temporal graph, changed
files and unchanged encoding. This is conservative contamination handling, not
a permanent article irrelevance label or proof of future-price leakage.
"""
from __future__ import annotations
import argparse
import collections
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
import prepare_grounded_training as prep

VERSION='grounded-price-research-derived-v1'
POLICY_SCHEMA='grounded-input-quality-quarantine-1'


def quality_plan(docs,pairs,policy):
    prep.require(policy.get('schema')==POLICY_SCHEMA and policy.get('frozen_before_training') is True,'Quality policy is not frozen pre-fit')
    records=policy['records'];by_id={row['article_id']:row for row in records}
    prep.require(len(records)==len(by_id)>0,'Duplicate/empty quarantine policy')
    prep.require(all(row.get('split') in ('train','test') and row.get('reason') and row.get('evidence_type') for row in records),'Invalid quarantine record')
    indexes={};result_docs=[]
    for index,doc in enumerate(docs):
        row=dict(doc);record=by_id.get(doc['record_id'])
        if record is not None:
            prep.require(doc['split']==record['split'],'Quality policy article split differs or touches fixed validation')
            prep.require(doc['record_id'] not in indexes,'Repeated primary article id')
            indexes[doc['record_id']]=index
            row['input_quality_quarantine']={'reason':record['reason'],'evidence_type':record['evidence_type'],
                                             'original_split':doc['split'],'permanent_irrelevance_label':False,
                                             'future_price_leakage_proven':False}
            if doc['split']=='train':row['split']='quality_quarantine'
        result_docs.append(row)
    prep.require(set(indexes)==set(by_id),'Quarantine policy article missing from prepared dataset')
    result_pairs=pairs.copy();rows_by_split=collections.Counter();pair_counts=collections.Counter()
    for rid,index in indexes.items():
        split=by_id[rid]['split'];selected=result_pairs.article.eq(index)
        prep.require(selected.any() and result_pairs.loc[selected,'split'].eq(split).all(),'Article/pair split differs')
        rows_by_split[split]+=1;pair_counts[split]+=int(selected.sum())
        if split=='train':result_pairs.loc[selected,'split']='quality_quarantine'
    return result_docs,result_pairs,sorted(indexes.values()),{'articles_by_original_split':dict(rows_by_split),'pairs_by_original_split':dict(pair_counts)}


def verify_encoding(directory,prepared_sha):
    directory=Path(directory);encoded=json.loads((directory/'encoded.json').read_text());encoding=json.loads((directory/'encoding_protocol.json').read_text())
    prep.require(encoded.get('complete') is True and encoding['prepared_sha256']==prepared_sha,'Encoding does not bind the declared parent prepared snapshot')
    prep.require(encoded['encoding_protocol_sha256']==prep.sha(directory/'encoding_protocol.json'),'Encoding protocol hash differs')
    for name,digest in encoded['files'].items():prep.require(prep.sha(directory/name)==digest,'Encoded feature bank differs')
    return encoded,encoding


def derive(parent,out,integrated,quality_policy):
    parent,out,integrated,quality_policy=map(lambda p:Path(p).resolve(),(parent,out,integrated,quality_policy))
    prep.require(not out.exists(),'Derived output must be a new directory')
    prepared=prep.verify_prepared(parent);parent_prepared_sha=prep.sha(parent/'prepared.json');verify_encoding(parent,parent_prepared_sha)
    policy=json.loads(quality_policy.read_text(encoding='utf-8-sig'))
    review=Path(policy['review_artifact']['path'])
    if not review.is_absolute():review=quality_policy.parent/review
    prep.require(prep.sha(review)==policy['review_artifact']['sha256'],'Approved quality review changed')
    docs=list(prep.rows(parent/'articles.jsonl'));pairs=pd.read_parquet(parent/'pairs.parquet')
    changed_docs,changed_pairs,affected,quality_counts=quality_plan(docs,pairs,policy)
    out.mkdir(parents=True)
    names=set(prepared['files'])|{'prepared.json','encoding_protocol.json','encoded.json','target_text.npy','target_sentiment.npy'}
    for name in sorted(names):
        prep.require(Path(name).name==name,'Unexpected nested prepared file')
        shutil.copyfile(parent/name,out/name)
    shutil.copyfile(parent/'prepared.json',out/'parent_prepared.json')
    shutil.copyfile(quality_policy,out/'quality_policy.json')
    # The copied parent input is still byte-identical here, allowing the frozen
    # graph builder to verify it honestly before the separate derivation stage.
    graph_receipt=prep.add_graphs(out,integrated)
    with (out/'articles.jsonl').open('w',encoding='utf8',newline='\n') as stream:
        for row in changed_docs:prep.write_row(stream,row)
    changed_pairs.to_parquet(out/'pairs.parquet',index=False)
    for name in ('feature_mapping.npy','direct_mask.npy','mentions.npy'):
        array=np.load(out/name,mmap_mode='r+');array[affected]=0;array.flush();array._mmap.close()
    protocol={'schema':VERSION,'frozen_before_training':True,'parent':{'directory':str(parent),'prepared_sha256':parent_prepared_sha,
        'encoding_protocol_sha256':prep.sha(parent/'encoding_protocol.json'),'encoded_sha256':prep.sha(parent/'encoded.json')},
        'quality_policy':{'source':str(quality_policy),'sha256':prep.sha(quality_policy),'review_path':str(review.resolve()),'review_sha256':prep.sha(review)},
        'integrated':{'directory':str(integrated),'manifest_sha256':prep.sha(integrated/'manifest.json')},
        'code_sha256':prep.sha(Path(__file__)),'preparation_code_sha256':prep.sha(Path(prep.__file__)),
        'changed_policy':'train articles/pairs excluded from loss and train priors; test pairs/splits/labels unchanged, inputs and masks zero => abstention',
        'encoding_lineage':'unchanged parent feature entries/bank; parent prepared SHA does not claim to describe derived masks',
        'unused_encoded_features_retained':True,'quality_counts':quality_counts,'affected_article_indices':affected,
        'production_approved':False,'future_price_leakage_proven':False,'permanent_irrelevance_labels_created':False}
    prep.dump(out/'derivation_protocol.json',protocol)
    mask=np.load(out/'direct_mask.npy',mmap_mode='r');mapping=np.load(out/'feature_mapping.npy',mmap_mode='r')
    node={t:i for i,t in enumerate(pd.read_json(out/'companies.json',dtype={'ticker':str}).ticker)}
    counts={}
    for split in ('train','val','test','legacy_val','quality_quarantine'):
        article_ids=np.flatnonzero([row['split']==split for row in changed_docs]);selection=changed_pairs.split.eq(split)
        counts[split+'_articles']=len(article_ids);counts[split+'_pairs']=int(selection.sum())
        counts[split+'_input_pairs']=int((mapping[article_ids]>0).sum())
        group=changed_pairs.loc[selection]
        counts[split+'_predictable_pairs']=int(mask[group.article.to_numpy(),group.ticker.map(node).to_numpy()].sum())
    mask._mmap.close();mapping._mmap.close()
    parent_encoded=json.loads((out/'encoded.json').read_text())
    summary={'version':VERSION,'complete':True,'counts':counts,'parent':protocol['parent'],'source':prepared['source'],
             'new_feature_entries':prepared['new_feature_entries'],'old_feature_bank_size':prepared['old_feature_bank_size'],
             'legacy_val_pairs':prepared['legacy_val_pairs'],'encoded':True,'trained':False,'encoding_prepared_sha256':parent_prepared_sha,
             'encoded_features_unchanged':True,'quality_counts':quality_counts,'production_approved':False,
             'derivation_protocol_sha256':prep.sha(out/'derivation_protocol.json'),
             'files':{p.name:prep.sha(p) for p in out.iterdir() if p.is_file() and p.name!='prepared.json'}}
    prep.dump(out/'prepared.json',summary)
    receipt={'complete':True,'prepared_sha256':prep.sha(out/'prepared.json'),'derivation_protocol_sha256':summary['derivation_protocol_sha256'],
             'parent_prepared_sha256':parent_prepared_sha,'encoding_unchanged_sha256':prep.sha(out/'encoded.json'),
             'quality_counts':quality_counts,'counts':counts,'graph_receipt_sha256':prep.sha(out/'graphs_receipt.json')}
    prep.dump(out/'derivation_complete.json',receipt)
    verify_dataset(out)
    return receipt


def verify_dataset(out):
    out=Path(out);prepared=json.loads((out/'prepared.json').read_text())
    if prepared.get('version')==prep.VERSION:return prep.verify_prepared(out)
    prep.require(prepared.get('version')==VERSION and prepared.get('complete') is True,'Unknown/incomplete derived dataset')
    protocol=json.loads((out/'derivation_protocol.json').read_text());receipt=json.loads((out/'derivation_complete.json').read_text())
    prep.require(receipt.get('complete') is True and receipt['prepared_sha256']==prep.sha(out/'prepared.json'),'Derived completion differs')
    prep.require(prepared['derivation_protocol_sha256']==prep.sha(out/'derivation_protocol.json')==receipt['derivation_protocol_sha256'],'Derivation protocol differs')
    prep.require(protocol['schema']==VERSION and protocol['frozen_before_training'] is True,'Unfrozen derivation')
    prep.require(protocol['code_sha256']==prep.sha(Path(__file__)) and protocol['preparation_code_sha256']==prep.sha(Path(prep.__file__)),'Derivation code changed')
    parent=Path(protocol['parent']['directory']);parent_prepared=prep.verify_prepared(parent)
    parent_sha=prep.sha(parent/'prepared.json')
    prep.require(parent_sha==protocol['parent']['prepared_sha256']==prep.sha(out/'parent_prepared.json')==prepared['encoding_prepared_sha256'],'Parent preparation identity differs')
    for name,digest in prepared['files'].items():
        prep.require(Path(name).name==name and prep.sha(out/name)==digest,'Derived file changed: '+name)
    for name in ('encoding_protocol.json','encoded.json','new_feature_entries.jsonl','target_text.npy','target_sentiment.npy'):
        prep.require(prep.sha(parent/name)==prep.sha(out/name),'Parent feature lineage differs: '+name)
    verify_encoding(out,parent_sha)
    prep.require(prep.sha(Path(protocol['quality_policy']['review_path']))==protocol['quality_policy']['review_sha256'],'Approved reviewer artifact changed')
    prep.require(prep.sha(out/'quality_policy.json')==protocol['quality_policy']['sha256'],'Approved quality policy changed')
    prep.require(prep.sha(Path(protocol['integrated']['directory'])/'manifest.json')==protocol['integrated']['manifest_sha256'],'Integrated graph source changed')
    graph_receipt=json.loads((out/'graphs_receipt.json').read_text())
    prep.require(graph_receipt['integrated_manifest_sha256']==protocol['integrated']['manifest_sha256'],'Graph and derivation reference different integrated sources')
    prep.require(graph_receipt['candidate_sha256']==prep.sha(out/'graphs_candidate.json') and graph_receipt['base_sha256']==prep.sha(out/'graphs_base.json'),'Derived graph content differs')
    expected_docs,expected_pairs,affected,counts=quality_plan(list(prep.rows(parent/'articles.jsonl')),pd.read_parquet(parent/'pairs.parquet'),json.loads((out/'quality_policy.json').read_text(encoding='utf-8-sig')))
    prep.require(expected_docs==list(prep.rows(out/'articles.jsonl')) and counts==protocol['quality_counts'] and affected==protocol['affected_article_indices'],'Derived quarantine semantics differ')
    pd.testing.assert_frame_equal(expected_pairs,pd.read_parquet(out/'pairs.parquet'),check_dtype=True)
    for name in ('feature_mapping.npy','direct_mask.npy','mentions.npy'):
        original=np.load(parent/name,mmap_mode='r');actual=np.load(out/name,mmap_mode='r')
        prep.require(original.shape==actual.shape and original.dtype==actual.dtype,'Derived array schema differs')
        for start in range(0,len(original),4096):
            end=min(start+4096,len(original));expected=np.array(original[start:end],copy=True)
            local=[i-start for i in affected if start<=i<end]
            if local:expected[local]=0
            prep.require(np.array_equal(expected,actual[start:end]),'Derived array differs outside approved quarantine: '+name)
        original._mmap.close();actual._mmap.close()
    return prepared


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--parent',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--integrated',type=Path,required=True);parser.add_argument('--quality-policy',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(derive(args.parent,args.out,args.integrated,args.quality_policy),ensure_ascii=False),flush=True)
