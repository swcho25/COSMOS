"""Ensure graph rebuilding cannot admit future/heldout news or price sessions."""
import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from experiment_current_events import clean_graph


class CurrentGraphTest(unittest.TestCase):
    def test_future_heldout_news_and_prices_cannot_change_snapshot(self):
        meta=pd.DataFrame(dict(ticker=['A','B','C'],market=['KOSPI','KOSPI','NASDAQ']))
        days=pd.bdate_range('2019-01-01','2019-12-31');x=np.arange(len(days))
        prices=pd.DataFrame(dict(A=np.exp(.001*x+.01*np.sin(x)),B=np.exp(.001*x+.02*np.sin(x)),C=np.exp(.001*x)),index=days)
        def article(i,t,split='train'):
            return dict(record_id=str(i),published_date=t[:10],available_at=t,split=split,title='A B',companies=[dict(ticker='A'),dict(ticker='B')])
        old=[article(i,'2019-05-01T15:00:00+00:00') for i in range(3)]
        initial=clean_graph('2020-01-01',old,meta,prices)
        poison=old+[article(4,'2020-01-01T15:00:00+00:00'),article(5,'2019-04-01T15:00:00+00:00','val')]
        future=pd.DataFrame(dict(A=[1.],B=[1e9],C=[1.]),index=pd.to_datetime(['2020-01-02']))
        self.assertEqual(initial,clean_graph('2020-01-01',poison,meta,pd.concat([prices,future])))
        self.assertTrue(any(e['type']=='co_mention' for e in initial['edges']))
        for e in initial['edges']:
            self.assertIn(e['type'],('co_mention','correlation'))
            self.assertNotIn('C',(e['src'],e['dst']))


if __name__=='__main__':unittest.main()
