"""Same GAT parameters as NewsGAT, with distinct text per direct company node.

text/sentiment are [batch,company,feature]. Changing one target does not silently
broadcast its content to every direct node; graph messages may still propagate it.
The parameter names and sizes match NewsGAT, permitting numerical broadcast equivalence
tests. No service checkpoint loader is changed.
"""
import torch
from news_impact_model import NewsGAT


class TargetNewsGAT(NewsGAT):
    def forward(self,text,sentiment,mentions,static,edge_index,edge_attr,attention=False):
        b,n,_=mentions.shape
        if text.ndim!=3 or sentiment.shape[:2]!=(b,n) or text.shape[:2]!=(b,n):raise ValueError('Expected per-company features')
        ids=torch.arange(n,device=text.device)
        node=torch.cat([static.expand(b,-1,-1),mentions,self.company(ids).expand(b,-1,-1)],-1)
        news=self.text(torch.cat([text,sentiment],-1))
        h=self.node(node)+mentions[:,:,:1]*news
        offsets=torch.arange(b,device=text.device)*n
        ei=(edge_index[:,None,:]+offsets[None,:,None]).reshape(2,-1);ea=edge_attr.repeat(b,1)
        h=h.reshape(b*n,-1)
        h=self.norm1(h+self.dropout(torch.nn.functional.gelu(self.gat1(h,ei,ea))))
        if attention:out,weights=self.gat2(h,ei,ea,return_attention_weights=True)
        else:out=self.gat2(h,ei,ea)
        h=self.norm2(h+self.dropout(torch.nn.functional.gelu(out)))
        direction=self.direction(h).reshape(b,n,2,3);magnitude=self.magnitude(h).sigmoid().reshape(b,n,2)
        return (direction,magnitude,weights) if attention else (direction,magnitude)
