"""Frozen supervised-data learning curve with matched three-seed text controls.

Run the unchanged fit_target trainer at each prepared nested article budget.
Every budget uses identical 2023 validation rows, graphs, labels, encoders and
PCA. Compare text with zero text/sentiment at that same budget; company identity,
mention metadata, graph and all training examples remain in the control.

Compact validation pair IDs refer to rows 0:4813 in the new table. The 4743
retained predictions map into that population; the other 70 are always counted
wrong for new models. Frozen baseline predictions use ORIGINAL pair indices
from original_validation_indices.npy, never the compact indices by accident.

All pairwise text-budget comparisons, each text/no-content comparison and each
text/frozen-baseline comparison are fixed before training. Direction accuracy is
averaged equally across sessions and then the four market/horizon cells. HAC
lags 5/20 each receive one Holm family covering every contrast x four cells;
reported 95% intervals are unadjusted. Magnitude MAE is descriptive on the common
4743 rows. This repeatedly inspected validation is not a fresh test. No test
inference, service connection, automatic promotion or search until 65% occurs.

Completed arms are hash-checked and reused. An interrupted arm restarts all three
seeds in a NEW attempt directory, preserving the incomplete attempt. No trained
checkpoint is silently overwritten during resume.
"""

import argparse
from datetime import datetime, timezone
import gc
import itertools
import json
from pathlib import Path
import re
import time

import numpy as np
import pandas as pd
from scipy.stats import norm
import torch

from audit_news_impact import hac_mean_se
from experiment_news_scale import load_data, FEATURES
from experiment_news_augmentation import BASELINE_PATHS
from train_target_news import fit_target
from verify_news_predictive_value import sha, dump, holm

HERE = Path(__file__).resolve().parent
VARIANTS = ('text', 'no_content')
BASELINES = ('original', 'ticker_prior', 'majority')
CODE_FILES = (
    'train_news_scale.py', 'experiment_news_scale.py', 'experiment_current_events.py',
    'experiment_news_augmentation.py', 'train_target_news.py', 'target_news_model.py',
    'news_impact_model.py', 'train_news_impact.py', 'news_impact_data.py',
    'target_news_features.py', 'news_quality.py', 'news_content.py',
    'verify_news_predictive_value.py', 'audit_news_impact.py',
)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def write_frozen(path, value):
    """Allow identical recomputation, reject replacement of recorded results."""
    path = Path(path)
    if path.exists():
        assert read_json(path) == value, f'Existing frozen JSON differs: {path}'
    else:
        dump(path, value)


def model_name(size, variant):
    assert variant in VARIANTS
    return f'size_{int(size)}_{variant}'


def comparison_plan(sizes):
    result = [(f'scale_{small}_to_{large}', model_name(large, 'text'), model_name(small, 'text'))
              for small, large in itertools.combinations(sizes, 2)]
    result += [(f'news_content_{size}', model_name(size, 'text'), model_name(size, 'no_content'))
               for size in sizes]
    result += [(f'{model_name(size, "text")}_vs_{baseline}', model_name(size, 'text'), baseline)
               for size in sizes for baseline in BASELINES]
    return result


