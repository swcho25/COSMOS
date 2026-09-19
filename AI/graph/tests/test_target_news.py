"""Regression tests for observed contamination and target-specific signal paths."""
import sys
import unittest
from pathlib import Path
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from target_news_features import clean_target_article,valid_target_hit,select_target_context
from target_news_model import TargetNewsGAT
from news_impact_model import NewsGAT
from test_news_company_context import CharacterTokenizer


class TargetTests(unittest.TestCase):
    def test_related_feed_cannot_inject_target(self):
        d,f,q=clean_target_article(dict(title='Laboratory workshop',body='Useful article.\n관련 뉴스\nKia 2026 outlook'))
        self.assertNotIn('Kia',d['body']);self.assertFalse(q)
        _,_,q=clean_target_article(dict(title='Retail closures',body='Comcast gadget Technologyread more Apple news Technologyread more actual article'))
        self.assertTrue(q)
    def test_homonyms_rejected_but_companies_preserved(self):
        for text,ticker,word in [('Apple Valley','AAPL','Apple'),('일본 글로비스대학','086280','글로비스'),('My L.A. Workday','WDAY','Workday')]:
            p=text.index(word);self.assertFalse(valid_target_hit(text,ticker,p,p+len(word)))
        text='Workday enterprise cloud software WDAY';self.assertTrue(valid_target_hit(text,'WDAY',0,7))
        self.assertTrue(valid_target_hit('Apple reports earnings','AAPL',0,5))
    def test_title_mention_keeps_body_event_and_target_changes_window(self):
        title='Alpha and Beta';body='x '*100+'Alpha won a contract. '+'y '*120+'Beta lost its contract.'
        full=title+'\n'+body;hits=[]
        for name in ('Alpha','Beta'):
            start=0
            while (p:=full.find(name,start))>=0:
                hits.append(dict(ticker=name,start=p,end=p+len(name)));start=p+len(name)
        texts=[]
        for name,expected in [('Alpha','won a contract'),('Beta','lost its contract')]:
            text,audit=select_target_context(title,body,hits,name,name,CharacterTokenizer(),128)
            self.assertIn(expected,text);self.assertTrue(audit['covered']);self.assertLessEqual(audit['tokens'],128);texts.append(text)
        self.assertNotEqual(*texts)
    def test_broadcast_matches_original_and_targets_are_separate(self):
        torch.manual_seed(2);old=NewsGAT(3,4,text_dim=5,hidden=16,dropout=0).eval()
        new=TargetNewsGAT(3,4,text_dim=5,hidden=16,dropout=0).eval();new.load_state_dict(old.state_dict())
        text=torch.randn(1,5);sent=torch.randn(1,3);mentions=torch.zeros(1,3,5);mentions[:,:,0]=1
        static=torch.randn(3,4);edges=torch.empty(2,0,dtype=torch.long);attrs=torch.empty(0,11)
        with torch.inference_mode():
            a=old(text,sent,mentions,static,edges,attrs)
            expanded=text[:,None,:].expand(-1,3,-1).clone();ss=sent[:,None,:].expand(-1,3,-1)
            b=new(expanded,ss,mentions,static,edges,attrs)
            # GEMM uses a different leading batch shape; allow float32 roundoff.
            for x,y in zip(a,b):torch.testing.assert_close(x,y,rtol=1e-5,atol=1e-6)
            expanded[:,0]+=2;c=new(expanded,ss,mentions,static,edges,attrs)
            self.assertGreater((c[0][:,0]-b[0][:,0]).abs().max().item(),1e-5)
            torch.testing.assert_close(c[0][:,1:],b[0][:,1:],rtol=0,atol=0)


if __name__=='__main__':unittest.main()
