"""Independent data, weights, selectors, CPU replay and statistics verification.

Reuse the frozen augmentation verifier for data lineage, then independently
derive training CE and original-population accuracy weights from actual rows.
No training code/metric weight helper is used for expected weights or scores.
Reload 30 selected checkpoints on CPU for six validation articles, replay both
selectors from their shared trajectories, and reconstruct each selected epoch's
accuracy and unweighted loss from all saved validation predictions. Predictions
for unselected epochs were not stored: their history accuracy cannot be fully
recomputed, and selector replay alone does not independently prove those values.
All HAC comparisons use 4,813 original validation pairs, including 70 misses.
No test predictions, original source edits, model promotion or GPU use occur.
"""

import argparse
import collections
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from scipy.stats import norm

from news_impact_data import graph_arrays, node_features
from target_news_model import TargetNewsGAT
from verify_news_augmentation import read_and_check as verify_augmentation
from verify_news_predictive_value import dump, sha


HERE = Path(__file__).resolve().parent
ARMS = ('standard', 'no_edges', 'no_content', 'balanced', 'balanced_no_content')
SELECTORS = ('loss', 'accuracy')
BASELINES = ('original', 'ticker_prior', 'majority')
CONTRASTS = [
    ('graph', 'standard_loss', 'no_edges_loss'),
    ('selection_content', 'standard_accuracy', 'standard_loss'),
    ('selection_no_content', 'no_content_accuracy', 'no_content_loss'),
    ('weight_content', 'balanced_accuracy', 'standard_accuracy'),
    ('weight_no_content', 'balanced_no_content_accuracy', 'no_content_accuracy'),
    ('news_standard', 'standard_loss', 'no_content_loss'),
    ('news_accuracy', 'standard_accuracy', 'no_content_accuracy'),
    ('news_balanced', 'balanced_accuracy', 'balanced_no_content_accuracy'),
]


def hashes_unchanged(out, protocol, prepared):
    for path, digest in protocol['source_hashes'].items():
        assert sha(Path(path)) == digest, path
    for name, digest in protocol['code_hashes'].items():
        assert sha(HERE / name) == digest, name
    for name, digest in prepared['artifact_hashes'].items():
        assert sha(out / name) == digest, name


def independent_weights(s):
    pairs, docs = s['pairs'], s['docs']
    active = np.array([d['split'] == 'train' for d in docs])
    n_articles = int(active.sum())
    train_ids = np.flatnonzero(pairs.split.eq('train').to_numpy() & active[s['article']]
                              & pairs.is_direct.to_numpy() & s['mask'][s['article'], s['node']])
    train = pairs.iloc[train_ids]
    assert not train.duplicated(['article', 'ticker']).any()
    assert train.article.nunique() == n_articles
    expected = np.zeros((*s['mask'].shape, 2), dtype=np.float32)
    totals = []
    for market in ('KOSPI', 'NASDAQ'):
        market_rows = train.loc[train.market.eq(market)]
        days = market_rows.anchor_date.unique()
        assert len(days)
        for _, group in market_rows.groupby('anchor_date'):
            value = n_articles / (4 * len(days) * len(group))
            ii = group.index.to_numpy()
            expected[s['article'][ii], s['node'][ii], :] = value
        for horizon in range(2):
            ids = market_rows.index.to_numpy()
            total = expected[s['article'][ids], s['node'][ids], horizon].sum(dtype=np.float64)
            np.testing.assert_allclose(total, n_articles / 4, rtol=2e-7)
            totals.append(dict(market=market, horizon=(1, 3)[horizon], days=len(days),
                               pairs=len(ids), coefficient_sum=float(total)))
    assert np.count_nonzero(expected) == len(train_ids) * 2
    np.testing.assert_allclose(expected.sum(dtype=np.float64), n_articles, rtol=2e-7)
    full_ids, indices = s['full_ids'], s['indices']
    full = pairs.iloc[full_ids]
    all_weights = np.zeros((len(full_ids), 2), dtype=np.float64)
    for market in ('KOSPI', 'NASDAQ'):
        positions = np.flatnonzero(full.market.eq(market).to_numpy())
        market_rows = full.iloc[positions]
        days = market_rows.anchor_date.nunique()
        for day in market_rows.anchor_date.unique():
            rows = positions[market_rows.anchor_date.eq(day).to_numpy()]
            all_weights[rows] = 1 / (4 * days * len(rows))
        np.testing.assert_allclose(all_weights[positions].sum(axis=0), [.25, .25], atol=1e-14)
    positions = np.searchsorted(full_ids, indices)
    np.testing.assert_array_equal(full_ids[positions], indices)
    selected_weights = all_weights[positions]
    np.testing.assert_allclose(all_weights.sum(), 1, atol=1e-14)
    assert len(full_ids) - len(indices) == 70 and selected_weights.sum() < 1
    return expected, selected_weights, dict(training_articles=n_articles, training_pairs=len(train_ids),
        training_cell_totals=totals, all_original_selection_mass=float(all_weights.sum()),
        retained_selection_mass=float(selected_weights.sum()), missing_selection_mass=float(1-selected_weights.sum()),
        original_pairs=len(full_ids), retained_pairs=len(indices), missing_pairs=70)


