"""Train-only targets, frozen-feature dataset and validation-selected GAT ensembles.

The three-seed joint direction/magnitude learner is callable through fit_gat.
Baseline models, shared evaluation and the command-line pipeline follow next.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import torch
from scipy.stats import spearmanr
from sklearn.decomposition import PCA
from torch.nn import functional as F

from news_impact_data import OUT, graph_arrays, mention_array, node_features
from news_impact_model import NewsGAT


def targets(y, train):
    thresholds = np.quantile(abs(y[train]), 0.33, axis=0)
    scales = np.maximum(np.quantile(abs(y[train]), 0.95, axis=0), 1e-6)
    labels = np.where(y > thresholds, 2, np.where(y < -thresholds, 0, 1)).astype("int64")
    magnitude = np.minimum(abs(y) / scales, 1).astype("float32")
    return labels, magnitude, thresholds.astype("float32"), scales.astype("float32")




class TrainingData:
    def __init__(self, out=OUT):
        self.out = Path(out)
        from news_text_features import feature_manifest
        saved_manifest = json.loads((self.out / "feature_manifest.json").read_text())
        if saved_manifest != feature_manifest(self.out):
            raise ValueError("Text features are stale; rerun news_text_features.py for the current articles/preprocessing")
        self.docs = [json.loads(l) for l in (self.out / "articles.jsonl").open(encoding="utf8")]
        self.pairs = pd.read_parquet(self.out / "pairs.parquet")
        self.meta = pd.read_json(self.out / "companies.json", dtype={"ticker": str})
        self.tickers = self.meta.ticker.tolist()
        self.graphs = json.loads((self.out / "graphs.json").read_text(encoding="utf8"))
        self.sentiment = np.load(self.out / "sentiment.npy")
        embedding = np.load(self.out / "embeddings.npy")
        article_train = np.array([r["split"] == "train" for r in self.docs])
        pca = PCA(n_components=min(64, embedding.shape[1], int(article_train.sum())), random_state=42)
        pca.fit(embedding[article_train])
        self.text = pca.transform(embedding).astype("float32")
        np.savez(self.out / "text_projection.npz", mean=pca.mean_, components=pca.components_)
        self.mentions = np.stack([mention_array(r["companies"], self.tickers) for r in self.docs])
        self.static = {k: node_features(self.meta, g) for k, g in self.graphs.items()}
        self.graph_arrays = {k: graph_arrays(g, self.tickers) for k, g in self.graphs.items()}
        self.y = self.pairs[["y_1d", "y_3d"]].to_numpy(dtype="float32")
        self.train = (self.pairs.split == "train").to_numpy()
        self.val = (self.pairs.split == "val").to_numpy()
        self.test = (self.pairs.split == "test").to_numpy()
        self.cls, self.mag, self.thresholds, self.scales = targets(self.y, self.train)
        self.node_idx = self.pairs.ticker.map({t: i for i, t in enumerate(self.tickers)}).to_numpy()
        self.article_idx = self.pairs.article.to_numpy()
        shape = (len(self.docs), len(self.tickers), 2)
        self.dense_cls = np.zeros(shape, dtype="int64")
        self.dense_mag = np.zeros(shape, dtype="float32")
        self.mask = np.zeros(shape[:2], dtype="bool")
        self.dense_cls[self.article_idx, self.node_idx] = self.cls
        self.dense_mag[self.article_idx, self.node_idx] = self.mag
        self.mask[self.article_idx, self.node_idx] = True





def loss_fn(logits, pred_mag, cls, mag, mask):
    ce = F.cross_entropy(logits.reshape(-1, 3), cls.reshape(-1), reduction="none").reshape(*cls.shape)
    err = F.smooth_l1_loss(pred_mag, mag, reduction="none")
    # Equal article weighting, both horizons jointly; no masked rows affect loss.
    per_node = (ce + err).mean(-1) * mask
    return (per_node.sum(-1) / mask.sum(-1).clamp(min=1)).mean()


def batches(data, split, batch_size, rng=None):
    keys = sorted({r["graph"] for r in data.docs if r["split"] == split})
    result = []
    for key in keys:
        ids = np.array([i for i, r in enumerate(data.docs) if r["split"] == split and r["graph"] == key])
        if rng is not None:
            rng.shuffle(ids)
        result.extend((key, ids[i:i + batch_size]) for i in range(0, len(ids), batch_size))
    if rng is not None:
        rng.shuffle(result)
    return result


def fit_gat(data, seeds=3, epochs=30, patience=6, batch_size=24, device="cuda"):
    dev = torch.device(device)
    ts = lambda a, dtype=None: torch.as_tensor(a, dtype=dtype, device=dev)
    text, sentiment, mentions = ts(data.text), ts(data.sentiment), ts(data.mentions)
    static = {k: ts(v) for k, v in data.static.items()}
    edges = {k: (ts(e), ts(a)) for k, (e, a) in data.graph_arrays.items()}
    cls, mag, mask = ts(data.dense_cls), ts(data.dense_mag), ts(data.mask)
    configs, states, history, predictions = [], [], [], []
    config = dict(n_companies=len(data.tickers), static_dim=next(iter(data.static.values())).shape[-1],
                  text_dim=data.text.shape[-1], edge_dim=11)

    def run(model, split, optimizer=None, rng=None, predict=False):
        model.train(optimizer is not None)
        losses, sizes = [], []
        # Outputs are only retained for prediction; training never stores autograd graphs.
        result_p = np.empty((len(data.docs), len(data.tickers), 2, 3), dtype="float32") if predict else None
        result_m = np.empty((len(data.docs), len(data.tickers), 2), dtype="float32") if predict else None
        for key, ids in batches(data, split, batch_size, rng):
            ii = ts(ids)
            with torch.set_grad_enabled(optimizer is not None):
                logits, pred_mag = model(text[ii], sentiment[ii], mentions[ii], static[key], *edges[key])
                loss = loss_fn(logits, pred_mag, cls[ii], mag[ii], mask[ii])
                if optimizer is not None:
                    optimizer.zero_grad(set_to_none=True)
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 2.)
                    optimizer.step()
            losses.append(float(loss.detach().cpu())); sizes.append(len(ids))
            if predict:
                result_p[ids] = logits.softmax(-1).detach().cpu().numpy()
                result_m[ids] = pred_mag.detach().cpu().numpy()
        return float(np.average(losses, weights=sizes)), result_p, result_m

    for seed in range(seeds):
        torch.manual_seed(seed); np.random.seed(seed)
        if dev.type == "cuda":
            torch.cuda.manual_seed_all(seed)
        model = NewsGAT(**config).to(dev)
        optimizer = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-3)
        rng = np.random.default_rng(seed)
        best, best_epoch, state = float("inf"), -1, None
        for epoch in range(epochs):
            start = time.monotonic()
            train_loss, _, _ = run(model, "train", optimizer, rng)
            val_loss, _, _ = run(model, "val")
            history.append(dict(seed=seed, epoch=epoch, train_loss=train_loss, val_loss=val_loss,
                                seconds=time.monotonic() - start))
            print(json.dumps(history[-1]), flush=True)
            if val_loss < best - 1e-5:
                best, best_epoch = val_loss, epoch
                state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            elif epoch - best_epoch >= patience:
                break
        model.load_state_dict(state)
        states.append(state); configs.append(dict(seed=seed, best_epoch=best_epoch, val_loss=best))
        # Test first becomes visible ONLY after the checkpoint has been selected.
        result_p = np.zeros((len(data.pairs), 2, 3), dtype="float32")
        result_m = np.zeros((len(data.pairs), 2), dtype="float32")
        for split in ("train", "val", "test"):
            _, p, m = run(model, split, predict=True)
            selected = (data.pairs.split == split).to_numpy()
            a, n = data.article_idx[selected], data.node_idx[selected]
            result_p[selected], result_m[selected] = p[a, n], m[a, n]
        predictions.append((result_p, result_m))
        torch.save({"config": config, "state_dict": state, "selection": configs[-1]}, data.out / f"gat_seed{seed}.pt")
        pd.DataFrame(history).to_csv(data.out / "training_history.csv", index=False)
    return np.mean([p for p, _ in predictions], axis=0), np.mean([m for _, m in predictions], axis=0), configs


def rank_ic(x, y):
    if len(x) < 3 or np.ptp(x) < 1e-10 or np.ptp(y) < 1e-10:
        return None
    return float(spearmanr(x, y).statistic)


def common_rank_score(x, y):
    """Same label-eligible articles for every model; constant ranking = zero skill."""
    if len(y) < 3 or np.ptp(y) < 1e-10:
        return None
    ic = rank_ic(x, y)
    return ic if ic is not None else 0.
