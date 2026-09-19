"""Three bounded comparisons on immutable news augmentation data.

1. Remove only inter-company messages, preserving nodes/features/labels.
2. Select by direction accuracy rather than composite loss, from exactly the
   same visited checkpoints. Composite validation loss still controls stopping.
3. Reweight training direction CE by market/session/horizon; retain original
   magnitude loss and the same accuracy selector. Matched zero-content controls
   distinguish a text benefit from a change in company/market statistics.

Five arms x three seeds = fifteen fits. Each trajectory yields loss/accuracy
selectors, not two independently trained models. No input/label/threshold/PCA
revision, test inference, automatic promotion, or repeated search until 65%.
All comparisons are exploratory on the already reused 2023 validation period.
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from scipy.stats import norm

from experiment_news_augmentation import load_data, BASELINE_PATHS
from train_three_comparisons import fit_comparison
from three_comparison_metrics import make_train_weights, make_selection_weights
from verify_news_predictive_value import sha, dump, holm
from audit_news_impact import hac_mean_se

HERE = Path(__file__).resolve().parent
SOURCE = HERE/'artifacts/news_augmentation_20260915_v2'
ARMS = ('standard', 'no_edges', 'no_content', 'balanced', 'balanced_no_content')
SELECTORS = ('loss', 'accuracy')
CONTRASTS = [
    ('graph', 'standard_loss', 'no_edges_loss'),
    ('selection_content', 'standard_accuracy', 'standard_loss'),
    ('selection_no_content', 'no_content_accuracy', 'no_content_loss'),
    ('weight_content', 'balanced_accuracy', 'standard_accuracy'),
    ('weight_no_content', 'balanced_no_content_accuracy', 'no_content_accuracy'),
    ('news_standard', 'standard_loss', 'no_content_loss'),
    ('news_accuracy', 'standard_accuracy', 'no_content_accuracy'),
    ('news_balanced', 'balanced_accuracy', 'balanced_no_content_accuracy'),
] + [(f'{candidate}_vs_{baseline}', candidate, baseline)
     for candidate in ('no_edges_loss', 'standard_accuracy', 'balanced_accuracy')
     for baseline in ('original', 'ticker_prior', 'majority')]


def check_hashes(out):
    protocol = json.loads((out/'protocol.json').read_text(encoding='utf8'))
    prepared = json.loads((out/'prepared.json').read_text(encoding='utf8'))
    for path, digest in protocol['source_hashes'].items():
        assert sha(Path(path)) == digest, path
    for name, digest in protocol['code_hashes'].items():
        assert sha(HERE/name) == digest, name
    for name, digest in prepared['artifact_hashes'].items():
        assert sha(out/name) == digest, name
    return protocol


def prepare(out):
    out.mkdir(parents=True, exist_ok=False)
    upstream = json.loads((SOURCE/'protocol.json').read_text(encoding='utf8'))
    verified = json.loads((SOURCE/'reload_verification.json').read_text(encoding='utf8'))
    assert verified['source_unchanged'] and verified['checkpoints'] == 9
    source_hashes = dict(upstream['source_hashes'])
    source_hashes.update({str(p): sha(p) for p in SOURCE.rglob('*') if p.is_file()})
    code = list(upstream['code_hashes']) + [
        'experiment_three_comparisons.py', 'train_three_comparisons.py', 'three_comparison_metrics.py']
    for name, digest in upstream['code_hashes'].items():
        assert sha(HERE/name) == digest, name
    for path, digest in source_hashes.items():
        assert sha(Path(path)) == digest, path
    protocol = dict(source=str(SOURCE), source_hashes=source_hashes,
        code_hashes={name: sha(HERE/name) for name in code},
        arms=ARMS, selectors=SELECTORS, seeds=[0, 1, 2], contrasts=CONTRASTS,
        correction_family=68, hac_lags=[5, 20], statistical_population='original_direct_4813',
        settings=dict(epochs=30, patience=6, loss_delta=1e-5, accuracy_delta=1e-8,
            batch_size=24, lr=.0008, weight_decay=.001, hidden=64, dropout=.15),
        stopping='unweighted composite val loss, same visited epochs for both selectors',
        balance='direction CE only: N_articles/(4 * market_train_days * market_day_pairs); magnitude unchanged',
        graph_ablation='empty inter-company edge tensors; keep self-loops, static node attributes, candidates',
        no_content='zero text/sentiment; retain company identity, mention metadata and graph',
        primary_outputs=['no_edges_loss', 'standard_accuracy', 'balanced_accuracy'],
        data_revision=False, validation_only=True, reused_validation=True,
        automatic_model_replacement=False, user_target=.65,
        limitations='No fresh-period generalization claim; source timestamps and historical identities remain uncertified')
    dump(out/'protocol.json', protocol)
    data = load_data(SOURCE, 'augmented')
    indices = np.load(SOURCE/'pair_indices.npy')
    tw, vw = make_train_weights(data), make_selection_weights(data, indices)
    np.save(out/'train_direction_weights.npy', tw)
    np.save(out/'selection_weights.npy', vw)
    active = np.array([d['split'] == 'train' for d in data.docs])
    summary = dict(train_articles=int(active.sum()), train_pairs=int(data.mask[active].sum()),
        original_direct_pairs=int((data.val & data.pairs.is_direct.to_numpy()).sum()),
        retained_pairs=len(indices), train_weight_sum=float(tw.sum(dtype='float64')),
        selection_weight_sum=float(vw.sum()), train_weight_max=float(tw.max()),
        train_nonzero_values=int(np.count_nonzero(tw)), source_unchanged=True)
    assert summary['train_articles'] == 20458 and summary['train_pairs'] == 30873
    assert summary['original_direct_pairs'] == 4813 and summary['retained_pairs'] == 4743
    dump(out/'weight_summary.json', summary)
    dump(out/'prepared.json', summary | dict(artifact_hashes={p.name:sha(p) for p in out.iterdir() if p.is_file()}))
    check_hashes(out)
    print(json.dumps(summary), flush=True)


def evaluate(out):
    check_hashes(out)
    data = load_data(SOURCE, 'augmented')
    ids = np.load(SOURCE/'pair_indices.npy')
    full_ids = np.flatnonzero(data.val & data.pairs.is_direct.to_numpy())
    val, full = data.pairs.iloc[ids], data.pairs.iloc[full_ids]
    positions = np.searchsorted(full_ids, ids)
    np.testing.assert_array_equal(full_ids[positions], ids)
    outputs, baseline_full = {}, {}
    for arm in ARMS:
        for selector in SELECTORS:
            with np.load(out/arm/selector/'validation_predictions.npz') as f:
                np.testing.assert_array_equal(f['pair_indices'], ids)
                outputs[f'{arm}_{selector}'] = f['probabilities'], f['magnitude']
    for name, path in BASELINE_PATHS.items():
        with np.load(path) as f:
            outputs[name] = f['probabilities'][ids], f['magnitude'][ids]
            baseline_full[name] = f['probabilities'][full_ids]
    metrics, comparisons = [], []
    for market in ('KOSPI', 'NASDAQ'):
        retained_market = val.market.eq(market).to_numpy()
        full_market = full.market.eq(market).to_numpy()
        for h in range(2):
            daily_scores = {}
            actual = data.cls[ids, h][retained_market]
            for name, (prob, mag) in outputs.items():
                assert np.isfinite(prob).all() and np.isfinite(mag).all()
                np.testing.assert_allclose(prob.sum(-1), 1, atol=1e-6)
                assert prob.shape == (len(ids), 2, 3) and mag.shape == (len(ids), 2)
                pred = prob[retained_market, h].argmax(-1)
                correct = (pred == actual).astype(float)
                retained_daily = pd.Series(correct, index=val.loc[retained_market, 'anchor_date']).groupby(level=0).mean()
                if name in baseline_full:
                    all_correct = (baseline_full[name][:, h].argmax(-1) == data.cls[full_ids, h]).astype(float)
                else:
                    all_correct = np.zeros(len(full))
                    all_correct[positions] = prob[:, h].argmax(-1) == data.cls[ids, h]
                daily = pd.Series(all_correct[full_market], index=full.loc[full_market, 'anchor_date']).groupby(level=0).mean().sort_index()
                daily_scores[name] = daily
                metrics.append(dict(model=name, market=market, horizon=(1, 3)[h],
                    n_full=int(full_market.sum()), n_retained=int(retained_market.sum()), n_sessions=len(daily),
                    full_accuracy=float(daily.mean()), retained_accuracy=float(retained_daily.mean()),
                    pair_accuracy=float(correct.mean()),
                    balanced_accuracy=float(np.mean([(pred[actual == k] == k).mean() for k in range(3)])),
                    retained_magnitude_mae=float(np.abs(mag[retained_market, h].astype('float64')-data.mag[ids, h][retained_market]).mean())))
            for contrast, model, baseline in CONTRASTS:
                diff = (daily_scores[model]-daily_scores[baseline]).to_numpy()
                delta = float(diff.mean())
                for lag in (5, 20):
                    se = hac_mean_se(diff, lag)
                    p = float(2*norm.sf(abs(delta/se))) if se else float(delta == 0)
                    comparisons.append(dict(contrast=contrast, model=model, baseline=baseline,
                        market=market, horizon=(1, 3)[h], lag=lag, delta=delta, se=se, p=p))
    report, comp = pd.DataFrame(metrics), pd.DataFrame(comparisons)
    for lag in (5, 20):
        sel = comp.lag.eq(lag)
        assert sel.sum() == 68
        comp.loc[sel, 'p_holm_68'] = holm(comp.loc[sel, 'p'].to_numpy())
    report.to_csv(out/'validation_metrics.csv', index=False)
    comp.to_csv(out/'validation_comparisons.csv', index=False)
    scores = report.groupby('model').full_accuracy.mean().to_dict()
    result = dict(validation_only=True, original_direct_pairs=len(full_ids), retained_pairs=len(ids),
        original_population_accuracy=scores,
        conditional_accuracy=report.groupby('model').retained_accuracy.mean().to_dict(),
        user_target=.65, target_met_by_candidate={k:scores[k] >= .65 for k in ('no_edges_loss', 'standard_accuracy', 'balanced_accuracy')},
        comparisons=3, trained_trajectories=15, saved_selections=30, automatic_model_replacement=False)
    return result


def train(out):
    started = time.monotonic()
    check_hashes(out)
    check = json.loads((out/'data_verification.json').read_text(encoding='utf8'))
    assert check['source_unchanged']
    torch.set_num_threads(4)
    mapping = np.load(SOURCE/'feature_mapping.npy')
    text = np.load(SOURCE/'target_text.npy')
    sent = np.load(SOURCE/'target_sentiment.npy')
    indices = np.load(SOURCE/'pair_indices.npy')
    for arm in ARMS:
        folder = out/arm
        folder.mkdir(exist_ok=False)
        zero_content = arm in ('no_content', 'balanced_no_content')
        data = load_data(SOURCE, 'augmented')
        # Recomputed inputs must remain byte-identical to independent preparation.
        np.testing.assert_array_equal(make_train_weights(data), np.load(out/'train_direction_weights.npy'))
        np.testing.assert_array_equal(make_selection_weights(data, indices), np.load(out/'selection_weights.npy'))
        x, s = (np.zeros_like(text), np.zeros_like(sent)) if zero_content else (text, sent)
        dump(folder/'MODEL_POLICY.json', dict(research_only=True, validation_only=True, zero_content=zero_content,
            remove_edges=arm == 'no_edges', balance_direction=arm.startswith('balanced')))
        fit_comparison(data, mapping, x, s, folder, indices,
            remove_edges=arm == 'no_edges', balance_direction=arm.startswith('balanced'))
        dump(folder/'fit_complete.json', dict(seeds=3, selectors=2))
        del data
        torch.cuda.empty_cache()
    result = evaluate(out)
    check_hashes(out)
    dump(out/'completion.json', result | dict(source_unchanged=True, training_seconds=time.monotonic()-started))
    dump(out/'output_manifest.json', {str(p.relative_to(out)):sha(p) for p in out.rglob('*') if p.is_file() and p.name != 'output_manifest.json'})
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--stage', choices=('prepare', 'train'), required=True)
    args = parser.parse_args()
    (prepare if args.stage == 'prepare' else train)(args.out.resolve())