def input_hashes(out):
    """Validate the completed upstream chains before freezing any training."""
    pool = read_json(out/'pool_protocol.json')
    prepared = read_json(out/'prepared.json')
    features = read_json(out/'features_complete.json')
    verification = read_json(out/'data_verification.json')
    feature_verification = read_json(out/'features_verification.json')
    assert verification['source_unchanged'] is True
    assert feature_verification['source_unchanged'] is True
    assert feature_verification['prepared_sha256'] == sha(out/'prepared.json')
    assert feature_verification['code_hashes'] == verification['code_hashes']
    assert feature_verification['data_verification_sha256'] == sha(out/'data_verification.json')
    assert feature_verification['features_complete_sha256'] == sha(out/'features_complete.json')
    feature_checks = feature_verification['features']
    assert feature_checks['parent_features_exact'] is True
    assert feature_checks['projection_all_rows_match'] is True
    assert feature_checks['common_frozen_pca'] is True
    assert feature_checks['encoder_pairs'] > 0, 'CPU encoder replay is required before training'
    for name, expected in pool['code_hashes'].items():
        assert sha(HERE/name) == expected, f'Pool code changed: {name}'
    sources = dict(pool['source_hashes'])
    sources.update({str(out/name): digest for name, digest in prepared['artifact_hashes'].items()})
    sources.update({str(out/name): digest for name, digest in features['artifact_hashes'].items()})
    sources[str(FEATURES/'target_projection.npz')] = features['parent_projection_sha256']
    for role in ('embedding', 'sentiment_ko', 'sentiment_en'):
        stamp = out/f'new_{role}_complete.json'
        record = read_json(stamp)
        sources[str(out/f'new_{role}.npy')] = record['sha256']
        sources[str(out/f'{role}_audit.jsonl')] = record['audit_sha256']
        sources[str(stamp)] = sha(stamp)
    for name in ('pool_protocol.json', 'pool_completion.json', 'prepared.json',
                 'features_complete.json', 'data_verification.json', 'features_verification.json'):
        sources[str(out/name)] = sha(out/name)
    for name, path in BASELINE_PATHS.items():
        assert name in BASELINES
        sources[str(path)] = sha(path)
    for path, expected in sources.items():
        assert sha(Path(path)) == expected, f'Upstream source changed: {path}'
    # Bind the pre-feature independent audit to this exact prepared snapshot.
    assert sha(out/'prepared.json') == verification['prepared_sha256']
    assert verification['code_hashes'], 'Independent verifier code hash required'
    for name, expected in verification['code_hashes'].items():
        path = Path(name) if Path(name).is_absolute() else HERE/name
        assert sha(path) == expected, f'Verified code changed: {name}'
    return prepared, sources


def check_protocol(out):
    protocol = read_json(out/'training_protocol.json')
    for path, expected in protocol['source_hashes'].items():
        assert sha(Path(path)) == expected, f'Training source changed: {path}'
    for name, expected in protocol['code_hashes'].items():
        assert sha(HERE/name) == expected, f'Training code changed: {name}'
    return protocol


def register_protocol(out):
    if (out/'training_protocol.json').exists():
        return check_protocol(out)
    prepared, sources = input_hashes(out)
    sizes = prepared['sizes']
    assert sizes == sorted(set(sizes)) and all(isinstance(n, int) and n > 0 for n in sizes)
    assert prepared['validation_pairs'] == 4743 and prepared['original_direct_pairs'] == 4813
    contrasts = comparison_plan(sizes)
    protocol = dict(
        registered_at=datetime.now(timezone.utc).isoformat(),
        source_hashes=sources, code_hashes={name: sha(HERE/name) for name in CODE_FILES},
        sizes=sizes, variants=list(VARIANTS), seeds=[0, 1, 2],
        arms=[model_name(size, variant) for size in sizes for variant in VARIANTS],
        contrasts=contrasts, correction_family=len(contrasts)*4, hac_lags=[5, 20],
        confidence_intervals='normal HAC 95%, unadjusted for multiplicity',
        trainer='unchanged train_target_news.fit_target',
        selector='minimum unweighted composite validation loss; earliest improvement >1e-5',
        settings=dict(epochs=30, patience=6, min_delta=1e-5, batch_size=24,
                      lr=.0008, weight_decay=.001, hidden=64, dropout=.15, gradient_clip=2.),
        statistical_population='original_direct_4813; compact indices0:4813; 70 abstentions wrong',
        original_baseline_index_file='original_validation_indices.npy',
        no_content='zero embedding and sentiment; keep companies, mentions, graph and labels',
        baseline_policy='same frozen original/ticker_prior/majority predictions at every budget',
        graph_policy='one shared frozen annual graph set; this scales supervised examples only',
        magnitude_policy='descriptive MAE on common4743 rows; excluded magnitude loss undefined',
        validation_only=True, reused_validation=True, fresh_holdout=False,
        automatic_model_replacement=False, user_target=.65,
        limitations='Source times and historical legal identities remain uncertified; no guaranteed65%',
    )
    dump(out/'training_protocol.json', protocol)
    return check_protocol(out)