def read_and_check(out):
    protocol = json.loads((out / 'protocol.json').read_text(encoding='utf8'))
    prepared = json.loads((out / 'prepared.json').read_text(encoding='utf8'))
    assert tuple(protocol['arms']) == ARMS and tuple(protocol['selectors']) == SELECTORS
    assert protocol['seeds'] == [0, 1, 2]
    assert protocol['correction_family'] == 68 and protocol['hac_lags'] == [5, 20]
    assert protocol['settings'] == dict(epochs=30, patience=6, loss_delta=1e-5,
        accuracy_delta=1e-8, batch_size=24, lr=.0008, weight_decay=.001, hidden=64, dropout=.15)
    assert protocol['statistical_population'] == 'original_direct_4813'
    assert protocol['validation_only'] and protocol['reused_validation']
    assert not protocol['automatic_model_replacement'] and not protocol['data_revision']
    assert protocol['user_target'] == .65
    hashes_unchanged(out, protocol, prepared)
    source = Path(protocol['source'])
    assert source.resolve() == (HERE / 'artifacts/news_augmentation_20260915_v2').resolve()
    source_result, s = verify_augmentation(source)
    tw, sw, weight_result = independent_weights(s)
    saved_tw = np.load(out / 'train_direction_weights.npy')
    saved_sw = np.load(out / 'selection_weights.npy')
    assert saved_tw.shape == tw.shape and saved_sw.shape == sw.shape
    assert np.isfinite(saved_tw).all() and np.isfinite(saved_sw).all()
    np.testing.assert_allclose(saved_tw, tw, atol=1e-7, rtol=1e-7)
    np.testing.assert_allclose(saved_sw, sw, atol=1e-15, rtol=1e-12)
    assert not saved_tw[~np.array([d['split']=='train' for d in s['docs']])].any()
    assert np.all(saved_tw[~s['mask']] == 0)
    weight_summary = json.loads((out / 'weight_summary.json').read_text(encoding='utf8'))
    assert weight_summary['train_articles'] == weight_result['training_articles']
    assert weight_summary['train_pairs'] == weight_result['training_pairs']
    assert weight_summary['original_direct_pairs'] == 4813 and weight_summary['retained_pairs'] == 4743
    assert weight_summary['train_weight_sum'] == float(saved_tw.sum(dtype=np.float64))
    assert weight_summary['selection_weight_sum'] == float(saved_sw.sum())
    assert weight_summary['train_weight_max'] == float(saved_tw.max())
    assert weight_summary['train_nonzero_values'] == int(np.count_nonzero(saved_tw))
    y = s['pairs'][['y_1d', 'y_3d']].to_numpy(dtype=np.float32)
    s['cls'] = np.where(y > s['threshold'], 2, np.where(y < -s['threshold'], 0, 1)).astype(np.int64)
    s['target_magnitude'] = np.minimum(abs(y) / s['scale'], 1).astype(np.float32)
    s.update(wrapper_protocol=protocol, wrapper_prepared=prepared, selection_weights=sw)
    result = dict(source_unchanged=True, source_full_verification=source_result,
        weight_verification=weight_result, training_weights_independently_reconstructed=True,
        selection_weights_independently_reconstructed=True, all_arms_same_source=True,
        no_new_test_predictions=True, validation_only=True)
    return result, s


