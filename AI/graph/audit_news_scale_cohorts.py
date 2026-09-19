"""Read-only composition statistics for prepared nested news-scale cohorts.

No experiment/trainer imports, new selection, deduplication, semantic judgments,
threshold estimation, price lookup, prediction access, or model comparison.
An article's source region and its target companies' markets are separate axes.
Stored quality flags describe retained records' processing metadata, not a new
measurement of residual contamination. All source files remain unchanged.

Example:
    python audit_news_scale_cohorts.py --out artifacts/news_scale_20260916
Use --output another.json to choose a separate output. Existing outputs are not
overwritten. This helper intentionally does not modify prepared.json or freezes.
"""

import argparse
import collections
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_NAMES = (
    'prepared.json', 'articles.jsonl', 'pairs.parquet', 'direct_mask.npy',
    'companies.json', 'label_config.npz',
)
CLASS_NAMES = ('down', 'neutral', 'up')
HORIZONS = (1, 3)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding='utf8'))


def flag_list(value, context):
    if value is None:
        return None
    require(isinstance(value, list) and all(isinstance(x, str) for x in value),
            f'{context} must be a list of strings or absent')
    return sorted(set(value))  # Count each flag once per article, without removing articles.


def read_article_metadata(path):
    """Stream text without keeping large article bodies in the audit's memory."""
    records = []
    with path.open(encoding='utf8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            date = str(row['published_date'])[:10]
            companies = row.get('companies', [])
            require(all(isinstance(c.get('ticker'), str) for c in companies),
                    'Article company tickers must be strings; do not infer missing leading zeros')
            audit = row.get('publication_time_audit') or {}
            raw_hash, clean_hash = row.get('raw_body_hash'), row.get('clean_body_hash')
            rank = row.get('supervised_rank')
            if row['split'] == 'train':
                require(isinstance(rank, int) and not isinstance(rank, bool),
                        f'Training article has no integer supervised_rank: {row["record_id"]}')
            records.append(dict(
                article=len(records), record_id=row['record_id'], split=row['split'],
                supervised_rank=rank, region=row['region'], year=date[:4],
                published_date=date, source_dataset=row.get('source_dataset') or '__missing__',
                mentioned_tickers=sorted({c['ticker'] for c in companies}),
                quality_flags=flag_list(row.get('quality_flags'), 'quality_flags'),
                timestamp_status=audit.get('timestamp_status') or '__missing__',
                time_flags=flag_list(audit.get('time_flags'), 'time_flags'),
                body_hash_changed=(raw_hash != clean_hash) if raw_hash and clean_hash else None,
            ))
    require(records, 'No articles found')
    return pd.DataFrame(records).set_index('article', drop=False)


def histogram(values):
    values = np.asarray(values, dtype='int64')
    counter = collections.Counter(values.tolist())
    n = len(values)
    return dict(
        articles=n,
        distribution=[dict(companies=k, articles=v, fraction=v/n if n else None)
                      for k, v in sorted(counter.items())],
        mean=float(values.mean()) if n else None,
        minimum=int(values.min()) if n else None,
        median=float(np.median(values)) if n else None,
        p90=float(np.quantile(values, .90)) if n else None,
        p95=float(np.quantile(values, .95)) if n else None,
        maximum=int(values.max()) if n else None,
    )


def stored_flag_counts(series):
    counter = collections.Counter()
    missing = flagged = 0
    for value in series:
        if value is None:
            missing += 1
        elif value:
            flagged += 1
            counter.update(value)
    n = len(series)
    return dict(
        articles=n, metadata_missing=missing, metadata_present=n-missing,
        articles_with_any_stored_flag=flagged,
        articles_with_explicit_empty_flags=n-missing-flagged,
        flags=[dict(flag=k, articles=v, fraction_of_all_articles=v/n if n else None)
               for k, v in sorted(counter.items())],
    )


def count_categories(series, field):
    return [{field: str(key), 'articles': int(value)}
            for key, value in sorted(series.value_counts(dropna=False).items())]


def class_statistics(pairs, classes, thresholds, market_order):
    result = []
    for market in market_order:
        positions = pairs.index[pairs.market.eq(market)].to_numpy(dtype='int64')
        for h, days in enumerate(HORIZONS):
            values = classes[positions, h]
            counts = {name: int(np.count_nonzero(values == code))
                      for code, name in enumerate(CLASS_NAMES)}
            n = len(values)
            invalid = int(np.count_nonzero(values == -1))
            sessions = pd.DataFrame({'anchor_date': pairs.loc[positions, 'anchor_date'].to_numpy()})
            for code, name in enumerate(CLASS_NAMES):
                sessions[name] = (values == code).astype(float)
            session_means = sessions.groupby('anchor_date')[list(CLASS_NAMES)].mean()
            result.append(dict(
                market=market, horizon_days=days, pair_rows=n,
                threshold=float(thresholds[h]), class_counts=counts,
                class_fractions={key: value/n if n else None for key, value in counts.items()},
                nonfinite_label_rows=invalid,
                nonfinite_label_fraction=invalid/n if n else None,
                distinct_anchor_dates=len(session_means),
                equal_session_mean_class_fractions={name: float(session_means[name].mean()) if len(session_means) else None
                                                    for name in CLASS_NAMES},
            ))
    return result


def describe_cohort(docs, pairs, mask, classes, thresholds, market_order):
    """Describe all supplied rows; never silently discard duplicate pairs."""
    ids = docs.index.to_numpy(dtype='int64')
    n_articles, n_pairs = len(docs), len(pairs)
    require(set(pairs.article).issubset(set(ids)), 'Cohort pairs refer outside cohort articles')
    targets_per_article = pairs.groupby('article').ticker.nunique().reindex(ids, fill_value=0).to_numpy()
    mask_counts = np.count_nonzero(mask[ids], axis=1)
    mentioned = {ticker for group in docs.mentioned_tickers for ticker in group}
    target_sets = pairs.groupby('article').ticker.agg(set).to_dict()
    unlabelled_inputs = [len(set(row.mentioned_tickers)-target_sets.get(row.article, set()))
                        for row in docs.itertuples()]
    targets_without_input = sum(len(tickers-set(docs.loc[article, 'mentioned_tickers']))
                                for article, tickers in target_sets.items())
    direct_mask_nodes = int(np.count_nonzero(np.any(mask[ids], axis=0))) if n_articles else 0

    region_year = []
    pair_regions = pairs.article.map(docs.region)
    pair_years = pairs.article.map(docs.year)
    for (region, year), group in docs.groupby(['region', 'year'], sort=True):
        matching = pairs.loc[pair_regions.eq(region) & pair_years.eq(year)]
        region_year.append(dict(
            region=region, year=year, articles=len(group), pair_rows=len(matching),
            article_fraction=len(group)/n_articles if n_articles else None,
            pair_fraction=len(matching)/n_pairs if n_pairs else None,
            unique_target_companies=int(matching.ticker.nunique()),
        ))
    markets = []
    for market in market_order:
        group = pairs.loc[pairs.market.eq(market)]
        per_session = group.groupby('anchor_date').size()
        markets.append(dict(
            market=market, pair_rows=len(group), articles_with_target=int(group.article.nunique()),
            unique_target_companies=int(group.ticker.nunique()),
            pair_fraction=len(group)/n_pairs if n_pairs else None,
            article_fraction=int(group.article.nunique())/n_articles if n_articles else None,
            distinct_anchor_dates=len(per_session),
            first_anchor_date=str(per_session.index.min()) if len(per_session) else None,
            last_anchor_date=str(per_session.index.max()) if len(per_session) else None,
            pair_rows_per_anchor_date=dict(
                mean=float(per_session.mean()) if len(per_session) else None,
                median=float(per_session.median()) if len(per_session) else None,
                p90=float(per_session.quantile(.9)) if len(per_session) else None,
                maximum=int(per_session.max()) if len(per_session) else None,
                largest_session_pair_fraction=float(per_session.max()/len(group)) if len(group) else None,
                top_five_sessions_pair_fraction=float(per_session.nlargest(5).sum()/len(group)) if len(group) else None,
            ),
        ))

    eligible_pairs = pairs.is_direct.to_numpy(dtype=bool) & pairs['_mask_active'].to_numpy(dtype=bool)
    hash_known = docs.body_hash_changed.notna()
    hash_changed = sum(value is True for value in docs.body_hash_changed)
    return dict(
        articles=n_articles, unique_record_ids=int(docs.record_id.nunique()),
        duplicate_record_id_rows=int(docs.record_id.duplicated().sum()),
        target_pair_rows=n_pairs,
        unique_article_ticker_pairs=int(len(pairs[['article', 'ticker']].drop_duplicates())),
        duplicate_article_ticker_pair_rows=int(pairs.duplicated(['article', 'ticker']).sum()),
        articles_with_target_pairs=int(pairs.article.nunique()),
        direct_mask_active_cells=int(mask_counts.sum()),
        pair_rows_with_direct_mask=int(pairs['_mask_active'].sum()),
        direct_pair_rows_with_direct_mask=int(eligible_pairs.sum()),
        non_direct_pair_rows=int((~pairs.is_direct).sum()),
        integrity=dict(
            all_pair_rows_direct_and_masked=bool(eligible_pairs.all()),
            direct_mask_cells_equal_unique_target_pairs=int(mask_counts.sum()) == len(pairs[['article', 'ticker']].drop_duplicates()),
            all_articles_have_target_pairs=bool((targets_per_article > 0).all()),
            target_company_inputs_missing=int(targets_without_input),
        ),
        unique_companies=dict(labeled_targets=int(pairs.ticker.nunique()),
                              mentioned_in_article_inputs=len(mentioned), direct_mask_nodes=direct_mask_nodes),
        source_region_year=region_year, target_markets=markets,
        market_horizon_class_distribution=class_statistics(pairs, classes, thresholds, market_order),
        target_companies_per_article=histogram(targets_per_article),
        direct_mask_companies_per_article=histogram(mask_counts),
        mentioned_companies_per_article=histogram(docs.mentioned_tickers.map(len).to_numpy()),
        unlabelled_mentioned_companies_per_article=histogram(unlabelled_inputs),
        unlabelled_article_company_inputs=int(sum(unlabelled_inputs)),
        source_datasets=count_categories(docs.source_dataset, 'source_dataset'),
        stored_quality_flags_on_retained_articles=stored_flag_counts(docs.quality_flags),
        stored_publication_time_flags_on_retained_articles=stored_flag_counts(docs.time_flags),
        publication_timestamp_status=count_categories(docs.timestamp_status, 'timestamp_status'),
        body_hash_comparison=dict(known_articles=int(hash_known.sum()),
                                  changed_articles=int(hash_changed),
                                  unchanged_articles=int(hash_known.sum())-int(hash_changed),
                                  missing_articles=int((~hash_known).sum())),
    )


def build_audit(out):
    paths = {name: out/name for name in INPUT_NAMES}
    for name, path in paths.items():
        require(path.is_file(), f'Missing prepared input: {name}')
    before = {name: sha(path) for name, path in paths.items()}
    code_path = Path(__file__).resolve()
    code_hash = sha(code_path)
    prepared = read_json(paths['prepared.json'])
    expected = prepared.get('artifact_hashes', {})
    manifest_checks = {}
    for name, digest in before.items():
        manifest_checks[name] = digest == expected[name] if name in expected else None
        require(manifest_checks[name] is not False, f'Prepared manifest hash differs: {name}')
    sizes = prepared['sizes']
    require(isinstance(sizes, list) and sizes and all(type(x) is int and x > 0 for x in sizes),
            'Prepared sizes must be positive integer article budgets')
    require(sizes == sorted(set(sizes)), 'Prepared sizes must be increasing and unique')

    docs = read_article_metadata(paths['articles.jsonl'])
    meta = pd.DataFrame(read_json(paths['companies.json']))
    require(all(isinstance(t, str) for t in meta.ticker), 'Company dictionary tickers must be strings')
    require(meta.ticker.is_unique, 'Company dictionary tickers must be unique')
    require(meta.market.notna().all(), 'Company dictionary contains an unknown market')
    node = dict(zip(meta.ticker, range(len(meta))))
    ticker_market = dict(zip(meta.ticker, meta.market))
    market_order = sorted(meta.market.unique().tolist())
    mask = np.load(paths['direct_mask.npy'], mmap_mode='r', allow_pickle=False)
    require(mask.shape == (len(docs), len(meta)) and mask.dtype == bool,
            'direct_mask shape/dtype does not match article and company order')
    required_columns = ['article', 'ticker', 'split', 'market', 'is_direct', 'y_1d', 'y_3d', 'anchor_date']
    pairs = pd.read_parquet(paths['pairs.parquet'], columns=required_columns).reset_index(drop=True)
    require(pd.api.types.is_integer_dtype(pairs.article.dtype), 'Pair article indexes must be integers')
    require(pairs.article.between(0, len(docs)-1).all(), 'Pair article index out of range')
    require(all(isinstance(t, str) for t in pairs.ticker), 'Pair tickers must be strings')
    pair_nodes = pairs.ticker.map(node)
    require(pair_nodes.notna().all(), 'A pair ticker is absent from the company dictionary')
    require(pd.api.types.is_bool_dtype(pairs.is_direct.dtype), 'is_direct must be boolean')
    require(pairs.anchor_date.notna().all(), 'Pair anchor date is missing')
    require(pairs.market.eq(pairs.ticker.map(ticker_market)).all(), 'Pair market and company market disagree')
    pairs['_mask_active'] = mask[pairs.article.to_numpy(), pair_nodes.to_numpy(dtype='int64')]
    with np.load(paths['label_config.npz'], allow_pickle=False) as cfg:
        thresholds, scales = cfg['thresholds'].copy(), cfg['scales'].copy()
    require(thresholds.shape == (2,) and np.isfinite(thresholds).all() and (thresholds >= 0).all(),
            'Expected two finite nonnegative frozen label thresholds')
    require(scales.shape == (2,) and np.isfinite(scales).all() and (scales > 0).all(),
            'Expected two finite positive frozen magnitude scales')
    # Match the existing stored-label contract without importing a trainer:
    # float32 y, original saved thresholds, strict inequalities, equality neutral.
    y = pairs[['y_1d', 'y_3d']].to_numpy(dtype='float32')
    classes = np.where(y > thresholds, 2, np.where(y < -thresholds, 0, 1))
    classes[~np.isfinite(y)] = -1

    training = docs.loc[docs.split.eq('train')].sort_values('supervised_rank')
    require(len(training) == prepared['maximum_training_articles'], 'Training article count differs from prepared')
    require(training.supervised_rank.tolist() == list(range(len(training))),
            'supervised_rank must cover each training ordinal exactly once')
    require(sizes[-1] == len(training), 'Largest prepared budget must cover the prepared training articles')
    training_pairs = pairs.loc[pairs.split.eq('train')]
    require(set(training_pairs.article).issubset(set(training.index)), 'Training pair points to a nontraining article')
    require(len(training_pairs) == prepared['maximum_training_pairs'], 'Training pair count differs from prepared')
    known_companies = set(meta.ticker)
    require(all(set(tickers) <= known_companies for tickers in training.mentioned_tickers),
            'Training article mentions a company outside the model dictionary')

    cohorts = []
    previous_size, previous_pairs = 0, 0
    previous_ids = set()
    for size in sizes:
        selected = training.loc[training.supervised_rank < size]
        ids = set(selected.index)
        require(len(selected) == size and previous_ids <= ids, 'Article budgets are not nested prefixes')
        selected_pairs = training_pairs.loc[training_pairs.article.isin(ids)]
        stats = describe_cohort(selected, selected_pairs, mask, classes, thresholds, market_order)
        stats['article_budget'] = size
        stats['added_since_previous_budget'] = dict(
            previous_budget=previous_size, articles=size-previous_size,
            target_pair_rows=len(selected_pairs)-previous_pairs,
        )
        expected_regions = prepared.get('size_cohorts', {}).get(str(size))
        actual_regions = {r['region']+':'+r['year']: r['articles'] for r in stats['source_region_year']}
        stats['prepared_region_year_counts_match'] = actual_regions == expected_regions if expected_regions is not None else None
        require(stats['prepared_region_year_counts_match'] is not False,
                f'Region/year counts differ from prepared for budget {size}')
        cohorts.append(stats)
        previous_size, previous_pairs, previous_ids = size, len(selected_pairs), ids

    validation = pairs.loc[pairs.split.eq('val')]
    retained_validation = validation.loc[validation.is_direct & validation['_mask_active']]
    validation_summary = dict(
        common_to_all_budgets=True, original_pair_rows=len(validation),
        original_article_count=int(validation.article.nunique()),
        retained_direct_pair_rows=len(retained_validation),
        retained_article_count=int(retained_validation.article.nunique()),
        original_market_horizon_classes=class_statistics(validation, classes, thresholds, market_order),
        retained_market_horizon_classes=class_statistics(retained_validation, classes, thresholds, market_order),
    )
    require(len(retained_validation) == prepared['validation_pairs'], 'Retained validation count differs from prepared')
    require(len(validation) == prepared['original_direct_pairs'], 'Original validation count differs from prepared')
    after = {name: sha(path) for name, path in paths.items()}
    require(after == before, 'Inputs changed during the read-only audit; no report written')
    require(sha(code_path) == code_hash, 'Audit code changed during execution; no report written')
    return dict(
        schema_version=1, generated_at_utc=datetime.now(timezone.utc).isoformat(),
        purpose='Descriptive composition of prepared nested training cohorts; not selection, deduplication, filter tuning, or model choice',
        study_directory=str(out), inputs_unchanged=True,
        input_sha256=before, input_paths={name: str(path) for name, path in paths.items()},
        prepared_manifest_hash_checks=manifest_checks,
        code_path=str(code_path), code_sha256=code_hash,
        experiment_or_training_modules_imported=False, prices_or_predictions_read=False,
        thresholds_refit=False, data_rows_removed=False,
        label_contract=dict(
            horizons_days=list(HORIZONS), outcome_columns=['y_1d', 'y_3d'], comparison_input_dtype='float32',
            thresholds_dtype=str(thresholds.dtype), thresholds=thresholds.tolist(), scales=scales.tolist(),
            classes={'0': 'down', '1': 'neutral', '2': 'up'},
            rule='down: y < -threshold; neutral: -threshold <= y <= threshold; up: y > threshold; nonfinite recorded separately',
        ),
        requested_article_budgets=prepared.get('requested_sizes'), prepared_article_budgets=sizes,
        cohorts=cohorts, common_validation=validation_summary,
        caveats=[
            'Class distributions count article-company target pairs, not independent news events or price sessions.',
            'Equal-session class proportions average per-date proportions; they are descriptive class composition, not model accuracy.',
            'Target market article counts can overlap because one article may target companies in both markets.',
            'Mentioned input companies and labeled target companies need not be identical; distributions are separate.',
            'All prepared training pair rows are counted as stored; duplicate counts are reported, never removed.',
            'Stored quality/time flags are existing processing metadata on retained articles, not a fresh semantic review or residual-contamination detector.',
            'A removal flag can describe successful cleaning; an empty flag list does not certify clean content, historical identity, or correct timestamps.',
            'No existing label values, thresholds, sample membership, model settings, source files, or review freezes are changed.',
            'This descriptive audit does not establish predictive improvement or a 65% accuracy claim.',
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='Completed prepared study directory')
    parser.add_argument('--output', type=Path, help='New JSON path; default: OUT/cohort_composition_audit.json')
    args = parser.parse_args()
    out = args.out.resolve()
    output = (args.output or out/'cohort_composition_audit.json').resolve()
    require(output not in {out/name for name in INPUT_NAMES}, 'Output cannot replace an input file')
    require(not output.exists(), 'Output already exists; use --output with a new JSON path')
    result = build_audit(out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(dict(output=str(output), inputs_unchanged=result['inputs_unchanged'],
        cohorts=[dict(articles=c['articles'], target_pair_rows=c['target_pair_rows']) for c in result['cohorts']]),
        ensure_ascii=False))


if __name__ == '__main__':
    main()
