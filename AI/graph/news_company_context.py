"""Research-only mention-centered input selection for frozen news encoders.

Keep the existing 256 embedding / 128 sentiment token limits and article-level
representation. Retain a bounded title, then allocate the remaining tokens equally
to windows around the first matched mention of each direct company. Windows are
literal contiguous source spans, merged when overlapping. Never synthesize claims.
This does not introduce per-company embeddings or change candidate/graph features.
Many-company articles may still exceed the budget; coverage is measured explicitly.
"""
from __future__ import annotations

import bisect
import numpy as np
import torch
from news_text_features import FrozenText


def merge_spans(spans):
    merged=[]
    for start,end in sorted(set(spans)):
        if merged and start<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:
            merged.append((start,end))
    return merged


def select_context(title,body,mentions,tokenizer,max_length):
    """mentions: {ticker,start,end} from the sanitized full title+'\n'+body.

    The final text is retokenized to enforce the exact model budget. Coverage uses
    original character spans; it does not infer event comprehension from name presence.
    """
    original=title+"\n"+body
    offsets=tokenizer(original,add_special_tokens=False,return_offsets_mapping=True,
                      truncation=False,verbose=False)["offset_mapping"]
    offsets=[(a,b) for a,b in offsets if b>a]
    starts=[a for a,b in offsets]; ends=[b for a,b in offsets]
    title_tokens=tokenizer(title,add_special_tokens=False,return_offsets_mapping=True,
                           truncation=False,verbose=False)["offset_mapping"]
    title_cap=min(48,max_length//4)
    title_end=title_tokens[min(len(title_tokens),title_cap)-1][1] if title_tokens else 0
    first={}
    for m in sorted(mentions,key=lambda m:(m["start"],m["ticker"])):
        first.setdefault(m["ticker"],m)
    special=tokenizer.num_special_tokens_to_add(pair=False)
    budget=max(1,max_length-special-min(len(title_tokens),title_cap))
    quota=max(1,budget//max(1,len(first))-3)
    def spans_for(q):
        spans=[(0,title_end)] if title_end else []
        for m in first.values():
            if m["end"]<=title_end:continue
            a=bisect.bisect_right(ends,m["start"])
            b=bisect.bisect_left(starts,m["end"])
            if a>=len(offsets) or b<=a:continue
            room=max(0,q-(b-a)); left=max(0,a-room//2)
            right=min(len(offsets),max(b,left+q))
            left=max(0,min(left,right-max(q,b-a)))
            spans.append((offsets[left][0],offsets[right-1][1]))
        if not first and offsets:
            spans.append((0,offsets[min(len(offsets),max_length-special)-1][1]))
        return merge_spans(spans)
    while True:
        spans=spans_for(quota)
        selected="\n".join(original[a:b] for a,b in spans)
        size=len(tokenizer(selected,add_special_tokens=True,truncation=False,verbose=False)["input_ids"])
        if size<=max_length or quota<=1:break
        quota-=1
    # Pathological many-company/title cases: preserve deterministic earliest spans.
    while size>max_length and spans:
        spans.pop()
        selected="\n".join(original[a:b] for a,b in spans)
        size=len(tokenizer(selected,add_special_tokens=True,truncation=False,verbose=False)["input_ids"])
    covered={m["ticker"] for m in mentions if any(a<=m["start"] and m["end"]<=b for a,b in spans)}
    return selected,{"covered":sorted(covered),"matched":sorted(first),"tokens":size,"spans":spans}


class ContextEncoder(FrozenText):
    @torch.inference_mode()
    def encode_role(self,role,docs,hits,batch_size=32):
        tokenizer,model=self.get(role)
        result=np.zeros((len(docs),model.config.hidden_size if role=="embedding" else 3),dtype="float32")
        ids=[i for i,d in enumerate(docs) if role=="embedding" or (d["region"]=="domestic")== (role=="sentiment_ko")]
        coverage=[]
        limit=256 if role=="embedding" else 128
        if role!="embedding":
            labels={str(v).lower():int(k) for k,v in model.config.id2label.items()}
            if set(labels)!={"negative","neutral","positive"}:raise ValueError("Unexpected label order")
        for start in range(0,len(ids),batch_size):
            batch=ids[start:start+batch_size]; texts=[]
            for i in batch:
                text,audit=select_context(docs[i].get("title") or "",docs[i].get("body") or "",hits[i],tokenizer,limit)
                texts.append(text); coverage.append({"article":i,"role":role,**audit})
            inputs=tokenizer(texts,padding=True,truncation=True,max_length=limit,return_tensors="pt").to(self.device)
            if role=="embedding":
                hidden=model(**inputs).last_hidden_state
                mask=inputs["attention_mask"].unsqueeze(-1)
                pooled=(hidden*mask).sum(1)/mask.sum(1).clamp(min=1)
                values=torch.nn.functional.normalize(pooled,dim=-1)
            else:
                values=model(**inputs).logits.softmax(-1)[:,[labels[k] for k in ("negative","neutral","positive")]]
            result[batch]=values.cpu().numpy()
            if start%2048==0:print(f"context {role}: {start}/{len(ids)}",flush=True)
        return result,coverage
