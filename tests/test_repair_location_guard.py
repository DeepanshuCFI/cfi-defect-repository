"""The public-pin repair: a plan that is reviewed, then applied exactly (16 Nov 2026)."""
import inspect
import json

from scripts import repair_location_guard as rep

PIN = {"lat": 27.40, "lon": 80.10}


def _after(lat=None, lon=None, precision=None, match=None, conf=None):
    return {"lat": lat, "lon": lon, "geocode_precision": precision, "geocode_match": match,
            "geocode_confidence": conf, "geocode_method": None, "geocode_qualifier": None}


def test_outcomes():
    assert rep.outcome(PIN, _after(27.401, 80.101, "road")) == "keep"          # ~150 m
    assert rep.outcome(PIN, _after(27.60, 80.40, "locality")) == "move"        # ~37 km
    assert rep.outcome(PIN, _after()) == "unpin"
    assert rep.outcome(PIN, _after(precision="highway", match="NH 31")) == "highway"
    assert rep.outcome({"lat": None, "lon": None}, _after()) == "none"
    assert rep.outcome({"lat": None, "lon": None}, _after(27.6, 80.4, "town")) == "move"


def test_summary_counts_what_the_owner_is_asked_to_approve(tmp_path):
    recs = [{"outcome": "keep", "moved_km": 0.1, "after": _after(27.4, 80.1, "road", conf=0.8)},
            {"outcome": "move", "moved_km": 37.0, "after": _after(27.6, 80.4, "town", conf=0.65)},
            {"outcome": "move", "moved_km": 4.0, "after": _after(27.4, 80.14, "locality", conf=0.55)},
            {"outcome": "unpin", "moved_km": None, "after": _after()},
            {"outcome": "highway", "moved_km": None, "after": _after(precision="highway")}]
    s = rep.summarize(recs, tmp_path)
    assert s["outcome"] == {"keep": 1, "move": 2, "unpin": 1, "highway": 1, "none": 0}
    assert s["precision_after"]["town"] == 1 and s["precision_after"]["highway"] == 1
    assert s["moved_over_10km"] == 1 and s["below_publish_bar_after"] == 1
    assert json.loads((tmp_path / "summary.json").read_text())["reports"] == 5


def test_the_plan_never_touches_the_pipelines_own_cache_or_the_database():
    src = inspect.getsource(rep.plan)
    assert 'g.CACHE_PATH = out / "geocode_cache.json"' in src, "dry run must use a throwaway cache"
    assert "_connect(read_only=True)" in src
    assert "update " not in src.lower() and "insert " not in src.lower()


def test_apply_is_guarded_audited_and_unlinks_what_it_unpins():
    src = " ".join(inspect.getsource(rep.apply).split())
    assert "for update" in src, "row must be locked while its current pin is compared"
    assert "skipped_pin_changed" in src, "a pin changed since the plan must not be overwritten"
    assert "insert into review_action" in src and "before_json, after_json" in src
    assert "geom = null" in src and "cluster_id = null" in src
    assert rep.REVIEWER == "pipeline:location_guard"
    # scope is the public layer only
    assert "from public_incident p" in " ".join(rep.SELECT.split())


# ------------------------------------------- period totals found by reading (item 0.4)
def test_hand_read_period_totals_are_zeroed_even_below_the_pattern_threshold():
    from scripts import repair_aggregate_casualties as agg
    rows = [(14000, 5, 0, "alleging 5 deaths in crashes on it over the past 10 years", True),
            (15957, 5, 0, "a pickup crashed near Kantaur village, killing 5", True),
            (21141, 560, 0, "Jaipur recorded 560 road accident deaths in 10 months, statistics show", True)]
    # the pattern alone takes only the large roundup: under 10 casualties it is unsafe
    assert [r[0] for r in agg.select_hits(rows, set(), set())] == [21141]
    # a hand-read id is added; the real single crash is left alone
    assert [r[0] for r in agg.select_hits(rows, set(), {14000})] == [14000, 21141]
    # --skip wins over --ids
    assert [r[0] for r in agg.select_hits(rows, {14000}, {14000})] == [21141]
    assert agg._id_list(["x", "--ids", "13207,14000"], "--ids") == {13207, 14000}
