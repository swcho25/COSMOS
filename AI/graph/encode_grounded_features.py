"""Frozen-context embedding adapter for Transformers TokenizersBackend.

This is a separate execution stage: it preserves the prepared CPU dataset/code
hashes. Overflow windows expose EVERY non-special token, checked against the
unbounded original token sequence. No tail is dropped. Pool content tokens over
all nonoverlapping windows, then L2 normalize and use the old train-only PCA64.
The execution cap only pauses after committed chunks; it never changes inputs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import numpy as np

import prepare_grounded_training as prep


def model_windows(tokenizer, text, max_length=256):
    # TokenizersBackend.__call__ in the installed version first limits encode()
    # to max_length, then exposes overflow of that prefix. Use the tokenizers
    # Encoding object directly and restore its mutable backend configuration.
    backend=tokenizer.backend_tokenizer
    prior_truncation,prior_padding=backend.truncation,backend.padding
    try:
        backend.no_truncation();backend.no_padding()
        encoding=backend.encode(text,add_special_tokens=False);raw=encoding.ids.copy()
        prep.require(bool(raw),'Empty context')
        budget=max_length-tokenizer.num_special_tokens_to_add(pair=False)
        prep.require(budget>0,'Invalid special token budget')
        encoding.truncate(budget,stride=0,direction='right')
        windows=[];covered=[]
        for part in [encoding]+encoding.overflowing:
            processed=backend.post_process(part,add_special_tokens=True)
            ids,special,attention=processed.ids,processed.special_tokens_mask,processed.attention_mask
            own=[token for token,flag in zip(ids,special) if not flag]
            prep.require(own==part.ids and 0<len(own)<=len(ids)<=max_length,'Invalid post-processed content tokens')
            prep.require(len(special)==len(ids)==len(attention) and all(x==1 for x in attention),'Unexpected padding in raw window')
            covered.extend(own)
            values={'input_ids':ids,'attention_mask':attention,'token_type_ids':processed.type_ids}
            record={key:values[key] for key in tokenizer.model_input_names if key in values};record['special_tokens_mask']=special
            windows.append((record,len(own)))
        prep.require(covered==raw,'Overflow encoding changed, duplicated or dropped original tokens')
        return windows
    finally:
        if prior_truncation is None:backend.no_truncation()
        else:backend.enable_truncation(**prior_truncation)
        if prior_padding is None:backend.no_padding()
        else:backend.enable_padding(**prior_padding)


def encode(out, *, device='cuda', batch_size=32, chunk_size=256, max_new_entries=None):
    import torch
    from transformers import AutoModel, AutoTokenizer
    out=Path(out);prepared=prep.verify_prepared(out)
    prep.require(batch_size>0 and chunk_size>0 and (max_new_entries is None or max_new_entries>=chunk_size), 'Invalid execution batch/cap')
    model_dir=prep.BUNDLE/'models/embedding'
    binding={'prepared_sha256':prep.sha(out/'prepared.json'),'code_sha256':prep.sha(Path(__file__)),
             'preparation_code_sha256':prep.sha(Path(prep.__file__)),
             'model_files':{str(p.relative_to(model_dir)):prep.sha(p) for p in sorted(model_dir.rglob('*')) if p.is_file()},
             'model_spec':json.loads((prep.BUNDLE/'pretrained.json').read_text())['embedding'],'chunk_size':chunk_size,
             'pooling':'exclude_special_tokens_masked_content_token_mean_across_all_overflow_windows_then_l2',
             'overflow_token_identity':'concatenate_nonspecial_tokens_exactly_equals_full_untruncated_token_ids','max_length':256}
    protocol=out/'encoding_protocol.json';chunks=out/'embedding_chunks';chunks.mkdir(exist_ok=True)
    if protocol.exists():prep.require(json.loads(protocol.read_text())==binding,'Embedding code/model/protocol changed')
    else:prep.dump(protocol,binding)
    if (out/'encoded.json').exists():
        receipt=json.loads((out/'encoded.json').read_text())
        prep.require(receipt['complete'] is True and receipt['encoding_protocol_sha256']==prep.sha(protocol),'Encoding completion changed')
        for name,digest in receipt['files'].items():prep.require(prep.sha(out/name)==digest,'Encoded data changed')
        return receipt
    entries=list(prep.rows(out/'new_feature_entries.jsonl'));prep.require(len(entries)==prepared['new_feature_entries'],'Feature count changed')
    model=tokenizer=None;new_count=0
    for start in range(0,len(entries),chunk_size):
        end=min(start+chunk_size,len(entries));target=chunks/f'{start:08d}_{end:08d}.npz';receipt=target.with_suffix('.json')
        if receipt.exists():
            saved=json.loads(receipt.read_text());prep.require(saved['start']==start and saved['end']==end and target.exists() and prep.sha(target)==saved['sha256'],'Completed chunk differs');continue
        if (out/'STOP_ENCODING').exists() or (max_new_entries is not None and new_count>=max_new_entries):
            return {'status':'paused_stop_file' if (out/'STOP_ENCODING').exists() else 'paused_execution_cap','next_entry':start,'total':len(entries),'new_entries':new_count}
        if model is None:
            torch.set_num_threads(4)
            tokenizer=AutoTokenizer.from_pretrained(model_dir,local_files_only=True,trust_remote_code=False)
            model=AutoModel.from_pretrained(model_dir,local_files_only=True,trust_remote_code=False).to(device).eval()
        started=time.monotonic();flat=[];owners=[];weights=[];audits=[]
        for i,entry in enumerate(entries[start:end]):
            prep.require(hashlib.sha256(entry['text'].encode()).hexdigest()==entry['text_sha256'],'Context hash changed')
            windows=model_windows(tokenizer,entry['text']);audits.append({'feature_id':entry['feature_id'],'text_sha256':entry['text_sha256'],
                'tokens':sum(weight for _,weight in windows),'windows':len(windows),'truncated':False,'full_token_identity_verified':True})
            for record,weight in windows:flat.append(record);owners.append(i);weights.append(weight)
        sums=np.zeros((end-start,model.config.hidden_size),dtype='float64');totals=np.zeros(end-start)
        with torch.inference_mode():
            for offset in range(0,len(flat),batch_size):
                batch=tokenizer.pad(flat[offset:offset+batch_size],padding=True,return_tensors='pt')
                special=batch.pop('special_tokens_mask').to(device);batch={key:value.to(device) for key,value in batch.items()}
                hidden=model(**batch).last_hidden_state;mask=(batch['attention_mask']*(1-special)).unsqueeze(-1)
                means=((hidden*mask).sum(1)/mask.sum(1).clamp(min=1)).cpu().numpy()
                for j,mean in enumerate(means):
                    owner,weight=owners[offset+j],weights[offset+j];sums[owner]+=mean*weight;totals[owner]+=weight
        vectors=sums/totals[:,None];vectors/=np.maximum(np.linalg.norm(vectors,axis=1,keepdims=True),1e-12)
        prep.require(np.isfinite(vectors).all(),'Nonfinite embedding')
        temporary=target.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            np.savez_compressed(stream,embedding=vectors.astype('float32'),feature_ids=np.array([x['feature_id'] for x in entries[start:end]]));stream.flush();os.fsync(stream.fileno())
        prep.replace_retry(temporary,target)
        prep.dump(receipt,{'sha256':prep.sha(target),'start':start,'end':end,'audits':audits,'seconds':time.monotonic()-started})
        new_count+=end-start;print(json.dumps({'stage':'embedding','completed':end,'total':len(entries),'seconds':time.monotonic()-started}),flush=True)
    old=prepared['old_feature_bank_size'];total=old+len(entries)
    texts=np.lib.format.open_memmap(out/'target_text.npy',mode='w+',dtype='float32',shape=(total,64))
    sentiments=np.lib.format.open_memmap(out/'target_sentiment.npy',mode='w+',dtype='float32',shape=(total,3))
    texts[:old]=np.load(prep.FEATURES/'target_text.npy');sentiments[:old]=np.load(prep.FEATURES/'target_sentiment.npy')
    with np.load(prep.FEATURES/'target_projection.npz') as projection:
        for start in range(0,len(entries),chunk_size):
            end=min(start+chunk_size,len(entries));target=chunks/f'{start:08d}_{end:08d}.npz'
            with np.load(target) as chunk:
                prep.require(np.array_equal(chunk['feature_ids'],np.arange(old+start,old+end)),'Embedding feature identity differs')
                texts[old+start:old+end]=(chunk['embedding']-projection['mean'])@projection['components'].T
            sentiments[old+start:old+end]=[r['sentiment'] for r in entries[start:end]]
    texts.flush();sentiments.flush();del texts,sentiments
    receipt={'complete':True,'features':total,'new_features':len(entries),'encoding_protocol_sha256':prep.sha(protocol),
             'files':{name:prep.sha(out/name) for name in ('target_text.npy','target_sentiment.npy')},'truncated_contexts':0,'all_context_tokens_verified':True}
    prep.dump(out/'encoded.json',receipt);return receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--device',default='cuda')
    parser.add_argument('--batch-size',type=int,default=32);parser.add_argument('--chunk-size',type=int,default=256);parser.add_argument('--max-new-entries',type=int)
    args=parser.parse_args();print(json.dumps(encode(args.out,device=args.device,batch_size=args.batch_size,chunk_size=args.chunk_size,max_new_entries=args.max_new_entries)),flush=True)
