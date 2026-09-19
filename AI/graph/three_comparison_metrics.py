"""Frozen-population direction weights for the final three GNN comparisons.

The training intervention changes direction CE only. Within each of the two
markets and two horizons, every observed training anchor date has equal mass,
and that day's eligible article-company pairs share its mass. A weight of
N / (4 * number_of_dates * number_of_pairs_on_date), followed by division by
the batch's ARTICLE count, preserves a unit direction objective when averaging
over N training articles. Magnitude keeps the existing equal-article SmoothL1.
No outcome values, validation frequencies or future rows determine train mass.

Checkpoint selection uses the original direct validation population, including
the 70 hygiene abstentions. Retained predictions receive each original day's
denominator; absent predictions contribute zero instead of disappearing from
the score. This is the existing equal-market/horizon/session accuracy goal,
not micro accuracy, balanced-class accuracy or a changed prediction target.
"""
from __future__ import annotations

import numpy as np
import torch
from torch.nn import functional as F

MARKETS = ("KOSPI", "NASDAQ")
HORIZONS = 2


def _pair_positions(data):
    mask = np.asarray(data.mask)
    article = np.asarray(data.article_idx)
    node = np.asarray(data.node_idx)
    if mask.ndim != 2 or mask.shape[0] != len(data.docs):
        raise ValueError("mask must have shape [article, node]")
    if article.shape != (len(data.pairs),) or node.shape != article.shape:
        raise ValueError("pair position arrays must align with data.pairs")
    if not np.issubdtype(article.dtype, np.integer) or not np.issubdtype(node.dtype, np.integer):
        raise ValueError("pair article/node positions must be integers")
    if ((article < 0).any() or (article >= mask.shape[0]).any()
            or (node < 0).any() or (node >= mask.shape[1]).any()):
        raise ValueError("pair article/node position out of bounds")
    return mask.astype(bool, copy=False), article, node


def _market_date_weights(frame, numerator):
    """Count rows only; outcome/label columns are deliberately never accessed."""
    if frame.empty or frame[["market", "anchor_date"]].isna().any().any():
        raise ValueError("eligible pairs must have market and anchor_date")
    if not set(frame.market.unique()).issubset(MARKETS):
        raise ValueError("unexpected market in eligible population")
    result = np.zeros(len(frame), dtype=np.float64)
    for market in MARKETS:
        selected = frame.market.eq(market).to_numpy()
        if not selected.any():
            raise ValueError(f"no eligible dates for {market}")
        dates = frame.loc[selected, "anchor_date"]
        day_counts = dates.value_counts(dropna=False)
        result[selected] = numerator / (len(MARKETS) * HORIZONS * len(day_counts)
                                         * dates.map(day_counts).to_numpy())
    return result


def make_train_weights(data):
    """Return float32 [article,node,2]; inactive articles/nodes get zero.

    The sum is N up to float32 rounding, where N is the number of documents
    whose split is train. Each market/horizon owns N/4 mass. Every active mask
    entry must correspond to exactly one direct training pair. Both horizons
    are jointly eligible in the frozen dataset's shared 2-D direct mask.
    """
    mask, article, node = _pair_positions(data)
    active = np.asarray([d["split"] == "train" for d in data.docs], dtype=bool)
    n_articles = int(active.sum())
    if not n_articles or not mask[active].any(axis=1).all():
        raise ValueError("every active training article needs an eligible label")
    selected = active[article] & mask[article, node]
    frame = data.pairs.loc[selected, ["market", "anchor_date", "split", "is_direct"]]
    if not frame.split.eq("train").all() or not frame.is_direct.astype(bool).all():
        raise ValueError("active training labels must be direct training pairs")
    positions = article[selected] * mask.shape[1] + node[selected]
    if len(np.unique(positions)) != len(positions) or len(positions) != int(mask[active].sum()):
        raise ValueError("every active training mask entry needs one unique pair")
    per_pair = _market_date_weights(frame, float(n_articles))
    weights = np.zeros((*mask.shape, HORIZONS), dtype=np.float32)
    weights[article[selected], node[selected], :] = per_pair[:, None]
    return weights


