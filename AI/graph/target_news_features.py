"""Research target-specific frozen features with audited input-only safeguards.

Never use labels, predictions or returns in selection. Trim standalone related-news
sections; quarantine concatenated read-more feeds (cannot reliably reconstruct the
article boundary). Reject three observed entity homonyms locally, without rewriting
the production matcher. Remaining valid mentions include incidental/nonfinancial
mentions: this is not a learned relevance classifier. Preserve literal spans and
prefix the actual target ticker/name. Prefer a body mention over a title mention so
title mentions do not cause the old selector to emit only the headline.
"""
import re
import sys
from pathlib import Path
import numpy as np
import torch
from news_content import sanitize_article
from news_company_context import select_context
from news_text_features import FrozenText

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'ner'))
from matcher import CompanyMatcher


def clean_target_article(article):
    d=sanitize_article(article);body=d.get('body') or '';flags=[]
    marker=re.search(r'(?m)^\s*관련\s*뉴스\s*$',body)
    if marker:body=body[:marker.start()];flags.append('related_news_tail_removed')
    quarantine=len(re.findall(r'read\s+more',body,re.I))>=2
    if quarantine:flags.append('read_more_feed_quarantine')
    d['body']=body
    return d,flags,quarantine


def valid_target_hit(full,ticker,start,end):
    near=full[max(0,start-30):end+50]
    if ticker in ('AAPL',) and re.search(r'Apple\s+Valley',near,re.I):return False
    if ticker=='086280' and re.search(r'글로비스\s*(대학|캐피털)|Glovis\s+(University|Capital)',near,re.I):return False
    if ticker=='WDAY':
        context=full[max(0,start-100):end+160]
        if not re.search(r'\bWDAY\b|Workday,?\s+Inc|cloud|software|enterprise|human resources|payroll|클라우드|소프트웨어|워크데이',context,re.I):return False
    return True


def select_target_context(title,body,hits,ticker,name,tokenizer,limit):
    target=[h for h in hits if h['ticker']==ticker]
    if not target:raise ValueError('Target requires a verified literal mention')
    body_hits=[h for h in target if h['start']>len(title)]
    selected_hits=body_hits or target
    prefix=f'Target {ticker}: {name}\n'
    offsets=tokenizer(prefix,add_special_tokens=False,return_offsets_mapping=True,truncation=False,verbose=False)['offset_mapping']
    if len(offsets)>24:prefix=prefix[:offsets[23][1]]+'\n'
    cost=len(tokenizer(prefix,add_special_tokens=False,truncation=False,verbose=False)['input_ids'])+2
    budget=max(16,limit-cost)
    while True:
        # Title-only mention: use the lead too, not just the title.
        use_hits=selected_hits if body_hits else []
        excerpt,audit=select_context(title,body,use_hits,tokenizer,budget)
        text=prefix+excerpt
        size=len(tokenizer(text,add_special_tokens=True,truncation=False,verbose=False)['input_ids'])
        if size<=limit:break
        budget-=1
        if budget<8:raise ValueError('Target prefix cannot fit encoder budget')
    covered=any(any(a<=h['start'] and h['end']<=b for a,b in audit['spans']) for h in target)
    return text,dict(tokens=size,covered=covered,source_spans=audit['spans'],prefix=prefix)


class TargetEncoder(FrozenText):
    @torch.inference_mode()
    def encode_entries(self,role,docs,entries,names,batch_size=32):
        tokenizer,model=self.get(role)
        result=np.zeros((len(entries)+1,model.config.hidden_size if role=='embedding' else 3),dtype='float32')
        eligible=[j for j,e in enumerate(entries) if role=='embedding' or (docs[e['article']]['region']=='domestic')==(role=='sentiment_ko')]
        limit=256 if role=='embedding' else 128;audits=[]
        if role!='embedding':
            labels={str(v).lower():int(k) for k,v in model.config.id2label.items()}
            assert set(labels)=={'negative','neutral','positive'}
        for begin in range(0,len(eligible),batch_size):
            js=eligible[begin:begin+batch_size];texts=[]
            for j in js:
                e=entries[j];d=docs[e['article']]
                text,audit=select_target_context(d.get('title') or '',d.get('body') or '',e['hits'],e['ticker'],names[e['ticker']],tokenizer,limit)
                texts.append(text);audits.append(dict(feature_id=j+1,article=e['article'],ticker=e['ticker'],role=role,**audit))
            inputs=tokenizer(texts,padding=True,truncation=False,return_tensors='pt').to(self.device)
            if role=='embedding':
                hidden=model(**inputs).last_hidden_state;mask=inputs['attention_mask'].unsqueeze(-1)
                values=torch.nn.functional.normalize((hidden*mask).sum(1)/mask.sum(1).clamp(min=1),dim=-1)
            else:values=model(**inputs).logits.softmax(-1)[:,[labels[k] for k in ('negative','neutral','positive')]]
            result[np.array(js)+1]=values.cpu().numpy()
            if begin%2048==0:print(f'target {role}: {begin}/{len(eligible)}',flush=True)
        return result,audits
