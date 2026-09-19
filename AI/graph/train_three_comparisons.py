"""Paired checkpoint selectors for fixed-trajectory GAT comparison arms.

Each call trains three seeds for one root-specified arm. Inter-company edge
removal keeps every node, static feature, input, candidate and GAT self-loop.
Direction balancing changes only training CE; validation always uses the old
unweighted CE + magnitude loss. The legacy loss selector alone controls stopping
(30 epochs, min_delta 1e-5, patience 6). The accuracy selector observes exactly
the same candidate epochs and selects the earliest improvement above 1e-8.

Validation probabilities and magnitude are collected in the same forward pass
as validation loss. Computing accuracy does not draw RNG, change model state or
extend training. Both selector outputs retain identical original pair indices;
selection weights account for original-population hygiene abstentions as wrong.
No test prediction, feature refit, model promotion or source mutation occurs.
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from target_news_model import TargetNewsGAT
from three_comparison_metrics import (
    balanced_direction_loss,
    make_selection_weights,
    make_train_weights,
    score_direction,
)
from train_news_impact import batches, loss_fn


SELECTORS = ('loss', 'accuracy')


def fit_comparison(data, mapping, text_features, sent_features, out, indices, *,
                   remove_edges=False, balance_direction=False):
    """Save both three-seed ensembles and return selector -> selections lists."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for path in [out/'training_history.csv', out/'selections.json'] + [out/s for s in SELECTORS]:
        if path.exists():
            raise FileExistsError(f'Refusing to overwrite comparison output: {path}')
    for selector in SELECTORS:
        (out/selector).mkdir()

    indices = np.asarray(indices, dtype=np.int64)
    assert indices.ndim == 1 and len(indices) and np.all(indices[1:] > indices[:-1])
    selection_weights = make_selection_weights(data, indices)
    actual = np.asarray(data.cls)[indices]
    assert selection_weights.shape == actual.shape == (len(indices), 2)
    assert np.isfinite(selection_weights).all() and (selection_weights >= 0).all()

    ts = lambda x: torch.as_tensor(x, device='cuda')
    mapping, text, sent, mentions = map(ts, (mapping, text_features, sent_features, data.mentions))
    static = {key: ts(value) for key, value in data.static.items()}
    edges = {}
    for key, (edge_index, edge_attr) in data.graph_arrays.items():
        if remove_edges:
            edge_index = np.empty((2, 0), dtype=np.int64)
            edge_attr = np.empty((0, 11), dtype=np.float32)
        edges[key] = (ts(edge_index), ts(edge_attr))
    cls, mag, mask = map(ts, (data.dense_cls, data.dense_mag, data.mask))
    train_weights = ts(make_train_weights(data)) if balance_direction else None
    if train_weights is not None:
        assert train_weights.shape == cls.shape

    # Same architecture and optimizer defaults as train_target_news.fit_target.
    config = dict(n_companies=len(data.tickers), static_dim=next(iter(data.static.values())).shape[-1],
                  text_dim=text_features.shape[-1], edge_dim=11, hidden=64, dropout=.15)
    history = []
    selections = {selector: [] for selector in SELECTORS}
    probabilities = {selector: [] for selector in SELECTORS}
    magnitudes = {selector: [] for selector in SELECTORS}
    article_rows = {}
    for row, pair_index in enumerate(indices):
        article_rows.setdefault(int(data.article_idx[pair_index]), []).append(row)
    article_rows = {article: np.asarray(rows, dtype=np.int64) for article, rows in article_rows.items()}

    def forward(model, key, ids):
        ii = ts(ids)
        feature_ids = mapping[ii]
        return model(text[feature_ids], sent[feature_ids], mentions[ii], static[key], *edges[key])

    def run(model, split, optimizer=None, rng=None):
        training = optimizer is not None
        model.train(training)
        total = 0.
        count = 0
        if not training:
            assert split == 'val'
            p = np.full((len(indices), 2, 3), np.nan, dtype=np.float32)
            m = np.full((len(indices), 2), np.nan, dtype=np.float32)
            seen = np.zeros(len(indices), dtype=np.int64)
        else:
            p = m = None
        for key, ids in batches(data, split, 24, rng):
            with torch.set_grad_enabled(training):
                logits, pred = forward(model, key, ids)
                ii = ts(ids)
                if training and balance_direction:
                    loss = balanced_direction_loss(logits, pred, cls[ii], mag[ii], mask[ii], train_weights[ii])
                else:
                    # Deliberately unchanged even for a balanced-training arm.
                    loss = loss_fn(logits, pred, cls[ii], mag[ii], mask[ii])
                if training:
                    optimizer.zero_grad(set_to_none=True)
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 2.)
                    optimizer.step()
                else:
                    pp = logits.softmax(-1).cpu().numpy()
                    mm = pred.cpu().numpy()
                    for j, article in enumerate(ids):
                        rows = article_rows.get(int(article))
                        if rows is not None:
                            node_indices = data.node_idx[indices[rows]]
                            p[rows] = pp[j, node_indices]
                            m[rows] = mm[j, node_indices]
                            seen[rows] += 1
            total += float(loss.detach().cpu()) * len(ids)
            count += len(ids)
        assert count > 0, f'No {split} articles'
        value = total / count
        assert np.isfinite(value), f'Nonfinite {split} loss'
        if not training:
            assert (seen == 1).all(), 'Each validation pair must be collected exactly once'
            assert np.isfinite(p).all() and np.isfinite(m).all()
            np.testing.assert_allclose(p.sum(-1), 1, atol=1e-6)
        return value, p, m

    for seed in range(3):
        torch.manual_seed(seed)
        np.random.seed(seed)
        torch.cuda.manual_seed_all(seed)
        model = TargetNewsGAT(**config).cuda()
        optimizer = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-3)
        rng = np.random.default_rng(seed)
        best_loss = float('inf')
        best_loss_epoch = -1
        best_accuracy = float('-inf')
        selected = {}
        for epoch in range(30):
            start = time.monotonic()
            train_loss, _, _ = run(model, 'train', optimizer, rng)
            val_loss, p, m = run(model, 'val')
            accuracy = float(score_direction(p, actual, selection_weights))
            assert np.isfinite(accuracy) and 0 <= accuracy <= 1
            row = dict(seed=seed, epoch=epoch, train_loss=train_loss, val_loss=val_loss,
                       val_accuracy=accuracy, seconds=time.monotonic()-start)
            history.append(row)
            print(json.dumps(dict(mode=out.name, **row)), flush=True)

            improved = []
            if val_loss < best_loss - 1e-5:
                best_loss, best_loss_epoch = val_loss, epoch
                improved.append('loss')
            if accuracy > best_accuracy + 1e-8:
                best_accuracy = accuracy
                improved.append('accuracy')
            if improved:
                # A copied CPU snapshot is immutable; both selectors can share
                # it when they improve at the same epoch without extra forwards.
                state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
                for selector in improved:
                    selected[selector] = dict(state_dict=state, probabilities=p.copy(), magnitude=m.copy(),
                                              selection=dict(seed=seed, best_epoch=epoch, val_loss=val_loss,
                                                             val_accuracy=accuracy, selector=selector))
            pd.DataFrame(history).to_csv(out/'training_history.csv', index=False)
            # Only the original loss criterion controls candidate-epoch budget.
            # The stopping epoch remains a candidate for BOTH selectors.
            if epoch-best_loss_epoch >= 6:
                break

        for selector in SELECTORS:
            result = selected[selector]
            selection = dict(result['selection'], candidate_epochs=epoch+1,
                             stopping_rule='unweighted_composite_val_loss',
                             selection_min_delta=1e-5 if selector == 'loss' else 1e-8,
                             patience=6, max_epochs=30, tie_policy='earliest')
            selections[selector].append(selection)
            probabilities[selector].append(result['probabilities'])
            magnitudes[selector].append(result['magnitude'])
            folder = out/selector
            torch.save(dict(config=config, state_dict=result['state_dict'], selection=selection,
                            mode=out.name, selector=selector, remove_edges=bool(remove_edges),
                            balance_direction=bool(balance_direction),
                            loss='balanced_direction_article_magnitude' if balance_direction else 'direct_only'),
                       folder/f'gat_seed{seed}.pt')
            np.savez_compressed(folder/f'validation_seed{seed}.npz', pair_indices=indices,
                                probabilities=result['probabilities'], magnitude=result['magnitude'])
        (out/'selections.json').write_text(json.dumps(selections, ensure_ascii=False, indent=2), encoding='utf8')
        del model, optimizer, selected

    for selector in SELECTORS:
        # Same float32 seed mean as the existing trainer and verifier contract.
        p = np.mean(probabilities[selector], axis=0)
        m = np.mean(magnitudes[selector], axis=0)
        np.savez_compressed(out/selector/'validation_predictions.npz', pair_indices=indices,
                            probabilities=p, magnitude=m)
    return selections
