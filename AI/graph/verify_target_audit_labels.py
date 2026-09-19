"""Recompute 100 existing delayed labels from prices/index levels independently."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from news_impact_data import close_times
from verify_news_predictive_value import dump,sha

HERE=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--audit',type=Path,required=True);args=ap.parse_args();out=args.audit
    reviews=pd.read_csv(out/'reviewed_100.csv',dtype={'ticker':str})
    price_path=HERE/'data/prices_daily.parquet';prices=pd.read_parquet(price_path)
    source=HERE/'artifacts/news_impact_v2';errors=[];times=[];price_hash=sha(price_path)
    for market,group in reviews.groupby('market'):
        p=prices[prices.market.eq(market)].pivot(index='trading_at',columns='ticker',values='adj_close').sort_index()
        p.index=pd.to_datetime(p.index);closes=close_times(p.index,market)
        f=pd.read_csv(source/f'index_{market}.csv');index=pd.Series(f.Close.to_numpy(),index=pd.to_datetime(f.Date.str[:10])).reindex(p.index)
        for r in group.itertuples():
            available=pd.Timestamp(r.available_at);anchor=int(closes.searchsorted(available,side='right'))
            assert p.index[anchor].strftime('%Y-%m-%d')==r.anchor_date
            assert p.index[anchor+3].strftime('%Y-%m-%d')==r.label_end
            assert closes[anchor]>available
            values=[]
            for h in (1,3):
                value=np.log(p[r.ticker].iloc[anchor+h]/p[r.ticker].iloc[anchor])-np.log(index.iloc[anchor+h]/index.iloc[anchor])
                expected=getattr(r,f'y_{h}d');np.testing.assert_allclose(value,expected,atol=1e-7,rtol=1e-6)
                values.append(float(abs(value-expected)))
            errors.extend(values);times.append(float((closes[anchor]-available).total_seconds()/3600))
    assert sha(price_path)==price_hash
    result=dict(pairs=len(reviews),label_values=len(errors),max_label_error=max(errors),
        availability_to_anchor_hours_min=min(times),availability_to_anchor_hours_median=float(np.median(times)),
        availability_to_anchor_hours_max=max(times),source_price_unchanged=True,
        notice='Price arithmetic and alignment verified; does not prove causal news labels or original publication timestamps.')
    dump(out/'label_verification.json',result);print(json.dumps(result))


if __name__=='__main__':main()
