"""Synthetic routing/calibration checks; no real test labels or GPU inference."""
from pathlib import Path
import copy
import json
import math
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_cascade_routing as c


def score(label="include", value=0.995):
    return {key: value if key == label else (1 - value) / 2 for key in c.LABELS}


def ids(prefix, count):
    return [prefix + str(i) for i in range(count)]


def candidate():
    return c.tune_policy(["include"] * 30 + ["exclude"] * 30,
                         [score("include")] * 30 + [score("exclude")] * 30,
                         dev_ids=ids("dev-pair-", 60), dev_group_ids=ids("dev-group-", 60))


def policy(include=200, exclude=200):
    count = include + exclude
    return c.confirm_policy(["include"] * include + ["exclude"] * exclude,
                            [score("include")] * include + [score("exclude")] * exclude,
                            candidate(), cal_ids=ids("cal-pair-", count), cal_group_ids=ids("cal-group-", count))


class Bounds(unittest.TestCase):
    def test_exact_special_cases_and_183_requirement(self):
        self.assertEqual(c.agreement_lower_bound(0, 0), 0)
        self.assertEqual(c.agreement_lower_bound(0, 20), 0)
        self.assertAlmostEqual(c.agreement_lower_bound(183, 183, .025), .025 ** (1 / 183))
        self.assertLess(c.agreement_lower_bound(182, 182, .025), .98)
        self.assertGreaterEqual(c.agreement_lower_bound(183, 183, .025), .98)
        self.assertAlmostEqual(c.agreement_lower_bound(1, 1, .05), .05)

    def test_independent_scipy_crosscheck(self):
        from scipy.stats import beta
        for successes, total in [(1, 2), (3, 10), (90, 100), (497, 512), (508, 512), (180, 183)]:
            for alpha in [.05, .025, .005]:
                with self.subTest(successes=successes, total=total, alpha=alpha):
                    expected = float(beta.ppf(alpha, successes, total - successes + 1))
                    self.assertAlmostEqual(c.agreement_lower_bound(successes, total, alpha), expected, places=12)

    def test_invalid_counts_and_confidences(self):
        for args in [(2, 1), (-1, 5), (True, 2), (1, 2, 0), (1, 2, float("nan"))]:
            with self.assertRaises(ValueError):
                c.agreement_lower_bound(*args)


