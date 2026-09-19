"""Calibrate a selective student against a frozen teacher, never against test.

The student's three scores rank include/exclude/review; a high score is NOT a
probability that the decision is factually correct. Only calibration rows choose
automatic include/exclude thresholds from a predeclared finite grid. The
preferred API tunes on DEV then confirms two frozen thresholds on CAL; the
separate select_policy API searches CAL with full grid multiplicity correction.
Every
invalid, tied, review-leading, unsupported or out-of-distribution row goes to
the original teacher. Teacher review is a distinct label, not an exclusion.

For each direction/grid point, an exact one-sided Clopper-Pearson lower bound
is Bonferroni corrected over the whole grid. When article/de-duplication groups
are supplied, a group succeeds only when ALL of its auto-routed pairs agree
with the teacher. That is a group agreement bound, NOT a pair accuracy bound.
Independence across the supplied groups remains an assumption, not something
this function can verify. Missing group IDs disable automation by default.

This module returns routing decisions only. It fabricates neither quotes nor
direct/indirect relations and does not emit the grounded graph-selection
schema. A student include still needs separately validated source provenance
and must not be presented as a teacher-grounded edge. No GPU, files or network.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from numbers import Real


VERSION = "kg_cascade_routing_v1"
LABELS = ("include", "exclude", "review")
DEFAULT_CONFIG = {
    "threshold_grid": [0.8, 0.9, 0.95, 0.975, 0.99],
    "min_support": 183,
    "minimum_agreement_lower_bound": 0.98,
    "family_confidence": 0.95,
    "require_group_ids": True,
    "one_pair_per_group": True,
    "dev_min_support": 20,
    "dev_max_disagreement": 0.02,
    "test_max_disagreement_upper_bound": 0.02,
    "test_min_auto_coverage": 0.30,
    "calibration_split": "calibration",
}


def _finite_number(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def config_with_defaults(config=None):
    """Validate and serialize the predeclared grid; never derive it from scores."""
    if config is not None and not isinstance(config, dict):
        raise ValueError("config must be a dictionary")
    supplied = {} if config is None else dict(config)
    if set(supplied) - set(DEFAULT_CONFIG):
        raise ValueError("unknown calibration configuration keys")
    result = {**DEFAULT_CONFIG, **supplied}
    grid = result["threshold_grid"]
    if not isinstance(grid, (list, tuple)) or not grid:
        raise ValueError("threshold_grid must be a nonempty fixed sequence")
    if any(not _finite_number(x) or not 0.5 < x <= 1 for x in grid):
        raise ValueError("grid entries must be finite and in (0.5, 1]")
    if len(set(grid)) != len(grid):
        raise ValueError("duplicate thresholds are not allowed")
    result["threshold_grid"] = sorted(float(x) for x in grid)
    if isinstance(result["min_support"], bool) or not isinstance(result["min_support"], int) or result["min_support"] < 1:
        raise ValueError("min_support must be a positive integer")
    for field in ("minimum_agreement_lower_bound", "family_confidence", "test_max_disagreement_upper_bound", "test_min_auto_coverage"):
        if not _finite_number(result[field]) or not 0 < result[field] < 1:
            raise ValueError(field + " must be finite and in (0, 1)")
        result[field] = float(result[field])
    if type(result["require_group_ids"]) is not bool:
        raise ValueError("require_group_ids must be boolean")
    if type(result["one_pair_per_group"]) is not bool:
        raise ValueError("one_pair_per_group must be boolean")
    if isinstance(result["dev_min_support"], bool) or not isinstance(result["dev_min_support"], int) or result["dev_min_support"] < 1:
        raise ValueError("dev_min_support must be a positive integer")
    if not _finite_number(result["dev_max_disagreement"]) or not 0 <= result["dev_max_disagreement"] < 1:
        raise ValueError("dev_max_disagreement must be finite and in [0, 1)")
    if result["calibration_split"] != "calibration":
        raise ValueError("threshold selection requires the calibration split")
    return result


def _binomial_survival(k, n, probability):
    """P(X >= k), evaluated in log space; used only by the exact bound."""
    if k <= 0:
        return 1.0
    if probability <= 0:
        return 0.0
    if probability >= 1:
        return 1.0
    logp, logq = math.log(probability), math.log1p(-probability)
    logterm = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * logp + (n - k) * logq
    maxlog, scaled = logterm, 1.0
    for j in range(k, n):
        logterm += math.log(n - j) - math.log(j + 1) + logp - logq
        if logterm > maxlog:
            scaled = scaled * math.exp(maxlog - logterm) + 1.0
            maxlog = logterm
        else:
            scaled += math.exp(logterm - maxlog)
    return min(1.0, math.exp(maxlog + math.log(scaled)))


def agreement_lower_bound(successes, total, alpha=0.05):
    """Exact one-sided binomial lower bound, with zero support returning zero."""
    if any(isinstance(x, bool) or not isinstance(x, int) for x in (successes, total)) or not 0 <= successes <= total:
        raise ValueError("successes and total must be valid integer counts")
    if not _finite_number(alpha) or not 0 < alpha < 1:
        raise ValueError("alpha must be finite and in (0, 1)")
    if successes == 0:
        return 0.0
    if successes == total:
        return math.exp(math.log(alpha) / total)
    low, high = 0.0, 1.0
    for _ in range(64):
        midpoint = (low + high) / 2
        if _binomial_survival(successes, total, midpoint) < alpha:
            low = midpoint
        else:
            high = midpoint
    # Lower endpoint is conservative with respect to bisection rounding.
    return low


def _score_error(probs):
    if not isinstance(probs, dict) or set(probs) != set(LABELS):
        return "invalid_score_keys"
    if any(not _finite_number(value) or not 0 <= value <= 1 for value in probs.values()):
        return "invalid_score_value"
    if not math.isclose(math.fsum(probs.values()), 1.0, rel_tol=0, abs_tol=1e-6):
        return "scores_do_not_sum_to_one"
    return None


def _winning_label(probs):
    best = max(probs.values())
    winners = [label for label in LABELS if probs[label] == best]
    return winners[0] if len(winners) == 1 else None


def _routing_reason(probs, errors=None, ood=False):
    if errors:
        return "upstream_validation_error"
    if type(ood) is not bool:
        return "invalid_ood_flag"
    if ood:
        return "out_of_distribution"
    problem = _score_error(probs)
    if problem:
        return problem
    winner = _winning_label(probs)
    if winner is None:
        return "score_tie"
    if winner == "review":
        return "student_review"
    return None


def _validate_labels(labels, field):
    values = list(labels)
    if any(x not in LABELS for x in values):
        raise ValueError(field + " must contain include/exclude/review only")
    return values


def _vector(values, size, default, field):
    result = [default] * size if values is None else list(values)
    if len(result) != size:
        raise ValueError(field + " length does not match rows")
    return result


def _identities(values, size, field, unique=False):
    if values is None:
        return None
    result = list(values)
    if len(result) != size or any(not isinstance(x, str) or not x for x in result):
        raise ValueError(field + " must have one nonempty string per row")
    if unique and len(set(result)) != size:
        raise ValueError(field + " must be unique")
    return result


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def select_policy(cal_labels, cal_probs, config=None, *, cal_ids=None, cal_group_ids=None, errors=None, ood=None):
    """Choose only calibration thresholds, recording every tested grid point.

    cal_labels must be already validated frozen-teacher decisions; this module
    does not turn arbitrary teacher JSON into labels. Caller must establish
    training/development/calibration/test group isolation before calling this.
    Group IDs must cover the SAME duplicate/article groups used for splitting.
    No inference about generalization is made when those groups are dependent.
    """
    settings = config_with_defaults(config)
    labels, probs = _validate_labels(cal_labels, "cal_labels"), list(cal_probs)
    if len(labels) != len(probs):
        raise ValueError("calibration labels and scores differ in length")
    size = len(labels)
    ids = _identities(cal_ids, size, "cal_ids", unique=True)
    groups = _identities(cal_group_ids, size, "cal_group_ids")
    if groups is not None and settings["one_pair_per_group"] and len(set(groups)) != size:
        raise ValueError("one pair per independent calibration group is required")
    error_rows, ood_rows = _vector(errors, size, None, "errors"), _vector(ood, size, False, "ood")
    reasons = [_routing_reason(p, e, o) for p, e, o in zip(probs, error_rows, ood_rows)]
    missing_groups = groups is None and settings["require_group_ids"]
    units = groups if groups is not None else [str(i) for i in range(size)]
    multiplicity = 2 * len(settings["threshold_grid"])
    alpha = (1 - settings["family_confidence"]) / multiplicity
    trials, directions = [], {}
    for label in ("include", "exclude"):
        eligible = []
        for threshold in settings["threshold_grid"]:
            indices = [i for i, p in enumerate(probs) if reasons[i] is None and _winning_label(p) == label and p[label] >= threshold]
            grouped = defaultdict(list)
            for i in indices:
                grouped[units[i]].append(labels[i] == label)
            support = len(grouped)
            successes = sum(all(items) for items in grouped.values())
            bound = agreement_lower_bound(successes, support, alpha)
            passing = not missing_groups and support >= settings["min_support"] and bound >= settings["minimum_agreement_lower_bound"]
            trial = {"direction": label, "threshold": threshold, "routed_pairs": len(indices),
                     "teacher_agree_pairs": sum(labels[i] == label for i in indices),
                     "teacher_review_pairs": sum(labels[i] == "review" for i in indices),
                     "support_units": support, "all_correct_units": successes,
                     "agreement_lower_bound": bound, "alpha": alpha, "eligible": passing}
            trials.append(trial)
            if passing:
                eligible.append(trial)
        if eligible:
            selected = min(eligible, key=lambda x: (-x["routed_pairs"], -x["threshold"]))
            directions[label] = {"enabled": True, **{key: selected[key] for key in
                ("threshold", "routed_pairs", "support_units", "all_correct_units", "agreement_lower_bound")}}
        else:
            directions[label] = {"enabled": False, "threshold": None, "routed_pairs": 0, "support_units": 0,
                                 "all_correct_units": 0, "agreement_lower_bound": None}
    enabled = any(value["enabled"] for value in directions.values())
    policy = {"version": VERSION, "config": settings, "directions": directions, "trials": trials,
              "state": "selective_acceleration" if enabled else "disabled_all_teacher_no_acceleration",
              "calibration_pairs": size, "calibration_pair_ids": ids,
              "calibration_group_ids": sorted(set(groups)) if groups is not None else None,
              "group_ids_missing": missing_groups,
              "bound_unit": "duplicate_article_group_all_correct" if groups is not None else "pair_independence_assumed",
              "bound_is_factual_accuracy": False, "reference_kind": "frozen_teacher_decision_not_human_gold",
              "multiplicity": multiplicity, "per_trial_alpha": alpha,
              "forced_fallback_counts": dict(sorted(Counter(r for r in reasons if r).items())),
              "score_semantics": "class_score_not_probability_of_correctness",
              "assumptions": ["fixed student selected without calibration or test labels",
                              "fixed grid chosen before calibration outcomes",
                              "independent exchangeable calibration groups; future shift is not covered",
                              "teacher agreement does not establish factual correctness"]}
    policy["policy_sha256"] = _fingerprint(policy)
    return policy


def tune_policy(dev_labels, dev_probs, config=None, *, dev_ids=None, dev_group_ids=None, errors=None, ood=None):
    """Pick two thresholds on DEV only; this never authorizes automatic routing.

    DEV disagreement is an empirical tuning criterion, not a confidence bound.
    A threshold with insufficient DEV support stays disabled rather than being
    guessed on calibration data. One frozen candidate per direction proceeds
    to confirm_policy, which may disable it but cannot lower/change it.
    """
    settings = config_with_defaults(config)
    labels, probs = _validate_labels(dev_labels, "dev_labels"), list(dev_probs)
    size = len(labels)
    if size != len(probs):
        raise ValueError("development labels and scores differ in length")
    ids = _identities(dev_ids, size, "dev_ids", unique=True)
    groups = _identities(dev_group_ids, size, "dev_group_ids")
    if groups is not None and settings["one_pair_per_group"] and len(set(groups)) != size:
        raise ValueError("one pair per independent development group is required")
    error_rows, ood_rows = _vector(errors, size, None, "errors"), _vector(ood, size, False, "ood")
    reasons = [_routing_reason(p, e, o) for p, e, o in zip(probs, error_rows, ood_rows)]
    trials, directions = [], {}
    for label in ("include", "exclude"):
        eligible = []
        for threshold in settings["threshold_grid"]:
            indices = [i for i, p in enumerate(probs) if reasons[i] is None and _winning_label(p) == label and p[label] >= threshold]
            disagreements = sum(labels[i] != label for i in indices)
            fraction = disagreements / len(indices) if indices else None
            trial = {"direction": label, "threshold": threshold, "support_pairs": len(indices),
                     "teacher_disagreements": disagreements, "teacher_disagreement_fraction": fraction,
                     "eligible": len(indices) >= settings["dev_min_support"] and fraction <= settings["dev_max_disagreement"]}
            trials.append(trial)
            if trial["eligible"]:
                eligible.append(trial)
        best = min(eligible, key=lambda x: (-x["support_pairs"], -x["threshold"])) if eligible else None
        directions[label] = {"enabled_candidate": best is not None, "threshold": best["threshold"] if best else None}
    candidate = {"version": VERSION, "stage": "dev_frozen_candidate_not_routing_policy", "config": settings,
                 "directions": directions, "dev_trials": trials, "dev_pairs": size,
                 "dev_pair_ids": ids, "dev_group_ids": sorted(set(groups)) if groups is not None else None,
                 "graph_materialization_ready": False, "is_accuracy": False}
    candidate["candidate_sha256"] = _fingerprint(candidate)
    return candidate


def confirm_policy(cal_labels, cal_probs, candidate, config=None, *, cal_ids=None, cal_group_ids=None, errors=None, ood=None):
    """Confirm DEV-frozen thresholds once using independent CAL groups.

    A failure disables that direction. It does not search a new threshold,
    revise the student, or silently reuse CAL as a second development set.
    Confidence alpha is divided by two even if DEV selected only one direction.
    """
    if not isinstance(candidate, dict) or candidate.get("stage") != "dev_frozen_candidate_not_routing_policy":
        raise ValueError("a frozen development candidate is required")
    content = dict(candidate)
    if content.pop("candidate_sha256", None) != _fingerprint(content):
        raise ValueError("development candidate fingerprint mismatch")
    settings = config_with_defaults(candidate["config"] if config is None else config)
    if settings != candidate["config"]:
        raise ValueError("configuration cannot change after development candidate freeze")
    labels, probs = _validate_labels(cal_labels, "cal_labels"), list(cal_probs)
    size = len(labels)
    if size != len(probs):
        raise ValueError("calibration labels and scores differ in length")
    ids = _identities(cal_ids, size, "cal_ids", unique=True)
    groups = _identities(cal_group_ids, size, "cal_group_ids")
    if groups is not None and settings["one_pair_per_group"] and len(set(groups)) != size:
        raise ValueError("one pair per independent calibration group is required")
    for current, previous, label in ((ids, candidate["dev_pair_ids"], "pair"), (groups, candidate["dev_group_ids"], "group")):
        if previous is not None and current is None:
            raise ValueError("calibration " + label + " IDs required for development isolation audit")
        if previous is not None and current is not None and set(previous) & set(current):
            raise ValueError("development/calibration " + label + " leakage")
    error_rows, ood_rows = _vector(errors, size, None, "errors"), _vector(ood, size, False, "ood")
    reasons = [_routing_reason(p, e, o) for p, e, o in zip(probs, error_rows, ood_rows)]
    units = groups if groups is not None else [str(i) for i in range(size)]
    missing_groups = groups is None and settings["require_group_ids"]
    alpha = (1 - settings["family_confidence"]) / 2
    trials, directions = [], {}
    for label in ("include", "exclude"):
        proposed = candidate["directions"][label]
        threshold = proposed["threshold"]
        if proposed["enabled_candidate"] and threshold not in settings["threshold_grid"]:
            raise ValueError("development threshold is outside frozen grid")
        indices = [i for i, p in enumerate(probs) if proposed["enabled_candidate"] and reasons[i] is None
                   and _winning_label(p) == label and p[label] >= threshold]
        grouped = defaultdict(list)
        for i in indices:
            grouped[units[i]].append(labels[i] == label)
        support, successes = len(grouped), sum(all(items) for items in grouped.values())
        bound = agreement_lower_bound(successes, support, alpha)
        enabled = (proposed["enabled_candidate"] and not missing_groups and support >= settings["min_support"]
                   and bound >= settings["minimum_agreement_lower_bound"])
        trial = {"direction": label, "threshold": threshold, "routed_pairs": len(indices),
                 "teacher_agree_pairs": sum(labels[i] == label for i in indices),
                 "teacher_review_pairs": sum(labels[i] == "review" for i in indices),
                 "support_units": support, "all_correct_units": successes,
                 "agreement_lower_bound": bound, "alpha": alpha, "eligible": enabled}
        trials.append(trial)
        directions[label] = {"enabled": enabled, "threshold": threshold if enabled else None,
                             "candidate_threshold": threshold, "routed_pairs": len(indices), "support_units": support,
                             "all_correct_units": successes, "agreement_lower_bound": bound}
    enabled = any(value["enabled"] for value in directions.values())
    policy = {"version": VERSION, "config": settings, "directions": directions, "trials": trials,
              "selection_method": "dev_frozen_calibration_confirm_only",
              "candidate_sha256": candidate["candidate_sha256"],
              "state": "selective_acceleration" if enabled else "disabled_all_teacher_no_acceleration",
              "calibration_pairs": size, "calibration_pair_ids": ids,
              "calibration_group_ids": sorted(set(groups)) if groups is not None else None,
              "dev_pair_ids": candidate["dev_pair_ids"], "dev_group_ids": candidate["dev_group_ids"],
              "group_ids_missing": missing_groups,
              "bound_unit": "duplicate_article_group_all_correct" if groups is not None else "pair_independence_assumed",
              "bound_is_factual_accuracy": False, "reference_kind": "frozen_teacher_decision_not_human_gold",
              "multiplicity": 2, "per_trial_alpha": alpha,
              "forced_fallback_counts": dict(sorted(Counter(r for r in reasons if r).items())),
              "score_semantics": "class_score_not_probability_of_correctness",
              "assumptions": ["one frozen candidate per direction before CAL", "independent exchangeable groups",
                              "article-group-balanced pilot; not full pair-weighted population certification",
                              "teacher agreement is not factual correctness; shifted inputs require fallback"]}
    policy["policy_sha256"] = _fingerprint(policy)
    return policy


fit_thresholds = tune_policy
validate_fixed_policy = confirm_policy


def _validate_policy(policy):
    if not isinstance(policy, dict) or policy.get("version") != VERSION:
        raise ValueError("invalid routing policy version")
    serialized = dict(policy)
    digest = serialized.pop("policy_sha256", None)
    if digest != _fingerprint(serialized):
        raise ValueError("routing policy fingerprint mismatch")
    settings = config_with_defaults(policy["config"])
    for label in ("include", "exclude"):
        rule = policy["directions"][label]
        if type(rule["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        if rule["enabled"] and (rule["threshold"] not in settings["threshold_grid"] or
                                rule["support_units"] < settings["min_support"] or
                                rule["agreement_lower_bound"] < settings["minimum_agreement_lower_bound"]):
            raise ValueError("enabled rule lacks required calibration support")
    return settings


def route(probs, policy, errors=None, ood=False, *, require_grounded_selection=False):
    """Return a student decision or a teacher request; never fabricate evidence."""
    _validate_policy(policy)
    if type(require_grounded_selection) is not bool:
        raise ValueError("require_grounded_selection must be boolean")
    reason = "grounded_selection_requested" if require_grounded_selection else _routing_reason(probs, errors, ood)
    if reason is None:
        winner = _winning_label(probs)
        rule = policy["directions"][winner]
        if rule["enabled"] and probs[winner] >= rule["threshold"]:
            return {"source": "student", "decision": winner, "needs_teacher": False,
                    "reason": "calibrated_auto_zone", "policy_sha256": policy["policy_sha256"],
                    "grounded_evidence_available": False, "relation": None, "evidence": [],
                    "evidence_status": "not_generated", "graph_materialization_ready": False}
        reason = "direction_disabled" if not rule["enabled"] else "outside_auto_zone"
    return {"source": "teacher_fallback", "decision": None, "needs_teacher": True,
            "reason": reason, "policy_sha256": policy["policy_sha256"],
            "grounded_evidence_available": False, "relation": None, "evidence": [],
            "evidence_status": "not_generated", "graph_materialization_ready": False}


def _reference_errors(decisions, references):
    return {"wrong_accept": sum(p == "include" and r == "exclude" for p, r in zip(decisions, references)),
            "relevant_miss_exclude": sum(p == "exclude" and r == "include" for p, r in zip(decisions, references)),
            "relevant_defer_review": sum(p == "review" and r == "include" for p, r in zip(decisions, references)),
            "reference_review_auto_decision": sum(p != "review" and r == "review" for p, r in zip(decisions, references)),
            "decision_agreement": sum(p == r for p, r in zip(decisions, references))}


def evaluate_cascade(test_labels, test_probs, policy, *, test_ids=None, test_group_ids=None,
                     external_reference=None, reference_kind=None, errors=None, ood=None, timings=None):
    """Paired final-test comparison; threshold policy is read-only and unchanged.

    test_labels are validated all-teacher outcomes. Fallback reuses those SAME
    outcomes to isolate routing changes, not a fresh stochastic teacher run.
    external_reference may be explicitly marked ``ai_reference_not_human_gold``
    or ``independent_human_reference``. Never call teacher agreement accuracy.
    IDs/group IDs are required when recorded at calibration so test leakage
    cannot be hidden by simply omitting the corresponding identity arrays.
    """
    settings = _validate_policy(policy)
    labels, probs = _validate_labels(test_labels, "test_labels"), list(test_probs)
    if len(labels) != len(probs):
        raise ValueError("test labels and scores differ in length")
    size = len(labels)
    ids = _identities(test_ids, size, "test_ids", unique=True)
    groups = _identities(test_group_ids, size, "test_group_ids")
    if groups is not None and settings["one_pair_per_group"] and len(set(groups)) != size:
        raise ValueError("one pair per independent test group is required")
    for current, calibrated, name in ((ids, policy["calibration_pair_ids"], "pair"),
                                      (groups, policy["calibration_group_ids"], "group")):
        if calibrated is not None and current is None:
            raise ValueError("test " + name + " IDs are required for isolation audit")
        if current is not None and calibrated is not None and set(current) & set(calibrated):
            raise ValueError("test/calibration " + name + " leakage")
    for current, previous, name in ((ids, policy.get("dev_pair_ids"), "pair"), (groups, policy.get("dev_group_ids"), "group")):
        if previous is not None and current is None:
            raise ValueError("test " + name + " IDs required for development isolation audit")
        if previous is not None and current is not None and set(previous) & set(current):
            raise ValueError("test/development " + name + " leakage")
    error_rows, ood_rows = _vector(errors, size, None, "errors"), _vector(ood, size, False, "ood")
    routes = [route(p, policy, e, o) for p, e, o in zip(probs, error_rows, ood_rows)]
    decisions = [teacher if item["needs_teacher"] else item["decision"] for teacher, item in zip(labels, routes)]
    automatic = sum(not item["needs_teacher"] for item in routes)
    false_include = sum(p == "include" and teacher == "exclude" for p, teacher in zip(decisions, labels))
    false_exclude = sum(p == "exclude" and teacher == "include" for p, teacher in zip(decisions, labels))
    review_to_include = sum(p == "include" and teacher == "review" for p, teacher in zip(decisions, labels))
    review_to_exclude = sum(p == "exclude" and teacher == "review" for p, teacher in zip(decisions, labels))
    agreement = sum(p == teacher for p, teacher in zip(decisions, labels))
    report = {"evaluation_split": "test", "policy_sha256": policy["policy_sha256"],
              "total_pairs": size, "auto_pairs": automatic, "fallback_pairs": size - automatic,
              "auto_coverage": automatic / size if size else None,
              "fallback_fraction": (size - automatic) / size if size else None,
              "teacher_agreement_pairs": agreement, "teacher_agreement": agreement / size if size else None,
              "auto_teacher_agreement": (automatic - (size - agreement)) / automatic if automatic else None,
              "false_include_vs_teacher_exclude": false_include,
              "false_exclude_vs_teacher_include": false_exclude,
              "teacher_review_to_student_include": review_to_include,
              "teacher_review_to_student_exclude": review_to_exclude,
              "total_disagreements": size - agreement,
              "disagreement_categories_disjoint": True,
              "teacher_decision_counts": dict(Counter(labels)), "cascade_decision_counts": dict(Counter(decisions)),
              "fallback_reason_counts": dict(Counter(r["reason"] for r in routes if r["needs_teacher"])),
              "state": "no_acceleration_on_this_test" if automatic == 0 else "selective_test_routing",
              "is_accuracy": False, "thresholds_changed_using_test": False,
              "graph_materialization_ready": False, "evidence_or_relation_parity_measured": False,
              "fallback_uses_same_frozen_teacher_outcome": True,
              "rows": [{"pair_id": ids[i] if ids else None, "teacher_decision": labels[i],
                        "cascade_decision": decisions[i], "route": routes[i]} for i in range(size)]}
    assert size - agreement == false_include + false_exclude + review_to_include + review_to_exclude
    bounds_valid = bool(size and groups is not None and len(set(groups)) == size)
    upper = 1 - agreement_lower_bound(agreement, size, 0.05) if bounds_valid else None
    report["teacher_disagreement_upper_bound_95"] = upper
    report["test_bound_unit"] = "one_pair_per_duplicate_article_group" if bounds_valid else "unavailable_without_unique_groups"
    report["conditional_routes"] = {}
    for label in ("include", "exclude"):
        indices = [i for i, r in enumerate(routes) if not r["needs_teacher"] and r["decision"] == label]
        matching = sum(labels[i] == label for i in indices)
        report["conditional_routes"][label] = {"auto_pairs": len(indices), "teacher_disagreements": len(indices) - matching,
            "teacher_disagreement_fraction": (len(indices) - matching) / len(indices) if indices else None,
            "teacher_disagreement_upper_bound_95": 1 - agreement_lower_bound(matching, len(indices), .05) if indices and bounds_valid else None}
    speed_pass = None
    if timings is not None:
        expected = {"student_total_seconds", "teacher_all_total_seconds", "fallback_teacher_total_seconds"}
        if not isinstance(timings, dict) or set(timings) != expected or any(not _finite_number(v) or v < 0 for v in timings.values()):
            raise ValueError("timings require finite nonnegative student/all-teacher/fallback totals")
        cascade_seconds = timings["student_total_seconds"] + timings["fallback_teacher_total_seconds"]
        if cascade_seconds <= 0 or timings["teacher_all_total_seconds"] <= 0:
            raise ValueError("timings must describe a positive, consistent same-workload comparison")
        speed_pass = cascade_seconds < timings["teacher_all_total_seconds"]
        report["timing_comparison"] = {**timings, "cascade_total_seconds": cascade_seconds,
                                      "measured_speedup": timings["teacher_all_total_seconds"] / cascade_seconds,
                                      "scope": "decision routing only; excludes unmeasured downstream evidence extraction"}
    parity_pass = upper is not None and upper <= settings["test_max_disagreement_upper_bound"]
    coverage_pass = size > 0 and automatic / size >= settings["test_min_auto_coverage"]
    report["pilot_gates"] = {"decision_parity_pass": parity_pass, "coverage_pass": coverage_pass,
                            "measured_speed_pass": speed_pass, "all_pass": parity_pass and coverage_pass and speed_pass is True,
                            "factual_accuracy_or_full_graph_parity_claim": False}
    if external_reference is not None:
        if reference_kind not in {"ai_reference_not_human_gold", "independent_human_reference"}:
            raise ValueError("external references require an explicit supported reference_kind")
        references = _validate_labels(external_reference, "external_reference")
        if len(references) != size:
            raise ValueError("external reference length differs from test rows")
        teacher_metrics = _reference_errors(labels, references)
        cascade_metrics = _reference_errors(decisions, references)
        report["external_reference_comparison"] = {
            "reference_kind": reference_kind, "total_pairs": size,
            "teacher_all": teacher_metrics, "cascade": cascade_metrics,
            "cascade_minus_teacher": {key: cascade_metrics[key] - teacher_metrics[key] for key in teacher_metrics},
            "is_independent_factual_accuracy_estimate": reference_kind == "independent_human_reference"}
    elif reference_kind is not None:
        raise ValueError("reference_kind without external_reference")
    return report
