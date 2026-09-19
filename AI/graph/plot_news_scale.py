"""Plot completed, exploratory news-scale validation results without retraining.

Reads only evaluation.json and validation_metrics.csv. The plotted accuracy is
original_population_accuracy: a mean over four market/horizon cells, each of
which first averages within trading sessions. It is not pair-micro accuracy,
retained-only accuracy, balanced accuracy, or an average over seed accuracies.
No aggregate confidence interval is manufactured from the cell intervals.

Usage: python plot_news_scale.py --out artifacts/news_scale_learning_20260916
Creates only OUT/outputs/learning_curve.png and its provenance JSON. Existing
outputs are never overwritten. This module does not import experiment code,
read model checkpoints, mutate frozen inputs, or generate demonstration files.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib import font_manager, pyplot as plt, ticker
import numpy as np
import pandas as pd


SIZES = (20000, 50000, 100000)
CELLS = {('KOSPI', 1), ('KOSPI', 3), ('NASDAQ', 1), ('NASDAQ', 3)}
FONT_FAMILY = 'Malgun Gothic'
FULL_YLIM = (0., .70)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def find_korean_font():
    """Fail visibly instead of silently drawing missing Korean glyphs."""
    path = Path(font_manager.findfont(
        font_manager.FontProperties(family=FONT_FAMILY), fallback_to_default=False))
    require(path.is_file(), f'{FONT_FAMILY} font file was not found')
    return path


def validate_results(evaluation, metrics):
    require(evaluation.get('validation_only') is True and evaluation.get('reused_validation') is True
            and evaluation.get('fresh_holdout') is False,
            'Expected the reused exploratory validation, not a fresh-test claim')
    require(evaluation.get('sizes') == list(SIZES), 'Completed 20k/50k/100k results are required')
    require(evaluation.get('trained_trajectories') == 18,
            'Expected two variants x three sizes x three seed trajectories')
    require((evaluation.get('original_direct_pairs'), evaluation.get('retained_pairs'),
             evaluation.get('excluded_as_wrong')) == (4813, 4743, 70),
            'Expected 4,813 original pairs, 4,743 retained, and 70 counted wrong')
    require(evaluation.get('automatic_model_replacement') is False,
            'This chart is descriptive and must not indicate automatic model promotion')
    target = float(evaluation['user_target'])
    require(math.isclose(target, .65, abs_tol=1e-12), 'Expected the fixed user target of 65%')
    scores = evaluation['original_population_accuracy']
    columns = {'model', 'market', 'horizon', 'full_accuracy', 'n_full', 'n_retained', 'n_sessions'}
    require(columns <= set(metrics.columns), f'Missing metric columns: {sorted(columns-set(metrics.columns))}')
    models = [f'size_{size}_{variant}' for size in SIZES for variant in ('text', 'no_content')] + ['original']
    population = None
    for model in models:
        rows = metrics.loc[metrics.model.eq(model)].copy()
        require(len(rows) == 4 and set(zip(rows.market, rows.horizon)) == CELLS,
                f'{model} must have exactly four distinct market/horizon cells')
        require(np.isfinite(rows.full_accuracy).all() and rows.full_accuracy.between(0, 1).all(),
                f'Invalid accuracy values: {model}')
        require((rows.n_sessions > 0).all(), f'No validation sessions for {model}')
        counts = rows.set_index(['market', 'horizon'])[['n_full', 'n_retained', 'n_sessions']].sort_index()
        if population is None:
            population = counts
        else:
            require(counts.equals(population), f'Validation populations differ for {model}')
        for horizon in (1, 3):
            subset = rows.loc[rows.horizon.eq(horizon)]
            require(int(subset.n_full.sum()) == 4813 and int(subset.n_retained.sum()) == 4743,
                    f'Validation pair counts differ for {model}, horizon {horizon}')
        score = float(scores[model])
        require(math.isfinite(score) and 0 <= score <= FULL_YLIM[1],
                f'{model} is outside the requested 0–70% full axis; refusing a clipped chart')
        require(math.isclose(score, float(rows.full_accuracy.mean()), rel_tol=0, abs_tol=1e-12),
                f'evaluation.json and the four-cell mean disagree for {model}')
    baseline = float(scores['original'])
    require(f'{100*baseline:.2f}' == '39.61', 'The frozen original reference should display as 39.61%')
    return dict(
        sizes=list(SIZES),
        text=[float(scores[f'size_{size}_text']) for size in SIZES],
        no_content=[float(scores[f'size_{size}_no_content']) for size in SIZES],
        original=baseline, user_target=target,
    )


def zoom_bounds(plot_data):
    values = plot_data['text'] + plot_data['no_content'] + [plot_data['original']]
    low, high = min(values), max(values)
    padding = max(.003, (high-low)*.25)
    return (max(0., math.floor((low-padding)*1000)/1000),
            min(FULL_YLIM[1], math.ceil((high+padding)*1000)/1000))


def make_figure(plot_data):
    """Construct only an in-memory figure; saving is an explicit final step."""
    find_korean_font()
    zoom = zoom_bounds(plot_data)
    style = {
        'font.family': FONT_FAMILY, 'axes.unicode_minus': False, 'font.size': 10.5,
        'axes.titlesize': 12.5, 'axes.labelsize': 10.5, 'text.color': '#25354B',
        'axes.labelcolor': '#25354B', 'xtick.color': '#445469', 'ytick.color': '#445469',
        'figure.facecolor': '#F7F9FC', 'axes.facecolor': 'white',
    }
    with plt.rc_context(style):
        fig, axes = plt.subplots(1, 2, figsize=(13.5, 7.4), gridspec_kw={'wspace': .23})
        fig.subplots_adjust(left=.075, right=.97, bottom=.255, top=.76)
        fig.suptitle('재사용 검증 · 탐색적 학습곡선', x=.5, y=.975,
                     fontsize=20, fontweight='bold')
        fig.text(.5, .913, '거래일별 정확도 평균 → KOSPI·NASDAQ × 1·3거래일의 4개 셀 동일 가중',
                 ha='center', fontsize=11.5, color='#526277')
        x = np.asarray(plot_data['sizes'])
        for index, ax in enumerate(axes):
            ax.plot(x, plot_data['text'], color='#176AA6', marker='o', linewidth=2.4,
                    markersize=7, label='뉴스 포함 (text)', zorder=3)
            ax.plot(x, plot_data['no_content'], color='#D77528', marker='s', linewidth=2.2,
                    markersize=6.5, linestyle='--', label='뉴스 내용 제거 (no_content)', zorder=3)
            ax.axhline(plot_data['original'], color='#687587', linestyle=(0, (5, 3)),
                       linewidth=1.5, label=f'고정 original {100*plot_data["original"]:.2f}%', zorder=2)
            if index == 0:
                ax.axhline(plot_data['user_target'], color='#8953AD', linestyle=(0, (2, 2)),
                           linewidth=1.7, label='사용자 목표 65%', zorder=2)
            ax.set_xlim(x[0]-8000, x[-1]+8000)
            ax.set_xticks(x, ['20k', '50k', '100k'])
            ax.set_xlabel('학습 기사 수 (중첩 표본)', labelpad=10)
            ax.set_ylabel('방향 정확도', labelpad=8)
            ax.set_axisbelow(True)
            ax.grid(axis='y', color='#DDE4ED', linewidth=.8)
            for spine in ('top', 'right'):
                ax.spines[spine].set_visible(False)
            for spine in ('left', 'bottom'):
                ax.spines[spine].set_color('#CCD5E1')
        axes[0].set_ylim(*FULL_YLIM)
        axes[0].yaxis.set_major_locator(ticker.MultipleLocator(.1))
        axes[0].yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1, decimals=0))
        axes[0].set_title('목표까지의 거리 · y축 0–70%', loc='left', pad=13, fontweight='bold')
        axes[1].set_ylim(*zoom)
        axes[1].yaxis.set_major_locator(ticker.MaxNLocator(5))
        axes[1].yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1, decimals=2 if zoom[1]-zoom[0] < .02 else 1))
        axes[1].set_title(f'관측 점수 확대 · y축 {100*zoom[0]:.1f}–{100*zoom[1]:.1f}%',
                          loc='left', pad=13, fontweight='bold')
        for i, size in enumerate(x):
            for variant, other, color in (('text', 'no_content', '#176AA6'), ('no_content', 'text', '#B85C15')):
                value = plot_data[variant][i]
                above = value > plot_data[other][i] or (value == plot_data[other][i] and variant == 'text')
                axes[1].annotate(f'{100*value:.2f}%', (size, value), xytext=(0, 9 if above else -11),
                                 textcoords='offset points', ha='center', va='bottom' if above else 'top',
                                 fontsize=9.5, fontweight='bold', color=color,
                                 bbox=dict(facecolor='white', edgecolor='none', alpha=.8, pad=1.1))
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .867),
                   ncol=4, frameon=False, fontsize=10, handlelength=2.8, columnspacing=1.7)
        fig.text(.075, .151,
                 '원래 4,813쌍 기준 · 새 모델의 70개 미응답은 오답 처리 · 3시드 예측확률 평균 앙상블',
                 fontsize=10.5, color='#445469')
        fig.text(.075, .109,
                 '신뢰구간 미표시: 4셀 집계 CI를 새로 추정하지 않음. 확대 패널의 y축은 0에서 시작하지 않음.',
                 fontsize=10, color='#526277')
        fig.text(.075, .063,
                 '반복 사용한 검증 결과이며 미사용 후속 기간의 최종 성능을 의미하지 않음. 목표 65%는 왼쪽 전체 축에 표시.',
                 fontsize=10, color='#526277')
    return fig, axes, zoom


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='Study with completed evaluation.json and validation_metrics.csv')
    args = parser.parse_args()
    out = args.out.resolve()
    inputs = {name: out/name for name in ('evaluation.json', 'validation_metrics.csv')}
    for path in inputs.values():
        require(path.is_file(), f'Completed evaluation input missing: {path}')
    destination = out/'outputs'
    image_path = destination/'learning_curve.png'
    manifest_path = destination/'learning_curve_manifest.json'
    require(not image_path.exists() and not manifest_path.exists(), 'Plot output already exists; refusing to overwrite')
    source_hashes = {name: sha(path) for name, path in inputs.items()}
    code_hash = sha(Path(__file__))
    evaluation = json.loads(inputs['evaluation.json'].read_text(encoding='utf8'))
    metrics = pd.read_csv(inputs['validation_metrics.csv'])
    plot_data = validate_results(evaluation, metrics)
    font_path = find_korean_font()
    fig, axes, zoom = make_figure(plot_data)
    try:
        fig.canvas.draw()
        require({name: sha(path) for name, path in inputs.items()} == source_hashes,
                'Evaluation inputs changed while drawing; no plot written')
        require(sha(Path(__file__)) == code_hash, 'Plot code changed while drawing; no plot written')
        destination.mkdir(parents=True, exist_ok=True)
        with image_path.open('xb') as stream:
            fig.savefig(stream, format='png', dpi=180, facecolor=fig.get_facecolor())
        manifest = dict(
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            source_directory=str(out), source_sha256=source_hashes,
            code_sha256=code_hash, image_sha256=sha(image_path), font_file=str(font_path),
            metric='original_population_accuracy', aggregation='equal mean of four market/horizon daily-macro accuracies',
            full_y_limits=list(FULL_YLIM), zoom_y_limits=list(zoom), plotted_values=plot_data,
            confidence_intervals_plotted=False, confidence_intervals_recomputed=False,
            reused_validation=True, fresh_holdout=False, ensemble_seeds=3,
            original_pairs=4813, retained_pairs=4743, excluded_as_wrong=70,
            source_files_unchanged=True, models_or_frozen_inputs_modified=False,
        )
        with manifest_path.open('x', encoding='utf8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    finally:
        plt.close(fig)
    print(json.dumps(dict(image=str(image_path), manifest=str(manifest_path), source_files_unchanged=True), ensure_ascii=False))


if __name__ == '__main__':
    main()