def completed_folder(out, arm, verify=True):
    """Resolve a committed, immutable attempt; no predictions from partial fits."""
    root = out/'models'/arm
    marker = read_json(root/'fit_complete.json')
    attempt = marker['attempt']
    assert re.fullmatch(r'attempt_\d{4}', attempt), attempt
    folder = root/attempt
    if verify:
        assert sha(out/'training_protocol.json') == marker['training_protocol_sha256']
        assert sha(folder/'fit_complete.json') == marker['fit_complete_sha256']
        done = read_json(folder/'fit_complete.json')
        assert done['arm'] == arm and done['seeds'] == 3
        for name, expected in done['artifact_hashes'].items():
            assert Path(name).name == name
            assert sha(folder/name) == expected, f'Completed arm changed: {arm}/{name}'
    return folder


def validate_predictions(probabilities, magnitude, count):
    assert probabilities.shape == (count, 2, 3)
    assert magnitude.shape == (count, 2)
    assert np.isfinite(probabilities).all() and np.isfinite(magnitude).all()
    assert ((probabilities >= 0) & (probabilities <= 1)).all()
    assert ((magnitude >= 0) & (magnitude <= 1)).all()
    np.testing.assert_allclose(probabilities.sum(-1), 1, atol=1e-6)


def evaluate(out):
    protocol = check_protocol(out)
    sizes = protocol['sizes']
    data = load_data(out, max(sizes))
    indices = np.load(out/'pair_indices.npy')
    original_indices = np.load(out/'original_validation_indices.npy')
    full_indices = np.flatnonzero(data.val & data.pairs.is_direct.to_numpy())
    np.testing.assert_array_equal(full_indices, np.arange(4813))
    assert len(indices) == 4743 and len(np.unique(indices)) == len(indices)
    assert np.all(np.diff(indices) > 0) and indices.min() >= 0 and indices.max() < 4813
    np.testing.assert_array_equal(original_indices, data.pairs.iloc[:4813].original_pair_index)
    assert len(np.unique(original_indices)) == 4813
    full = data.pairs.iloc[full_indices]
    retained = data.pairs.iloc[indices]
    outputs, baseline_full = {}, {}
    for arm in protocol['arms']:
        folder = completed_folder(out, arm)
        with np.load(folder/'validation_predictions.npz') as arrays:
            np.testing.assert_array_equal(arrays['pair_indices'], indices)
            outputs[arm] = arrays['probabilities'].copy(), arrays['magnitude'].copy()
    for name in BASELINES:
        with np.load(BASELINE_PATHS[name]) as arrays:
            # NEVER index the original files directly with compact pair IDs.
            full_p = arrays['probabilities'][original_indices]
            full_m = arrays['magnitude'][original_indices]
            validate_predictions(full_p, full_m, 4813)
            baseline_full[name] = full_p
            outputs[name] = full_p[indices], full_m[indices]
    for p, m in outputs.values():
        validate_predictions(p, m, len(indices))
    metrics, comparisons, daily_rows = [], [], []
    for market in ('KOSPI', 'NASDAQ'):
        keep_market = retained.market.eq(market).to_numpy()
        all_market = full.market.eq(market).to_numpy()
        for horizon_index, horizon in enumerate((1, 3)):
            daily_scores = {}
            actual = data.cls[indices, horizon_index][keep_market]
            for name, (prob, mag) in outputs.items():
                prediction = prob[keep_market, horizon_index].argmax(-1)
                correct = (prediction == actual).astype(float)
                kept_days = pd.Series(correct, index=retained.loc[keep_market, 'anchor_date']).groupby(level=0).mean()
                if name in baseline_full:
                    all_correct = (baseline_full[name][:, horizon_index].argmax(-1) ==
                                   data.cls[full_indices, horizon_index]).astype(float)
                else:
                    all_correct = np.zeros(len(full), dtype='float64')
                    all_correct[indices] = (prob[:, horizon_index].argmax(-1) ==
                                            data.cls[indices, horizon_index])
                daily = pd.Series(all_correct[all_market], index=full.loc[all_market, 'anchor_date'])
                daily = daily.groupby(level=0).agg(['mean', 'size', 'sum']).sort_index()
                daily_scores[name] = daily['mean']
                absolute_error = np.abs(mag[keep_market, horizon_index].astype('float64') -
                                        data.mag[indices, horizon_index][keep_market])
                magnitude_daily = pd.Series(absolute_error, index=retained.loc[keep_market, 'anchor_date']).groupby(level=0).mean()
                metric = dict(model=name, market=market, horizon=horizon,
                    n_full=int(all_market.sum()), n_retained=int(keep_market.sum()), n_sessions=len(daily),
                    full_accuracy=float(daily['mean'].mean()),
                    full_pair_accuracy=float(all_correct[all_market].mean()),
                    retained_accuracy=float(kept_days.mean()), retained_pair_accuracy=float(correct.mean()),
                    retained_balanced_accuracy=float(np.mean([(prediction[actual == k] == k).mean() for k in range(3)])),
                    retained_magnitude_mae=float(absolute_error.mean()),
                    retained_session_magnitude_mae=float(magnitude_daily.mean()))
                for lag in protocol['hac_lags']:
                    se = hac_mean_se(daily['mean'].to_numpy(), lag)
                    assert se is not None and np.isfinite(se)
                    metric[f'full_accuracy_hac_se_{lag}'] = se
                    metric[f'full_accuracy_ci95_low_{lag}'] = metric['full_accuracy'] - 1.959963984540054*se
                    metric[f'full_accuracy_ci95_high_{lag}'] = metric['full_accuracy'] + 1.959963984540054*se
                metrics.append(metric)
                daily_rows.extend(dict(model=name, market=market, horizon=horizon,
                    anchor_date=str(date), accuracy=float(row['mean']),
                    n_pairs=int(row['size']), n_correct=int(row['sum'])) for date, row in daily.iterrows())
            for contrast, model, baseline in protocol['contrasts']:
                a, b = daily_scores[model], daily_scores[baseline]
                assert a.index.equals(b.index)
                difference = (a - b).to_numpy()
                delta = float(difference.mean())
                for lag in protocol['hac_lags']:
                    se = hac_mean_se(difference, lag)
                    assert se is not None and np.isfinite(se)
                    p_value = float(2*norm.sf(abs(delta/se))) if se > 0 else float(delta == 0)
                    comparisons.append(dict(contrast=contrast, model=model, baseline=baseline,
                        market=market, horizon=horizon, lag=lag, n_sessions=len(difference),
                        delta=delta, se=se, p=p_value,
                        ci95_low=delta-1.959963984540054*se, ci95_high=delta+1.959963984540054*se))
    report, comp = pd.DataFrame(metrics), pd.DataFrame(comparisons)
    family = protocol['correction_family']
    for lag in protocol['hac_lags']:
        selected = comp.lag.eq(lag)
        assert int(selected.sum()) == family
        comp.loc[selected, 'p_holm'] = holm(comp.loc[selected, 'p'].to_numpy())
    comp['holm_family_size'] = family
    comp['ci95_adjusted'] = False
    report.to_csv(out/'validation_metrics.csv', index=False)
    comp.to_csv(out/'validation_comparisons.csv', index=False)
    with (out/'daily_scores.jsonl').open('w', encoding='utf8') as handle:
        for row in daily_rows:
            handle.write(json.dumps(row, ensure_ascii=False)+'\n')
    scores = report.groupby('model').full_accuracy.mean().to_dict()
    result = dict(validation_only=True, reused_validation=True, fresh_holdout=False,
        original_direct_pairs=4813, retained_pairs=4743, excluded_as_wrong=70,
        sizes=sizes, trained_trajectories=len(protocol['arms'])*3,
        original_population_accuracy=scores,
        conditional_accuracy=report.groupby('model').retained_accuracy.mean().to_dict(),
        retained_magnitude_mae=report.groupby('model').retained_magnitude_mae.mean().to_dict(),
        retained_session_magnitude_mae=report.groupby('model').retained_session_magnitude_mae.mean().to_dict(),
        comparison_count=len(protocol['contrasts']), correction_family=family, hac_lags=protocol['hac_lags'],
        user_target=.65, target_met_by_text_size={str(n):scores[model_name(n, 'text')] >= .65 for n in sizes},
        automatic_model_replacement=False, baseline_policy=protocol['baseline_policy'])
    write_frozen(out/'evaluation.json', result)
    return result


