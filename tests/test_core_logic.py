"""Unit tests for the pure decision logic (CLAUDE.md: dedup, scoring, gate).
Clustering (ST_ClusterDBSCAN) is SQL-side — verified live via `pipeline.run recompute`.

Run: python3 -m pytest tests/ -q
"""
import math
from datetime import date

from pipeline.processing.dedup import is_same_crash
from pipeline.processing.gate import publishable
from pipeline.processing.score import components, tier, total_score

GATE = {"extraction_confidence_min": 0.7, "geocode_confidence_min": 0.6,
        "require_infra_implicated": True}
CFG = {"casualty_weights": {"fatality": 5, "grievous_injury": 2, "minor_injury": 1},
       "recency_half_life_days": 90}
WEIGHTS = {"w1_fatalities_weighted": 0.30, "w2_crash_frequency": 0.25, "w3_recency": 0.15,
           "w4_vulnerable_user_share": 0.15, "w5_defect_severity": 0.10,
           "w6_evidence_strength": 0.05}
TIERS = {"critical": 75, "high": 50, "medium": 25, "watch": 0}


# ---------------------------------------------------------------- dedup
def _inc(d, f=1, i=0):
    return {"crash_date": d, "fatalities": f, "injuries": i}


def test_same_crash_merges():
    assert is_same_crash(_inc(date(2026, 7, 1)), _inc(date(2026, 7, 2)), dist_m=200)


def test_different_date_never_merges():
    assert not is_same_crash(_inc(date(2026, 7, 1)), _inc(date(2026, 7, 4)), dist_m=50)


def test_far_apart_never_merges():
    assert not is_same_crash(_inc(date(2026, 7, 1)), _inc(date(2026, 7, 1)), dist_m=5000)


def test_casualty_mismatch_never_merges():
    a, b = _inc(date(2026, 7, 1), f=1), _inc(date(2026, 7, 1), f=4)
    assert not is_same_crash(a, b, dist_m=100)


def test_undated_never_merges():
    assert not is_same_crash(_inc(None), _inc(date(2026, 7, 1)), dist_m=10)


# ---------------------------------------------------------------- scoring
RAW0 = {"fatalities": 0, "injuries": 0, "inc_6mo": 0, "days_since_last": None,
        "vulnerable_share": 0.0, "max_severity": 0.0, "n_sources": 0,
        "avg_geocode_conf": 0.0}


def test_zero_hotspot_scores_zero():
    assert total_score(components(RAW0, CFG), WEIGHTS) == 0.0


def test_score_monotonic_in_fatalities():
    lo = components({**RAW0, "fatalities": 1}, CFG)
    hi = components({**RAW0, "fatalities": 5}, CFG)
    assert total_score(hi, WEIGHTS) > total_score(lo, WEIGHTS)


def test_components_bounded_0_1():
    raw = {"fatalities": 100, "injuries": 500, "inc_6mo": 50, "days_since_last": 0,
           "vulnerable_share": 1.0, "max_severity": 5.0, "n_sources": 20,
           "avg_geocode_conf": 1.0}
    comp = components(raw, CFG)
    assert all(0.0 <= v <= 1.0 for v in comp.values())
    assert total_score(comp, WEIGHTS) <= 100.0


def test_recency_decay_half_life():
    c = components({**RAW0, "days_since_last": 90}, CFG)
    assert math.isclose(c["recency"], math.exp(-1), rel_tol=1e-6)


def test_repeat_crash_hotspot_outranks_single():
    single = {**RAW0, "fatalities": 2, "inc_6mo": 1, "days_since_last": 10,
              "n_sources": 1, "avg_geocode_conf": 0.8}
    repeat = {**RAW0, "fatalities": 2, "inc_6mo": 4, "days_since_last": 10,
              "n_sources": 4, "avg_geocode_conf": 0.8}
    assert (total_score(components(repeat, CFG), WEIGHTS)
            > total_score(components(single, CFG), WEIGHTS))


def test_tiers():
    assert tier(80, TIERS) == "critical"
    assert tier(60, TIERS) == "high"
    assert tier(30, TIERS) == "medium"
    assert tier(5, TIERS) == "watch"


# ---------------------------------------------------------------- confidence gate
def test_gate_passes_good_entry():
    ok, why = publishable(0.85, 0.8, True, ["potholes_or_broken_surface"], GATE)
    assert ok and why == "gate_passed"


def test_gate_blocks_low_extraction():
    ok, why = publishable(0.5, 0.9, True, ["potholes_or_broken_surface"], GATE)
    assert not ok and why == "extraction_confidence_below_min"


def test_gate_blocks_centroid_geocode():
    ok, why = publishable(0.9, 0.4, True, ["potholes_or_broken_surface"], GATE)
    assert not ok and why == "geocode_confidence_below_min"


def test_gate_blocks_no_infra():
    ok, why = publishable(0.9, 0.9, False, [], GATE)
    assert not ok and why == "infra_not_implicated"


def test_gate_blocks_other_infrastructure_only():
    ok, why = publishable(0.9, 0.9, True, ["other_infrastructure"], GATE)
    assert not ok and why == "other_infrastructure_needs_review"


def test_reviewer_override_wins():
    ok, why = publishable(0.1, 0.1, False, [], GATE, reviewed_approved=True)
    assert ok and why == "reviewer_approved"


# ---------------------------------------------------------------- auto-review policy
from pipeline.processing.auto_review import decide  # noqa: E402


def test_autoreview_publish_needs_conf_and_geocode():
    v = {"verdict": "confirm_publish", "confidence": 0.9}
    assert decide(v, 0.7) == "auto_published"
    assert decide(v, 0.4) is None                    # geocode gate never overridden
    assert decide({"verdict": "confirm_publish", "confidence": 0.6}, 0.9) is None


def test_autoreview_reject_threshold():
    assert decide({"verdict": "confirm_reject", "confidence": 0.9}, None) == "rejected"
    assert decide({"verdict": "confirm_reject", "confidence": 0.7}, None) is None


def test_autoreview_crash_only():
    assert decide({"verdict": "crash_only_ok", "confidence": 0.8}, 0.4) == "machine_ok"
    assert decide({"verdict": "crash_only_ok", "confidence": 0.5}, 0.9) is None


def test_autoreview_needs_human_never_acts():
    assert decide({"verdict": "needs_human", "confidence": 0.99}, 0.9) is None
