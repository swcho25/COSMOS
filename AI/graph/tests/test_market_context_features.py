"""Leakage guards: future price mutations, exchange cutoff and missing sessions."""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
import numpy as np
import pandas as pd
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from market_context_features import trailing_features,asof_positions,build_context
from target_news_model import TargetNewsGAT


class PastContextTests(unittest.TestCase):
    def test_article_specific_price_context_does_not_cross_batch(self):
        torch.manual_seed(3);model=TargetNewsGAT(3,29,text_dim=5,hidden=16,dropout=0).eval()
        text=torch.randn(2,3,5);sent=torch.randn(2,3,3);mentions=torch.zeros(2,3,5);mentions[:,:,0]=1
        context=torch.randn(2,3,29);edges=torch.empty(2,0,dtype=torch.long);attrs=torch.empty(0,11)
        with torch.inference_mode():
            before=model(text,sent,mentions,context,edges,attrs)[0]
            context[0,:,4:]+=2;after=model(text,sent,mentions,context,edges,attrs)[0]
        torch.testing.assert_close(before[1],after[1],atol=0,rtol=0)
        self.assertGreater((before[0]-after[0]).abs().max().item(),1e-5)

    def test_future_prices_cannot_change_past_features(self):
        dates=pd.bdate_range('2022-01-03',periods=100)
        price=pd.DataFrame({'A':np.exp(np.arange(100)*.01),'B':np.exp(np.arange(100)*.003)},index=dates)
        index=pd.Series(np.exp(np.arange(100)*.002),index=dates)
        expected=trailing_features(price,index)
        price.iloc[70:]*=100;index.iloc[70:]*=.01
        np.testing.assert_array_equal(expected[:70],trailing_features(price,index)[:70])
        np.testing.assert_allclose(expected[69,0,8],(.01-.002)*20,rtol=1e-6)

    def test_close_and_ingestion_buffer(self):
        # US DST/early close are supplied as actual UTC closes.
        closes=pd.to_datetime(['2023-03-10T21:00Z','2023-03-13T20:00Z','2023-11-24T18:00Z'])
        times=pd.to_datetime(['2023-03-10T21:14:59Z','2023-03-10T21:15Z','2023-03-13T20:14Z','2023-11-24T18:15Z'],format='mixed')
        np.testing.assert_array_equal(asof_positions(closes,times),[-1,0,0,2])

    def test_missing_price_is_not_forward_filled(self):
        dates=pd.bdate_range('2022-01-03',periods=80);price=pd.DataFrame({'A':np.arange(80)+100.},index=dates)
        index=pd.Series(np.arange(80)+1000.,index=dates);price.iloc[60]=np.nan
        x=trailing_features(price,index)
        self.assertTrue(np.isnan(x[60,0,0]));self.assertTrue(np.isnan(x[61,0,0]));self.assertTrue(np.isnan(x[65,0,9]))

    def test_validation_cannot_fit_normalizer_or_change_training_input(self):
        dates=pd.bdate_range('2022-01-03',periods=80);closes=dates.tz_localize('UTC')+pd.Timedelta(hours=6,minutes=30)
        docs=[dict(split=s,available_at=(closes[j]+pd.Timedelta(minutes=20)).isoformat()) for s,j in [('train',69),('val',79)]]
        meta=pd.DataFrame(dict(ticker=['A'],market=['KOSPI']))
        prices=pd.DataFrame(dict(ticker='A',market='KOSPI',trading_at=dates,adj_close=np.arange(80)+100.))
        index=pd.Series(np.arange(80)+1000.,index=dates)
        with patch('market_context_features.close_times',return_value=closes):
            first,scale,_=build_context(docs,meta,prices,dict(KOSPI=index))
            prices.loc[70:,'adj_close']*=5;index.iloc[70:]*=2
            second,changed,_=build_context(docs,meta,prices,dict(KOSPI=index))
        np.testing.assert_array_equal(first[0],second[0])
        for key in ('mean','std'):np.testing.assert_array_equal(scale[key],changed[key])
        self.assertFalse(np.array_equal(first[1],second[1]))


if __name__=='__main__':unittest.main()