def history_selections(history, seed):
    epochs = history.loc[history.seed.eq(seed)].sort_values('epoch')
    assert epochs.epoch.tolist() == list(range(len(epochs))) and 1 <= len(epochs) <= 30
    assert np.isfinite(epochs[['train_loss', 'val_loss', 'val_accuracy', 'seconds']]).all().all()
    assert epochs.val_accuracy.between(0, 1).all() and (epochs.seconds >= 0).all()
    best_loss, best_accuracy = float('inf'), float('-inf')
    selected = {}
    for row in epochs.itertuples():
        if row.val_loss < best_loss - 1e-5:
            best_loss = row.val_loss
            selected['loss'] = row
        if row.val_accuracy > best_accuracy + 1e-8:
            best_accuracy = row.val_accuracy
            selected['accuracy'] = row
        if row.epoch - selected['loss'].epoch >= 6:
            assert row.epoch == epochs.epoch.iloc[-1], 'Trajectory continued past loss patience'
    if len(epochs) < 30:
        assert epochs.epoch.iloc[-1] - selected['loss'].epoch >= 6
    return selected, len(epochs)


def unweighted_validation_loss(prob, mag, s):
    actual = s['cls'][s['indices']]
    ce = -np.log(np.take_along_axis(prob.astype(np.float64), actual[..., None], axis=-1)[..., 0])
    error = abs(mag.astype(np.float64) - s['target_magnitude'][s['indices']])
    huber = np.where(error < 1, .5 * error**2, error - .5)
    node_losses = (ce + huber).mean(axis=-1)
    return float(pd.Series(node_losses, index=s['article'][s['indices']]).groupby(level=0).mean().mean())


def read_predictions(path, indices):
    with np.load(path) as data:
        np.testing.assert_array_equal(data['pair_indices'], indices)
        p, m = data['probabilities'], data['magnitude']
    assert p.shape == (len(indices), 2, 3) and m.shape == (len(indices), 2)
    assert np.isfinite(p).all() and np.isfinite(m).all()
    assert (p >= 0).all() and (p <= 1).all() and (m >= 0).all() and (m <= 1).all()
    np.testing.assert_allclose(p.sum(axis=-1), 1, atol=1e-6)
    return p, m