def make_selection_weights(data, indices):
    """Return float64 [retained_pair,2], preserving full direct-val denominators.

    ``indices`` is an ordered unique subset of the original direct val rows;
    each must still have an eligible mask and an active val document. All
    direct val rows participate in the market/date counts, even if excluded
    from batching. The total retained weight can consequently be less than 1.
    """
    mask, article, node = _pair_positions(data)
    indices = np.asarray(indices)
    if indices.ndim != 1 or not np.issubdtype(indices.dtype, np.integer):
        raise ValueError("selection indices must be a one-dimensional integer array")
    if (not len(indices) or len(np.unique(indices)) != len(indices)
            or (indices < 0).any() or (indices >= len(data.pairs)).any()):
        raise ValueError("selection indices must be unique valid pair positions")
    full = data.pairs.split.eq("val").to_numpy() & data.pairs.is_direct.to_numpy(dtype=bool)
    if not full[indices].all() or not mask[article[indices], node[indices]].all():
        raise ValueError("selection indices must be retained direct val pairs")
    if not all(data.docs[int(i)]["split"] == "val" for i in article[indices]):
        raise ValueError("selection indices contain an inactive val article")
    full_ids = np.flatnonzero(full)
    per_pair = _market_date_weights(data.pairs.loc[full, ["market", "anchor_date"]], 1.)
    positions = np.searchsorted(full_ids, indices)
    return np.repeat(per_pair[positions, None], HORIZONS, axis=1)


def score_direction(probabilities, cls, weights):
    """Weighted argmax accuracy as a float; abstention mass is never normalized."""
    p = np.asarray(probabilities)
    actual = np.asarray(cls)
    w = np.asarray(weights, dtype=np.float64)
    if p.ndim != 3 or p.shape[1:] != (HORIZONS, 3) or actual.shape != p.shape[:2] or w.shape != actual.shape:
        raise ValueError("expected probabilities [pair,2,3], classes/weights [pair,2]")
    if (not np.isfinite(p).all() or (p < 0).any() or (p > 1).any()
            or not np.allclose(p.sum(-1), 1., rtol=0, atol=1e-5)):
        raise ValueError("probabilities must be finite normalized distributions")
    if not np.isin(actual, (0, 1, 2)).all():
        raise ValueError("classes must be 0, 1 or 2")
    if not np.isfinite(w).all() or (w < 0).any() or w.sum() > 1. + 1e-8:
        raise ValueError("selection weights must be nonnegative with total mass <= 1")
    return float(np.sum((p.argmax(-1) == actual) * w, dtype=np.float64))


def balanced_direction_loss(logits, pred, cls, mag, mask, weights):
    """Reweight CE by train market/date; keep original magnitude loss intact.

    Inputs are tensors for one batch. ``weights`` is the corresponding slice of
    ``make_train_weights`` moved to the training device. Division is by article
    count, not the batch's sum of weights; self-normalizing each batch would
    cancel part of the intended market/date intervention. Masked nodes remain
    zero even if a caller supplies nonzero weights for them.
    """
    if cls.ndim != 3 or cls.shape[-1] != HORIZONS or not cls.shape[0]:
        raise ValueError("classes must have nonempty shape [article,node,2]")
    if (logits.shape != (*cls.shape, 3) or pred.shape != cls.shape
            or mag.shape != cls.shape or weights.shape != cls.shape or mask.shape != cls.shape[:2]):
        raise ValueError("loss tensor shapes do not align")
    ce = F.cross_entropy(logits.reshape(-1, 3), cls.reshape(-1), reduction="none").reshape_as(cls)
    direction = (ce * weights * mask.unsqueeze(-1)).sum() / cls.shape[0]
    err = F.smooth_l1_loss(pred, mag, reduction="none")
    magnitude = ((err.mean(-1) * mask).sum(-1) / mask.sum(-1).clamp(min=1)).mean()
    return direction + magnitude
