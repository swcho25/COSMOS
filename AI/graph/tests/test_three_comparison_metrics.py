"""Independent small-population checks for CE reweighting and goal selection."""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch
from torch.nn import functional as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from three_comparison_metrics import (
    balanced_direction_loss, make_selection_weights, make_train_weights, score_direction,
)


def fixture():
    # Train: KOSPI day A has three pairs across two articles, day B one pair;
    # NASDAQ has one day with one pair. Val includes a completely missed date.
    rows = [
        (0, 0, "KOSPI", "2022-01-03", "train", True),
        (0, 1, "KOSPI", "2022-01-03", "train", True),
        (1, 0, "KOSPI", "2022-01-03", "train", True),
        (2, 1, "KOSPI", "2022-01-04", "train", True),
        (3, 2, "NASDAQ", "2022-01-03", "train", True),
        (4, 0, "KOSPI", "2023-01-03", "val", True),
        (4, 1, "KOSPI", "2023-01-03", "val", True),
        (5, 0, "KOSPI", "2023-01-04", "val", True),
        (6, 2, "NASDAQ", "2023-01-03", "val", True),
        (7, 2, "NASDAQ", "2026-01-03", "test", True),
        (0, 2, "NASDAQ", "2022-01-03", "train", False),
    ]
    pairs = pd.DataFrame(rows, columns=["article", "node", "market", "anchor_date", "split", "is_direct"])
    docs = [{"split": s} for s in ["train"] * 4 + ["val", "excluded", "val", "excluded"]]
    mask = np.zeros((8, 3), dtype=bool)
    for row in pairs.iloc[:10].itertuples():
        mask[row.article, row.node] = True
    mask[4, 1] = False
    mask[5, 0] = False
    return SimpleNamespace(docs=docs, pairs=pairs, mask=mask,
                           article_idx=pairs.article.to_numpy(), node_idx=pairs.node.to_numpy())


class TrainWeightsTest(unittest.TestCase):
    def test_exact_market_session_horizon_mass(self):
        d = fixture()
        w = make_train_weights(d)
        self.assertEqual(w.shape, (8, 3, 2))
        self.assertEqual(w.dtype, np.float32)
        self.assertAlmostEqual(float(w.sum(dtype=np.float64)), 4., places=6)
        for h in range(2):
            self.assertAlmostEqual(float(w[0, 0, h]), 1 / 6, places=7)
            self.assertAlmostEqual(float(w[:3, :, h].sum()), 1., places=6)
            self.assertAlmostEqual(float(w[3, :, h].sum()), 1., places=6)
            self.assertAlmostEqual(float(w[:2, :, h].sum()), .5, places=6)
            self.assertAlmostEqual(float(w[2, :, h].sum()), .5, places=6)
        self.assertTrue((w[4:] == 0).all())
        self.assertTrue((w[~d.mask] == 0).all())

    def test_train_weights_ignore_outcomes_and_future_frequencies(self):
        d = fixture()
        before = make_train_weights(d)
        d.cls = "must not read labels"
        d.y = "must not read outcomes"
        d.mag = "must not read magnitude"
        d.val = "must not derive train weights from validation"
        d.pairs["y_1d"] = np.nan
        d.pairs["y_3d"] = np.inf
        later = d.pairs.split.ne("train")
        d.pairs.loc[later, "market"] = "UNKNOWN_FUTURE_MARKET"
        d.pairs.loc[later, "anchor_date"] = "changed_future_date"
        np.testing.assert_array_equal(make_train_weights(d), before)

    def test_unrepresented_or_duplicate_mask_entry_is_rejected(self):
        d = fixture()
        d.mask[1, 1] = True
        with self.assertRaisesRegex(ValueError, "unique pair"):
            make_train_weights(d)
        d = fixture()
        d.pairs = pd.concat([d.pairs, d.pairs.iloc[[0]]], ignore_index=True)
        d.article_idx = d.pairs.article.to_numpy()
        d.node_idx = d.pairs.node.to_numpy()
        with self.assertRaisesRegex(ValueError, "unique pair"):
            make_train_weights(d)