def train(out):
    started = time.monotonic()
    protocol = register_protocol(out)
    protocol_hash = sha(out/'training_protocol.json')
    torch.set_num_threads(4)
    mapping = np.load(out/'feature_mapping.npy')
    text = np.load(out/'target_text.npy')
    sentiment = np.load(out/'target_sentiment.npy')
    indices = np.load(out/'pair_indices.npy')
    trained_now = []
    for size in protocol['sizes']:
        for variant in VARIANTS:
            arm = model_name(size, variant)
            model_root = out/'models'/arm
            if (model_root/'fit_complete.json').exists():
                completed_folder(out, arm)
                print(json.dumps(dict(stage='reused_completed_arm', arm=arm)), flush=True)
                continue
            model_root.mkdir(parents=True, exist_ok=True)
            attempt_number = 1
            while (model_root/f'attempt_{attempt_number:04d}').exists():
                attempt_number += 1
            folder = model_root/f'attempt_{attempt_number:04d}'
            folder.mkdir(exist_ok=False)
            data = load_data(out, size)
            active = np.array([row['split'] == 'train' for row in data.docs])
            assert int(active.sum()) == size
            train_pairs = int(data.mask[active].sum())
            assert int(data.train.sum()) == train_pairs
            policy = dict(arm=arm, article_budget=size, variant=variant, seeds=[0, 1, 2],
                          research_only=True, validation_only=True, no_test_prediction=True,
                          zero_content=variant == 'no_content', train_pairs=train_pairs,
                          training_protocol_sha256=protocol_hash)
            dump(folder/'MODEL_POLICY.json', policy)
            print(json.dumps(dict(stage='arm_started', **policy)), flush=True)
            x, s = ((np.zeros_like(text), np.zeros_like(sentiment))
                    if variant == 'no_content' else (text, sentiment))
            begin = time.monotonic()
            (prob, mag), selections = fit_target(data, mapping, x, s, folder, indices)
            validate_predictions(prob, mag, len(indices))
            dump(folder/'selections.json', selections)
            done = dict(arm=arm, seeds=3, article_budget=size, train_pairs=train_pairs,
                        seconds=time.monotonic()-begin, training_protocol_sha256=protocol_hash,
                        artifact_hashes={p.name: sha(p) for p in folder.iterdir() if p.is_file()})
            dump(folder/'fit_complete.json', done)
            dump(model_root/'fit_complete.json', dict(attempt=folder.name,
                 training_protocol_sha256=protocol_hash, fit_complete_sha256=sha(folder/'fit_complete.json')))
            completed_folder(out, arm)
            trained_now.append(arm)
            del data, x, s, prob, mag
            gc.collect(); torch.cuda.empty_cache()
            print(json.dumps(dict(stage='arm_complete', arm=arm, seconds=done['seconds'])), flush=True)
    check_protocol(out)
    result = evaluate(out)
    check_protocol(out)
    total_seconds = sum(read_json(completed_folder(out, arm)/'fit_complete.json')['seconds']
                        for arm in protocol['arms'])
    completion = result | dict(source_unchanged=True, training_seconds=total_seconds,
                               training_protocol_sha256=protocol_hash)
    write_frozen(out/'completion.json', completion)
    # Manifest only final training/evaluation outputs; input fingerprints are in
    # training_protocol. Incomplete attempts remain diagnostics, not final models.
    files = [out/name for name in ('training_protocol.json', 'validation_metrics.csv',
             'validation_comparisons.csv', 'daily_scores.jsonl', 'evaluation.json', 'completion.json')]
    for arm in protocol['arms']:
        folder = completed_folder(out, arm)
        files.extend(p for p in folder.iterdir() if p.is_file())
        files.append(out/'models'/arm/'fit_complete.json')
    write_frozen(out/'training_output_manifest.json', {str(p.relative_to(out)):sha(p) for p in files})
    print(json.dumps(result | dict(trained_this_invocation=trained_now,
                                   invocation_seconds=time.monotonic()-started)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--stage', choices=('train', 'evaluate'), required=True)
    arguments = parser.parse_args()
    destination = arguments.out.resolve()
    if arguments.stage == 'train':
        train(destination)
    else:
        print(json.dumps(evaluate(destination)), flush=True)
