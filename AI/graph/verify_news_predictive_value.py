"""Train-only market/ticker class proportions and median-strength baselines."""
import numpy as np
import pandas as pd


def priors(pairs, cls, mag):
    """All statistics use train rows only; test rows merely select a group."""
    train = pairs.split.eq('train').to_numpy()

    def stats(mask):
        return (np.stack([np.bincount(cls[mask, h], minlength=3) / mask.sum() for h in range(2)]),
                np.median(mag[mask], axis=0))

    global_prior = stats(train)
    market = {k: stats(train & pairs.market.eq(k).to_numpy()) for k in pairs.market[train].unique()}
    ticker = {k: stats(train & pairs.ticker.eq(k).to_numpy()) for k in pairs.ticker[train].unique()}
    result = {}
    for name, lookup in [('market_prior', market), ('ticker_prior', ticker)]:
        key = pairs.market if name == 'market_prior' else pairs.ticker
        pm = [lookup.get(k, market.get(m, global_prior)) for k, m in zip(key, pairs.market)]
        result[name] = (np.array([p for p, _ in pm], dtype='float32'),
                        np.array([m for _, m in pm], dtype='float32'))
    return result