class Calibration(unittest.TestCase):
    def test_grid_frozen_config_serializable(self):
        settings = c.config_with_defaults()
        settings["threshold_grid"].append(.999)
        self.assertNotIn(.999, c.DEFAULT_CONFIG["threshold_grid"])
        for bad in [{"threshold_grid": [.8, .8]}, {"threshold_grid": [float("nan")]},
                    {"threshold_grid": [.5]}, {"calibration_split": "test"}, {"made_up": 5}]:
            with self.assertRaises(ValueError):
                c.config_with_defaults(bad)

    def test_dev_candidate_cannot_route_before_cal(self):
        frozen = candidate()
        self.assertEqual(frozen["directions"]["include"]["threshold"], .99)
        with self.assertRaises(ValueError):
            c.route(score(), frozen)

    def test_dev_selects_most_coverage_with_declared_tie_break(self):
        frozen = c.tune_policy(["include"] * 40 + ["exclude"] * 10,
                              [score(value=.91)] * 20 + [score(value=.995)] * 20 + [score(value=.85)] * 10)
        self.assertEqual(frozen["directions"]["include"]["threshold"], .9)
        self.assertFalse(frozen["directions"]["exclude"]["enabled_candidate"])

    def test_confirm_fixed_thresholds_and_one_direction_only(self):
        selected = policy(include=183, exclude=30)
        self.assertTrue(selected["directions"]["include"]["enabled"])
        self.assertFalse(selected["directions"]["exclude"]["enabled"])
        self.assertEqual(selected["multiplicity"], 2)
        self.assertAlmostEqual(selected["per_trial_alpha"], .025)
        self.assertEqual(selected["directions"]["include"]["threshold"], .99)
        self.assertEqual(c.route(score("exclude"), selected)["reason"], "direction_disabled")

    def test_cal_failure_disables_and_does_not_change_threshold(self):
        n = 200
        frozen = candidate()
        selected = c.confirm_policy(["include"] * 199 + ["review"], [score()] * n, frozen,
                                    cal_ids=ids("new-", n), cal_group_ids=ids("g-", n))
        self.assertFalse(selected["directions"]["include"]["enabled"])
        self.assertEqual(selected["directions"]["include"]["candidate_threshold"], .99)
        self.assertEqual(selected["trials"][0]["teacher_review_pairs"], 1)
        self.assertEqual(selected["state"], "disabled_all_teacher_no_acceleration")
        self.assertEqual(frozen, candidate())

    def test_missing_groups_fails_closed(self):
        selected = c.select_policy(["include"] * 1000, [score()] * 1000)
        self.assertTrue(selected["group_ids_missing"])
        self.assertFalse(selected["directions"]["include"]["enabled"])

    def test_cal_grid_search_has_full_multiplicity_correction(self):
        selected = c.select_policy(["include"] * 200, [score()] * 200, cal_group_ids=ids("g", 200))
        self.assertEqual(selected["multiplicity"], 10)
        self.assertFalse(selected["directions"]["include"]["enabled"])
        self.assertTrue(policy(200, 0)["directions"]["include"]["enabled"])

    def test_duplicate_group_and_dev_cal_overlap_rejected(self):
        with self.assertRaises(ValueError):
            c.select_policy(["include"] * 200, [score()] * 200, cal_group_ids=["same"] * 200)
        frozen = candidate()
        with self.assertRaisesRegex(ValueError, "leakage"):
            c.confirm_policy(["include"], [score()], frozen, cal_ids=["dev-pair-0"], cal_group_ids=["new"])
        with self.assertRaisesRegex(ValueError, "required"):
            c.confirm_policy(["include"], [score()], frozen)

    def test_frozen_config_and_fingerprints_cannot_silently_change(self):
        frozen = candidate()
        with self.assertRaisesRegex(ValueError, "configuration cannot change"):
            c.confirm_policy([], [], frozen, {"min_support": 1})
        broken = copy.deepcopy(frozen)
        broken["directions"]["include"]["threshold"] = .8
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            c.confirm_policy([], [], broken)
        selected = policy()
        selected["directions"]["include"]["threshold"] = .8
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            c.route(score(), selected)


class RoutesAndEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.selected = policy()

    def test_auto_outputs_are_decisions_not_grounded_graph_edges(self):
        routed = c.route(score(), self.selected)
        self.assertEqual(routed["source"], "student")
        self.assertEqual(routed["decision"], "include")
        self.assertIsNone(routed["relation"])
        self.assertEqual(routed["evidence"], [])
        self.assertFalse(routed["graph_materialization_ready"])
        self.assertEqual(routed["evidence_status"], "not_generated")
        self.assertEqual(c.route(score(), self.selected, require_grounded_selection=True)["reason"], "grounded_selection_requested")

    def test_review_invalid_errors_ood_ties_force_teacher(self):
        cases = [({"include": .5, "exclude": .5, "review": 0}, "score_tie"),
                 (score("review"), "student_review"),
                 ({"include": .99, "exclude": .01}, "invalid_score_keys"),
                 ({"include": math.nan, "exclude": 0, "review": 0}, "invalid_score_value"),
                 ({"include": True, "exclude": 0, "review": 0}, "invalid_score_value"),
                 ({"include": .99, "exclude": .2, "review": 0}, "scores_do_not_sum_to_one")]
        for probabilities, expected in cases:
            result = c.route(probabilities, self.selected)
            self.assertTrue(result["needs_teacher"])
            self.assertEqual(result["reason"], expected)
        self.assertEqual(c.route(score(), self.selected, ["truncated"])["reason"], "upstream_validation_error")
        self.assertEqual(c.route(score(), self.selected, ood=True)["reason"], "out_of_distribution")
        self.assertEqual(c.route(score(), self.selected, ood="False")["reason"], "invalid_ood_flag")

    def evaluate(self, labels, probs, **kwargs):
        return c.evaluate_cascade(labels, probs, self.selected, test_ids=ids("test-pair-", len(labels)),
                                  test_group_ids=ids("test-group-", len(labels)), **kwargs)

    def test_disjoint_error_counts_and_fallback_preserves_teacher_review(self):
        before = json.dumps(self.selected, sort_keys=True)
        result = self.evaluate(["exclude", "include", "review", "review", "review"],
                               [score("include"), score("exclude"), score("include"), score("exclude"), score("review")])
        self.assertEqual(result["total_disagreements"], 4)
        for field in ("false_include_vs_teacher_exclude", "false_exclude_vs_teacher_include",
                      "teacher_review_to_student_include", "teacher_review_to_student_exclude"):
            self.assertEqual(result[field], 1)
        self.assertEqual(result["rows"][-1]["cascade_decision"], "review")
        self.assertEqual(result["teacher_agreement"], .2)
        self.assertFalse(result["is_accuracy"])
        self.assertEqual(json.dumps(self.selected, sort_keys=True), before)

    def test_external_ai_reference_wrong_accept_and_relevant_miss_separate(self):
        result = self.evaluate(["include", "exclude", "review"], [score("exclude"), score("include"), score("review")],
                               external_reference=["include", "exclude", "include"], reference_kind="ai_reference_not_human_gold")
        comparison = result["external_reference_comparison"]
        self.assertEqual(comparison["cascade"]["wrong_accept"], 1)
        self.assertEqual(comparison["cascade"]["relevant_miss_exclude"], 1)
        self.assertEqual(comparison["cascade"]["relevant_defer_review"], 1)
        self.assertFalse(comparison["is_independent_factual_accuracy_estimate"])
        with self.assertRaisesRegex(ValueError, "reference_kind"):
            self.evaluate(["include"], [score()], external_reference=["include"])

    def test_test_cal_and_dev_isolation_cannot_be_bypassed(self):
        for pair, group in [("cal-pair-0", "x"), ("x", "cal-group-0"), ("dev-pair-0", "x"), ("x", "dev-group-0")]:
            with self.assertRaisesRegex(ValueError, "leakage"):
                c.evaluate_cascade(["include"], [score()], self.selected, test_ids=[pair], test_group_ids=[group])
        with self.assertRaisesRegex(ValueError, "required"):
            c.evaluate_cascade(["include"], [score()], self.selected)

    def test_all_fallback_is_no_acceleration_even_with_perfect_agreement(self):
        result = self.evaluate(["include"] * 512, [score("review")] * 512)
        self.assertEqual(result["teacher_agreement"], 1)
        self.assertEqual(result["auto_coverage"], 0)
        self.assertEqual(result["state"], "no_acceleration_on_this_test")
        self.assertTrue(result["pilot_gates"]["decision_parity_pass"])
        self.assertFalse(result["pilot_gates"]["coverage_pass"])
        self.assertFalse(result["pilot_gates"]["all_pass"])

    def test_global_upper_bound_four_of_512_pass_five_fail(self):
        for errors, expected in [(4, True), (5, False)]:
            result = self.evaluate(["exclude"] * errors + ["include"] * (512 - errors), [score()] * 512,
                                   timings={"student_total_seconds": 1, "teacher_all_total_seconds": 100, "fallback_teacher_total_seconds": 0})
            self.assertEqual(result["pilot_gates"]["decision_parity_pass"], expected)
            self.assertEqual(result["pilot_gates"]["all_pass"], expected)
            self.assertEqual(result["timing_comparison"]["measured_speedup"], 100)

    def test_unmeasured_speed_never_completes_pilot_gates(self):
        result = self.evaluate(["include"] * 512, [score()] * 512)
        self.assertTrue(result["pilot_gates"]["decision_parity_pass"])
        self.assertTrue(result["pilot_gates"]["coverage_pass"])
        self.assertIsNone(result["pilot_gates"]["measured_speed_pass"])
        self.assertFalse(result["pilot_gates"]["all_pass"])

    def test_slower_fallback_rerun_is_measured_failure_not_rejected(self):
        result = self.evaluate(["include"] * 512, [score()] * 512,
                               timings={"student_total_seconds": 1, "teacher_all_total_seconds": 100, "fallback_teacher_total_seconds": 110})
        self.assertLess(result["timing_comparison"]["measured_speedup"], 1)
        self.assertFalse(result["pilot_gates"]["measured_speed_pass"])
        self.assertFalse(result["pilot_gates"]["all_pass"])

    def test_empty_test_no_zero_denominator_or_success_claim(self):
        result = self.evaluate([], [])
        self.assertIsNone(result["teacher_agreement"])
        self.assertIsNone(result["teacher_disagreement_upper_bound_95"])
        self.assertFalse(result["pilot_gates"]["all_pass"])


if __name__ == "__main__":
    unittest.main()
