"""Guard train-only streaming export, source provenance and nested cohorts."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spark import export_news_quality as prior
from spark import export_news_scale as scale


class ScaleExportTests(unittest.TestCase):
    def setUp(self):
        self.allowed = Path(__file__).resolve().parents[1] / 'artifacts'
        self.temp = tempfile.TemporaryDirectory(dir=self.allowed, prefix='test-scale-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.allowed.resolve()))
        self.temp.cleanup()

    def article(self, rid='source-1', **changes):
        value = dict(record_id=rid, region='domestic', year='2020',
                     published_date='2020-05-01', published_at_raw='2020/05/01 09:31',
                     published_timezone=None, metadata_json='{"raw":"unchanged"}',
                     body='원문\n본문과 원래 숫자 123', sampling_rank=1,
                     sampling_hash=scale.sample_key(rid))
        value.update(changes)
        return value

    def shard(self, rows, name='source.jsonl'):
        path = self.root / name
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n'
                                for row in rows), encoding='utf8')
        return path

    def test_prior_cohort_and_hash_prefix_are_preserved(self):
        self.assertEqual(scale.KEYWORDS, prior.KEYWORDS)
        self.assertEqual(scale.NEWS, prior.NEWS)
        self.assertEqual(scale.MENTIONS, prior.MENTIONS)
        self.assertEqual(scale.META, prior.META)
        ids = [f'기사-{index}' for index in range(37)]
        expected = sorted(ids, key=lambda rid: hashlib.sha256(
            ('financial-quality-v1:' + rid).encode('utf8')).hexdigest())
        actual = sorted(reversed(ids), key=scale.sample_key)
        self.assertEqual(actual, expected)
        self.assertTrue(set(actual[:5]) < set(actual[:25]))

    def test_merge_preserves_original_bytes_and_raw_metadata(self):
        path = self.shard([self.article()])
        output = self.root / 'merged.jsonl'
        self.assertEqual(scale.merge_cohorts([path], output,
                         {('domestic', '2020'): 1, ('overseas', '2019'): 0}, 25), 1)
        self.assertEqual(output.read_bytes(), path.read_bytes())

    def test_cross_cohort_duplicate_identity_is_an_error(self):
        path = self.shard([self.article(), self.article(region='overseas')])
        with self.assertRaisesRegex(ValueError, 'duplicate source ID'):
            scale.merge_cohorts([path], self.root / 'merged.jsonl', {}, 25)

    def test_holdout_and_partition_date_mismatch_are_errors(self):
        for index, row in enumerate((self.article(year='2023', published_date='2023-01-01'),
                                     self.article(published_date='2023-01-01'))):
            with self.subTest(row=row):
                path = self.shard([row], f'source-{index}.jsonl')
                with self.assertRaises(ValueError):
                    scale.merge_cohorts([path], self.root / f'merged-{index}.jsonl', {}, 25)

    def test_count_and_sampling_hash_integrity(self):
        cases = [(self.article(), {}),
                 (self.article(sampling_hash='not-the-source-hash'), {('domestic', '2020'): 1}),
                 (self.article(sampling_rank=26), {('domestic', '2020'): 1})]
        for index, (row, expected) in enumerate(cases):
            with self.subTest(index=index):
                path = self.shard([row], f'source-{index}.jsonl')
                with self.assertRaises(ValueError):
                    scale.merge_cohorts([path], self.root / f'merged-{index}.jsonl', expected, 25)

    def test_existing_output_is_never_overwritten(self):
        source = self.shard([self.article()])
        existing = self.root / 'existing.jsonl'
        existing.write_text('protected', encoding='utf8')
        with self.assertRaises(FileExistsError):
            scale.merge_cohorts([source], existing, {('domestic', '2020'): 1}, 25)
        self.assertEqual(existing.read_text(encoding='utf8'), 'protected')

    def test_spark_partition_order_cannot_change_output(self):
        rows = [self.article(f'row-{index}', sampling_rank=index + 1) for index in range(7)]
        first = self.shard(rows, 'first.jsonl')
        second = self.shard(list(reversed(rows)), 'second.jsonl')
        expected = {('domestic', '2020'): len(rows)}
        a, b = self.root / 'a.jsonl', self.root / 'b.jsonl'
        scale.merge_cohorts([first], a, expected, 25)
        scale.merge_cohorts([second], b, expected, 25)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_duplicate_or_missing_sampling_ranks_are_errors(self):
        for index, ranks in enumerate(((1, 1), (1, 3))):
            path = self.shard([self.article(f'row-{i}', sampling_rank=rank)
                               for i, rank in enumerate(ranks)], f'source-{index}.jsonl')
            with self.subTest(ranks=ranks), self.assertRaises(ValueError):
                scale.merge_cohorts([path], self.root / f'merged-{index}.jsonl',
                                   {('domestic', '2020'): 2}, 25)

    def prior_export(self):
        previous = self.root/'previous'
        previous.mkdir()
        scale.dump(previous/'protocol.json', dict(news_source=scale.NEWS,
            mentions_source=scale.MENTIONS, years=list(scale.YEARS), regions=list(scale.REGIONS),
            keyword_regex=scale.KEYWORDS,
            sampling='SHA256 financial-quality-v1:record_id ascending per region/year; ID tie breaker',
            keyword_scope='title plus first 1200 body characters',
            company_filter='confidence >= 0.9 and is_sports == false',
            article_filter='text_eligible and is_body_representative', labels_used=False))
        rows = [self.article('complete', year='2017', published_date='2017-01-01'),
                self.article('capped', year='2018', published_date='2018-01-01')]
        candidate = previous/'training_candidates.jsonl'
        candidate.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf8')
        stats = [dict(region=region, year=year, linked_rows=0, conflicting_record_ids=0,
                      invalid_record_ids=0, selected_articles=0, eligible_articles=0)
                 for region in scale.REGIONS for year in scale.YEARS]
        stats[0].update(selected_articles=1, eligible_articles=1, linked_rows=1)
        stats[1].update(selected_articles=1, eligible_articles=5, linked_rows=5)
        scale.dump(previous/'manifest.json', dict(complete=True, training_candidates=2,
            training_candidates_sha256=scale.file_sha(candidate), eligible_candidates=stats))
        output = self.root/'next'
        (output/'cohorts').mkdir(parents=True)
        return previous, output

    def test_reuse_only_exhausted_cohorts_from_verified_canonical(self):
        previous, output = self.prior_export()
        reusable, audit = scale.reuse_exhaustive_cohorts(previous, output, 10)
        self.assertIn(('domestic', '2017'), reusable)
        self.assertNotIn(('domestic', '2018'), reusable)
        self.assertFalse((output/'cohorts/domestic_2018.jsonl').exists())
        record = json.loads((output/'cohorts/domestic_2017.jsonl').read_text())
        self.assertEqual(record['record_id'], 'complete')
        self.assertEqual(audit['manifest_sha256'], scale.file_sha(previous/'manifest.json'))

    def test_reuse_rejects_tampering_and_changed_candidate_definition(self):
        previous, output = self.prior_export()
        path = previous/'training_candidates.jsonl'
        path.write_text(path.read_text()+'\n', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            scale.reuse_exhaustive_cohorts(previous, output, 10)
        protocol = json.loads((previous/'protocol.json').read_text())
        protocol['keyword_regex'] = 'changed'
        (previous/'protocol.json').write_text(json.dumps(protocol), encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'keyword_regex'):
            scale.reuse_exhaustive_cohorts(previous, output, 10)

    def incomplete_export(self):
        previous, output = self.prior_export()
        manifest = json.loads((previous/'manifest.json').read_text())
        self.assertTrue((previous/'manifest.json').resolve().is_relative_to(self.root.resolve()))
        (previous/'manifest.json').unlink()
        protocol = json.loads((previous/'protocol.json').read_text())
        protocol['per_year_region'] = 2
        (previous/'protocol.json').write_text(json.dumps(protocol), encoding='utf8')
        folder = previous/'cohorts'
        folder.mkdir()
        row = self.article('complete', year='2017', published_date='2017-01-01',
                           title='sample', companies=[dict(ticker='005930', confidence=1.)])
        (folder/'domestic_2017.jsonl').write_text(json.dumps(row)+'\n', encoding='utf8')
        scale.dump(folder/'domestic_2017.counts.json', manifest['eligible_candidates'][0])
        # Counts plus a .part file is an interrupted cohort, not a reusable one.
        scale.dump(folder/'domestic_2018.counts.json', manifest['eligible_candidates'][1])
        (folder/'domestic_2018.jsonl.part').write_text('INCOMPLETE; NEVER PARSE', encoding='utf8')
        # A final but capped cohort also must not be read or reused.
        scale.dump(folder/'domestic_2019.counts.json', dict(region='domestic', year='2019',
            linked_rows=5, eligible_articles=5, selected_articles=2,
            conflicting_record_ids=0, invalid_record_ids=0))
        (folder/'domestic_2019.jsonl').write_text('CAPPED; RECOMPUTE', encoding='utf8')
        return previous, output

    def test_partial_recovery_uses_only_final_exhaustive_shards(self):
        previous, output = self.incomplete_export()
        original = (previous/'cohorts/domestic_2017.jsonl').read_bytes()
        reused, audit = scale.recover_exhaustive_cohorts(previous, output, 10)
        self.assertEqual(set(reused), {('domestic', '2017')})
        self.assertTrue(audit['partial_recovery'])
        self.assertFalse(audit['previous_run_complete'])
        self.assertFalse(audit['partial_files_read'])
        self.assertTrue(audit['source_dependencies_unchanged'])
        self.assertEqual((previous/'cohorts/domestic_2017.jsonl').read_bytes(), original)
        self.assertFalse((previous/'manifest.json').exists())
        self.assertEqual(len(list((output/'cohorts').glob('*.jsonl'))), 1)

    def test_partial_recovery_rejects_hash_and_rank_tampering(self):
        previous, output = self.incomplete_export()
        source = previous/'cohorts/domestic_2017.jsonl'
        original = json.loads(source.read_text())
        for index, changes in enumerate((dict(sampling_hash='tampered'), dict(sampling_rank=2),
                                         dict(region='overseas'), dict(year='2023'))):
            source.write_text(json.dumps(original | changes)+'\n', encoding='utf8')
            destination = self.root/f'bad-{index}'
            (destination/'cohorts').mkdir(parents=True)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scale.recover_exhaustive_cohorts(previous, destination, 10)

    def test_partial_recovery_rejects_missing_rows(self):
        previous, output = self.incomplete_export()
        source = previous/'cohorts/domestic_2017.jsonl'
        source.write_text('', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'missing rows'):
            scale.recover_exhaustive_cohorts(previous, output, 10)

    def test_partial_recovery_rejects_nonzero_identity_conflicts(self):
        previous, output = self.incomplete_export()
        path = previous/'cohorts/domestic_2017.counts.json'
        counts = json.loads(path.read_text())
        counts['conflicting_record_ids'] = 1
        path.write_text(json.dumps(counts), encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'zero identity conflicts'):
            scale.recover_exhaustive_cohorts(previous, output, 10)

    def test_partial_recovery_detects_dependency_change_during_copy(self):
        previous, output = self.incomplete_export()
        source = previous/'cohorts/domestic_2017.jsonl'
        original_sha = scale.file_sha
        reads = 0
        def mutate_late(path):
            nonlocal reads
            if Path(path) == source:
                reads += 1
                if reads == 2:
                    source.write_text(source.read_text()+'\n', encoding='utf8')
            return original_sha(path)
        with patch.object(scale, 'file_sha', side_effect=mutate_late):
            with self.assertRaisesRegex(ValueError, 'dependency changed'):
                scale.recover_exhaustive_cohorts(previous, output, 10)


if __name__ == '__main__':
    unittest.main()