class DirectionLossTest(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(7)
        self.logits = torch.randn(3, 4, 2, 3, dtype=torch.float64, requires_grad=True)
        self.pred = torch.randn(3, 4, 2, dtype=torch.float64, requires_grad=True)
        self.cls = torch.randint(0, 3, (3, 4, 2))
        self.mag = torch.rand(3, 4, 2, dtype=torch.float64)
        self.mask = torch.tensor([[1, 1, 0, 0], [0, 1, 1, 1], [1, 0, 0, 0]], dtype=torch.bool)

    def test_uniform_article_weights_reproduce_existing_loss(self):
        weights = self.mask[..., None].expand_as(self.mag) / (2 * self.mask.sum(-1)[:, None, None])
        loss = balanced_direction_loss(self.logits, self.pred, self.cls, self.mag, self.mask, weights)
        ce = F.cross_entropy(self.logits.reshape(-1, 3), self.cls.reshape(-1), reduction="none").reshape_as(self.mag)
        err = F.smooth_l1_loss(self.pred, self.mag, reduction="none")
        original = (((ce + err).mean(-1) * self.mask).sum(-1) / self.mask.sum(-1)).mean()
        self.assertAlmostEqual(loss.item(), original.item(), places=7)

    def test_only_direction_changes_magnitude_and_masked_gradients_preserved(self):
        weights = torch.arange(1, 25, dtype=torch.float64).reshape(3, 4, 2) / 12
        total = balanced_direction_loss(self.logits, self.pred, self.cls, self.mag, self.mask, weights)
        ce = F.cross_entropy(self.logits.reshape(-1, 3), self.cls.reshape(-1), reduction="none").reshape_as(self.mag)
        direction = sum(float((ce[i, j] * weights[i, j]).sum())
                        for i in range(3) for j in range(4) if self.mask[i, j]) / 3
        # Independent original article magnitude formula, ignoring the helper.
        magnitude = np.mean([np.mean([
            F.smooth_l1_loss(self.pred[i, j], self.mag[i, j]).detach().item()
            for j in range(4) if self.mask[i, j]]) for i in range(3)])
        self.assertAlmostEqual(total.item() - direction, magnitude, places=12)
        total.backward()
        self.assertTrue(torch.equal(self.logits.grad[~self.mask], torch.zeros_like(self.logits.grad[~self.mask])))
        self.assertTrue(torch.equal(self.pred.grad[~self.mask], torch.zeros_like(self.pred.grad[~self.mask])))

    def test_batch_weighted_average_equals_full_objective(self):
        w = torch.arange(24, dtype=torch.float64).reshape(3, 4, 2) / 10
        args = (self.logits, self.pred, self.cls, self.mag, self.mask, w)
        full = balanced_direction_loss(*args)
        split = (balanced_direction_loss(*(x[:1] for x in args))
                 + 2 * balanced_direction_loss(*(x[1:] for x in args))) / 3
        self.assertAlmostEqual(full.item(), split.item(), places=12)


class SelectionWeightsTest(unittest.TestCase):
    def test_original_denominators_and_absent_date_are_retained(self):
        d = fixture()
        indices = np.array([8, 5])  # Deliberately nonmonotonic.
        w = make_selection_weights(d, indices)
        self.assertEqual(w.dtype, np.float64)
        np.testing.assert_array_equal(w, [[1 / 4, 1 / 4], [1 / 16, 1 / 16]])
        actual = np.array([[0, 2], [1, 1]])
        p = np.eye(3)[actual]
        self.assertEqual(score_direction(p, actual, w), .625)
        # NASDAQ scores 1/1, KOSPI scores mean(1/2, 0/1)=1/4.
        # Average the four market/horizon cells: (.25+.25+1+1)/4.
        self.assertEqual(score_direction(p, actual, w), (.25 + .25 + 1 + 1) / 4)
        p[0, 0] = [0, 1, 0]
        self.assertEqual(score_direction(p, actual, w), .375)

    def test_train_frequencies_and_label_values_do_not_set_selection_weights(self):
        d = fixture()
        ids = np.array([5, 8])
        before = make_selection_weights(d, ids)
        d.cls = object()
        d.pairs.loc[d.pairs.split.ne("val"), "anchor_date"] = "irrelevant"
        d.pairs.loc[d.pairs.split.ne("val"), "market"] = "irrelevant"
        np.testing.assert_array_equal(make_selection_weights(d, ids), before)

    def test_reject_nonpopulation_duplicates_and_abstentions(self):
        d = fixture()
        for ids in ([0, 8], [5, 5], [5, 6], [5, 7]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                make_selection_weights(d, np.array(ids))

    def test_reject_invalid_scores_instead_of_silent_argmax(self):
        p = np.ones((1, 2, 3)) / 3
        y = np.ones((1, 2), dtype=int)
        w = np.ones((1, 2)) / 2
        for bad in (p * 3, np.full_like(p, np.nan)):
            with self.assertRaises(ValueError):
                score_direction(bad, y, w)
        with self.assertRaises(ValueError):
            score_direction(p, y, w * 2)


if __name__ == "__main__":
    unittest.main()