def replay_checkpoints(out, s):
    indices, val = s['indices'], s['pairs'].iloc[s['indices']]
    article_ids = []
    for market in ('KOSPI', 'NASDAQ'):
        unique = sorted(set(val.loc[val.market.eq(market), 'article'].astype(int)))
        article_ids.extend((unique[0], unique[len(unique)//2], unique[-1]))
    assert len(set(article_ids)) == 6
    outputs = {}
    max_error = max_score_error = max_loss_error = 0.
    checkpoints = 0
    for arm in ARMS:
        folder = out / arm
        history = pd.read_csv(folder / 'training_history.csv')
        assert set(history.seed) == {0, 1, 2}
        selections = json.loads((folder / 'selections.json').read_text(encoding='utf8'))
        assert set(selections) == set(SELECTORS)
        text, sent = s['text'], s['sent']
        if arm in ('no_content', 'balanced_no_content'):
            text, sent = np.zeros_like(text), np.zeros_like(sent)
        for selector in SELECTORS:
            assert len(selections[selector]) == 3
            seed_probs, seed_mags = [], []
            for seed in range(3):
                selected, candidate_epochs = history_selections(history, seed)
                expected = selected[selector]
                checkpoint = torch.load(folder / selector / f'gat_seed{seed}.pt', map_location='cpu', weights_only=True)
                assert checkpoint['mode'] == arm and checkpoint['selector'] == selector
                assert checkpoint['remove_edges'] == (arm == 'no_edges')
                assert checkpoint['balance_direction'] == (arm in ('balanced', 'balanced_no_content'))
                assert checkpoint['loss'] == ('balanced_direction_article_magnitude'
                    if arm in ('balanced', 'balanced_no_content') else 'direct_only')
                assert checkpoint['config'] == dict(n_companies=len(s['tickers']),
                    static_dim=node_features(s['meta'], next(iter(s['graphs'].values()))).shape[-1],
                    text_dim=s['text'].shape[-1], edge_dim=11, hidden=64, dropout=.15)
                saved = checkpoint['selection']
                assert saved == selections[selector][seed]
                assert saved['seed'] == seed and saved['best_epoch'] == expected.epoch
                assert saved['candidate_epochs'] == candidate_epochs and saved['selector'] == selector
                assert saved['stopping_rule'] == 'unweighted_composite_val_loss'
                assert saved['patience'] == 6 and saved['max_epochs'] == 30 and saved['tie_policy'] == 'earliest'
                assert saved['selection_min_delta'] == (1e-5 if selector == 'loss' else 1e-8)
                np.testing.assert_allclose([saved['val_loss'], saved['val_accuracy']],
                                           [expected.val_loss, expected.val_accuracy], atol=1e-12)
                p, m = read_predictions(folder / selector / f'validation_seed{seed}.npz', indices)
                score = float(((p.argmax(-1) == s['cls'][indices]) * s['selection_weights']).sum())
                score_error = abs(score - saved['val_accuracy'])
                assert score_error < 1e-10, (arm, selector, seed, score_error)
                loss_error = abs(unweighted_validation_loss(p, m, s) - saved['val_loss'])
                assert loss_error < 2e-6, (arm, selector, seed, loss_error)
                max_score_error, max_loss_error = max(max_score_error, score_error), max(max_loss_error, loss_error)
                model = TargetNewsGAT(**checkpoint['config']).eval()
                model.load_state_dict(checkpoint['state_dict'])
                assert model.gat1.add_self_loops and model.gat2.add_self_loops
                for i in article_ids:
                    graph = s['graphs'][s['docs'][i]['graph']]
                    edge, attr = graph_arrays(graph, s['tickers'])
                    if arm == 'no_edges':
                        edge, attr = np.empty((2, 0), dtype=np.int64), np.empty((0, 11), dtype=np.float32)
                    feature = s['mapping'][i:i+1]
                    arrays = (text[feature], sent[feature], s['mentions'][i:i+1],
                              node_features(s['meta'], graph), edge, attr)
                    with torch.inference_mode():
                        logits, magnitude = model(*(torch.from_numpy(x) for x in arrays))
                        pp, mm = logits.softmax(-1)[0].numpy(), magnitude[0].numpy()
                    ii = np.flatnonzero(val.article.to_numpy() == i)
                    nodes = s['node'][indices[ii]]
                    for actual, expected_values in ((pp[nodes], p[ii]), (mm[nodes], m[ii])):
                        np.testing.assert_allclose(actual, expected_values, atol=2e-5, rtol=1e-4)
                        max_error = max(max_error, float(abs(actual - expected_values).max()))
                seed_probs.append(p); seed_mags.append(m); checkpoints += 1
            probability, magnitude = read_predictions(folder / selector / 'validation_predictions.npz', indices)
            np.testing.assert_array_equal(probability, np.mean(seed_probs, axis=0))
            np.testing.assert_array_equal(magnitude, np.mean(seed_mags, axis=0))
            outputs[f'{arm}_{selector}'] = (probability, magnitude)
    return dict(checkpoints=checkpoints, training_trajectories=15, cpu_validation_articles=6,
        cpu_forward_replays=checkpoints*6, max_cpu_prediction_error=max_error,
        selected_epoch_accuracy_max_error=max_score_error, selected_epoch_unweighted_loss_max_error=max_loss_error,
        earliest_selector_and_shared_epochs_verified=True, seed_ensembles_exact=True,
        unselected_epoch_accuracy_independently_recomputed=False,
        unselected_epoch_limit='Only trajectory history is available; full predictions were saved for selected epochs only'), outputs


def verify_metrics(out, s, outputs):
    indices, full_ids = s['indices'], s['full_ids']
    val, full = s['pairs'].iloc[indices], s['pairs'].iloc[full_ids]
    positions = np.searchsorted(full_ids, indices)
    baseline_paths = {'original': s['source'] / 'predictions_stage3.npz',
        'majority': s['source'] / 'predictions_majority.npz',
        'ticker_prior': HERE / 'artifacts/news_impact_v2_evidence/predictions_ticker_prior.npz'}
    full_correct = {}
    for name, (p, _) in outputs.items():
        correct = np.zeros((len(full_ids), 2), dtype=np.float64)
        correct[positions] = p.argmax(-1) == s['cls'][indices]
        full_correct[name] = correct
    for name, path in baseline_paths.items():
        assert sha(path) == s['wrapper_protocol']['source_hashes'][str(path)]
        with np.load(path) as pred:
            outputs[name] = (pred['probabilities'][indices], pred['magnitude'][indices])
            full_correct[name] = (pred['probabilities'][full_ids].argmax(-1) == s['cls'][full_ids]).astype(float)
    keys = {(name, market, h) for name in outputs for market in ('KOSPI', 'NASDAQ') for h in (1, 3)}
    metrics = pd.read_csv(out / 'validation_metrics.csv')
    assert len(metrics) == 52 and set(zip(metrics.model, metrics.market, metrics.horizon)) == keys
    errors, daily_by_key = [], {}
    full_scores, conditional_scores = collections.defaultdict(list), collections.defaultdict(list)
    for row in metrics.itertuples():
        h = {1:0, 3:1}[row.horizon]
        fs, rs = full.market.eq(row.market).to_numpy(), val.market.eq(row.market).to_numpy()
        p, m = outputs[row.model]
        predicted, actual = p[rs, h].argmax(-1), s['cls'][indices][rs, h]
        correct = (predicted == actual).astype(float)
        daily = pd.Series(full_correct[row.model][fs, h], index=full.loc[fs, 'anchor_date']).groupby(level=0).mean().sort_index()
        retained = pd.Series(correct, index=val.loc[rs, 'anchor_date']).groupby(level=0).mean().mean()
        expected = [daily.mean(), retained, correct.mean(),
                    np.mean([(predicted[actual == k] == k).mean() for k in range(3)]),
                    abs(m[rs, h].astype(np.float64) - s['target_magnitude'][indices][rs, h]).mean()]
        saved = [row.full_accuracy, row.retained_accuracy, row.pair_accuracy, row.balanced_accuracy, row.retained_magnitude_mae]
        np.testing.assert_allclose(expected, saved, atol=1e-10, rtol=1e-8)
        errors.extend(abs(np.asarray(expected)-saved))
        assert row.n_full == fs.sum() and row.n_retained == rs.sum() and row.n_sessions == len(daily)
        daily_by_key[row.model, row.market, row.horizon] = daily
        full_scores[row.model].append(float(daily.mean())); conditional_scores[row.model].append(float(retained))
    comparisons = pd.read_csv(out / 'validation_comparisons.csv')
    assert len(comparisons) == 136
    triples = {tuple(x) for x in s['wrapper_protocol']['contrasts']}
    assert len(triples) == 17 and set(CONTRASTS).issubset(triples)
    rest = {(model, baseline) for _, model, baseline in triples - set(CONTRASTS)}
    assert rest == {(m, b) for m in ('no_edges_loss', 'standard_accuracy', 'balanced_accuracy') for b in BASELINES}
    expected_keys = {(c, m, b, market, h, lag) for c, m, b in triples for market in ('KOSPI', 'NASDAQ') for h in (1, 3) for lag in (5, 20)}
    assert set(zip(comparisons.contrast, comparisons.model, comparisons.baseline, comparisons.market, comparisons.horizon, comparisons.lag)) == expected_keys
    stat_errors = []
    for row in comparisons.itertuples():
        a, b = daily_by_key[row.model, row.market, row.horizon], daily_by_key[row.baseline, row.market, row.horizon]
        assert a.index.equals(b.index)
        difference = (a-b).to_numpy(); delta = float(difference.mean())
        centered = difference-delta; n = len(centered); variance = float(centered@centered)/n
        for lag in range(1, min(row.lag, n-1)+1):
            variance += 2*(1-lag/(row.lag+1))*float(centered[lag:]@centered[:-lag])/n
        se = float(np.sqrt(max(variance, 0)/n)); p = float(2*norm.sf(abs(delta/se))) if se else float(delta == 0)
        np.testing.assert_allclose([delta, se, p], [row.delta, row.se, row.p], atol=1e-12, rtol=1e-9)
        stat_errors.append(max(abs(delta-row.delta), abs(se-row.se), abs(p-row.p)))
    for _, group in comparisons.groupby('lag'):
        assert len(group) == 68
        running = 0.
        for position, row in enumerate(group.sort_values('p').itertuples()):
            running = max(running, min(1, row.p*(68-position)))
            assert abs(row.p_holm_68-running) < 1e-10
    scores = {name: float(np.mean(values)) for name, values in full_scores.items()}
    completion = json.loads((out / 'completion.json').read_text(encoding='utf8'))
    assert completion['source_unchanged'] and completion['validation_only']
    assert completion['original_direct_pairs'] == 4813 and completion['retained_pairs'] == 4743
    assert completion['trained_trajectories'] == 15 and completion['saved_selections'] == 30
    assert completion['comparisons'] == 3 and not completion['automatic_model_replacement']
    assert completion['user_target'] == .65
    for name, score in scores.items():
        assert abs(score-completion['original_population_accuracy'][name]) < 1e-12
        assert abs(np.mean(conditional_scores[name])-completion['conditional_accuracy'][name]) < 1e-12
    targets = {name:scores[name] >= .65 for name in ('no_edges_loss', 'standard_accuracy', 'balanced_accuracy')}
    assert completion['target_met_by_candidate'] == targets
    assert not list(out.rglob('*test*predictions*'))
    return dict(metric_rows=52, max_metric_error=float(max(errors)), hac_comparisons=136,
        max_statistical_error=float(max(stat_errors)), hac_uses_original_4813=True,
        holm_family_per_lag=68, original_population_accuracy=scores,
        target_met_by_candidate=targets, no_new_test_predictions=True)


def independent_loss_behavior():
    """Compare actual balanced loss against scalar arithmetic, including gradients."""
    from three_comparison_metrics import balanced_direction_loss
    rng = np.random.default_rng(24601)
    logits = rng.normal(size=(3, 5, 2, 3))
    pred, target = rng.uniform(size=(3, 5, 2)), rng.uniform(size=(3, 5, 2))
    cls = rng.integers(0, 3, size=(3, 5, 2))
    mask = np.array([[1, 0, 0, 0, 0], [1, 1, 1, 0, 0], [1, 1, 1, 1, 1]], dtype=bool)
    weights = rng.uniform(.01, 3, size=(3, 5, 2))*mask[..., None]
    maximum = logits.max(axis=-1, keepdims=True)
    logsum = np.log(np.exp(logits-maximum).sum(axis=-1)) + maximum[..., 0]
    ce = logsum-np.take_along_axis(logits, cls[..., None], axis=-1)[..., 0]
    error = abs(pred-target)
    err = np.where(error < 1, .5*error**2, error-.5)
    expected_ce = (ce*weights).sum()/3
    expected_mag = ((err.mean(axis=-1)*mask).sum(axis=-1)/mask.sum(axis=-1)).mean()
    z = torch.tensor(logits, requires_grad=True); m = torch.tensor(pred, requires_grad=True)
    actual = balanced_direction_loss(z, m, torch.from_numpy(cls), torch.from_numpy(target),
        torch.from_numpy(mask), torch.from_numpy(weights))
    np.testing.assert_allclose(actual.detach().numpy(), expected_ce+expected_mag, atol=1e-12)
    actual.backward()
    probability = np.exp(logits-maximum)/np.exp(logits-maximum).sum(axis=-1, keepdims=True)
    onehot = np.eye(3)[cls]
    expected_direction_gradient = (probability-onehot)*weights[..., None]/3
    expected_magnitude_gradient = (pred-target)*mask[..., None]/mask.sum(axis=-1)[:, None, None]/6
    np.testing.assert_allclose(z.grad.numpy(), expected_direction_gradient, atol=1e-12)
    np.testing.assert_allclose(m.grad.numpy(), expected_magnitude_gradient, atol=1e-12)
    return dict(balanced_loss_scalar_match=True, direction_gradient_matches_weights=True,
        magnitude_gradient_matches_original_article_loss=True, masked_gradients_zero=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--data-only', action='store_true')
    args = parser.parse_args(); out = args.experiment.resolve(); torch.set_num_threads(4)
    result, state = read_and_check(out)
    result['balanced_loss_behavior'] = independent_loss_behavior()
    if not args.data_only:
        manifest = json.loads((out / 'output_manifest.json').read_text(encoding='utf8'))
        for name, digest in manifest.items():
            assert sha(out / name) == digest, name
        replay, outputs = replay_checkpoints(out, state)
        result.update(replay)
        result.update(verify_metrics(out, state, outputs))
        result['output_manifest_hashes_verified'] = len(manifest)
    hashes_unchanged(out, state['wrapper_protocol'], state['wrapper_prepared'])
    result['verification_code_sha256'] = sha(Path(__file__))
    dump(out / ('data_verification.json' if args.data_only else 'reload_verification.json'), result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
