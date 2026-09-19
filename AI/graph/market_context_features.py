"""Past-only market context for the delayed-return task, without changing labels.

Each row uses completed market sessions whose actual exchange close plus a fixed
15-minute ingestion buffer is <= the article's conservative availability. Windows
are trailing, never forward-filled. Missing values have explicit indicators.
The stored adjusted history can contain vendor revisions; this is a research
as-of cutoff on the downloaded history, not a historical real-time vendor archive.
"""
import numpy as np
import pandas as pd
from news_impact_data import close_times

FEATURE_NAMES=[f'{kind}_return_{h}' for kind in ('stock','market','excess') for h in (1,5,20)]+['stock_vol_20','market_vol_20','drawdown_60']
BUFFER=pd.Timedelta(minutes=15)


def trailing_features(price,index):
    """Return [session, company, 12] from levels; NaN remains missing."""
    log=np.log(price.where(price>0));benchmark=np.log(index.where(index>0))
    stock=[log-log.shift(h) for h in (1,5,20)]
    market=[benchmark-benchmark.shift(h) for h in (1,5,20)]
    broadcast=lambda s:np.repeat(s.to_numpy()[:,None],len(price.columns),axis=1)
    values=[v.to_numpy() for v in stock]+[broadcast(v) for v in market]
    values += [s.sub(m,axis=0).to_numpy() for s,m in zip(stock,market)]
    values += [log.diff().rolling(20,min_periods=20).std(ddof=0).to_numpy(),
               broadcast(benchmark.diff().rolling(20,min_periods=20).std(ddof=0)),
               (log-log.rolling(60,min_periods=60).max()).to_numpy()]
    return np.stack(values,axis=-1).astype('float32')


def asof_positions(closes,availability):
    closes=pd.DatetimeIndex(closes);availability=pd.DatetimeIndex(availability)
    return (closes+BUFFER).searchsorted(availability,side='right')-1


def build_context(docs,meta,prices,benchmarks):
    n=len(docs);k=len(meta);raw=np.full((n,k,len(FEATURE_NAMES)),np.nan,dtype='float32')
    age=np.zeros((n,k,1),dtype='float32');cutoffs=[]
    selected=np.array([i for i,d in enumerate(docs) if d['split'] in ('train','val')])
    available=pd.DatetimeIndex([pd.Timestamp(docs[i]['available_at']) for i in selected])
    for market,group in meta.groupby('market'):
        p=prices[prices.market.eq(market)].pivot(index='trading_at',columns='ticker',values='adj_close').sort_index()
        p.index=pd.to_datetime(p.index);p=p.reindex(columns=group.ticker)
        closes=close_times(p.index,market);pos=asof_positions(closes,available)
        values=trailing_features(p,benchmarks[market].reindex(p.index));valid=pos>=0;ids=selected[valid];positions=pos[valid]
        ni=group.index.to_numpy();raw[ids[:,None],ni[None,:]]=values[positions]
        lag=np.asarray((available[valid]-closes[positions]).total_seconds()/86400,dtype='float32')
        assert np.all(closes[positions]+BUFFER<=available[valid])
        age[ids[:,None],ni[None,:],0]=np.minimum(lag[:,None],30)/7
        cutoffs.extend(dict(article=int(i),market=market,close_time=c.isoformat(),available_at=a.isoformat(),lag_hours=float(h*24)) for i,c,a,h in zip(ids,closes[positions],available[valid],lag))
    train=np.array([d['split']=='train' for d in docs]);valid=np.isfinite(raw)
    # Fit normalization on train only. Multiplicity is article/node weighting.
    mean=np.nanmean(raw[train],axis=(0,1));std=np.nanstd(raw[train],axis=(0,1));std=np.maximum(std,1e-6)
    assert np.isfinite(mean).all() and np.isfinite(std).all()
    scaled=np.where(valid,(raw-mean)/std,0).clip(-8,8).astype('float32')
    result=np.concatenate([scaled,valid.astype('float32'),age],axis=-1)
    return result,dict(mean=mean,std=std),cutoffs
