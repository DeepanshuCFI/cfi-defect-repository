"""Unit tests for the pure decision logic (CLAUDE.md: dedup, scoring, gate).
Clustering (ST_ClusterDBSCAN) is SQL-side — verified live via `pipeline.run recompute`.

Run: python3 -m pytest tests/ -q
"""
import math
from datetime import date

from pipeline.processing.dedup import is_same_crash
from pipeline.processing.gate import publishable
from pipeline.processing.score import components, tier, total_score
from pipeline.fetch import is_content_duplicate
from pipeline.store import coerce_incident

GATE = {"extraction_confidence_min": 0.7, "geocode_confidence_min": 0.6,
        "require_infra_implicated": True}
CFG = {"casualty_weights": {"fatality": 5, "grievous_injury": 2, "minor_injury": 1},
       "recency_half_life_days": 90}
WEIGHTS = {"w1_fatalities_weighted": 0.30, "w2_crash_frequency": 0.25, "w3_recency": 0.15,
           "w4_vulnerable_user_share": 0.15, "w5_defect_severity": 0.10,
           "w6_evidence_strength": 0.05}
TIERS = {"critical": 75, "high": 50, "medium": 25, "watch": 0}


# ---------------------------------------------------------------- budget split
from pipeline.llmcost import extraction_share  # noqa: E402


def test_extraction_share_scales_with_adjudication_demand():
    b = 1.95
    # light day (measured 19-20 Jul: ~20 pending) -> extraction gets most of the budget
    light = extraction_share(20 + 30, b)
    # heavy backlog -> reserve grows, but never past the cap
    heavy = extraction_share(500, b)
    assert light > heavy
    assert 0.5 <= light <= 0.85
    assert math.isclose(heavy, 0.5)          # cap: adjudication never takes >50%


def test_extraction_share_floor_protects_adjudication():
    # zero pending must still reserve the floor — new incidents this run need review
    assert math.isclose(extraction_share(0, 1.95), 0.85)


def test_extraction_share_survives_zero_budget():
    assert extraction_share(10, 0) == 0.5


# ---------------------------------------------------------------- geocode vocabulary
# DB CHECK constraint pins geocode_method to this list (migrations/001, 012).
ALLOWED_GEOCODE_METHODS = {"coords_in_text", "landmark_district", "road_city",
                           "city_centroid", "district_centroid", "manual"}


def test_geocode_method_always_in_controlled_vocabulary(monkeypatch):
    """Regression: suffixed methods ('landmark_district_unanchored') violated the DB
    CHECK constraint and would have failed every geocode UPDATE."""
    from pipeline.processing import geocode as gc
    for state, span in (("", 0.5), ("Odisha", 0.5), ("Uttar Pradesh", 47.0)):
        monkeypatch.setattr(gc, "_resolve", lambda q, s=state, sp=span: {
            "lat": 27.5, "lon": 78.05, "state": s, "district": "Hathras",
            "display": "x", "span_km": sp})
        for kwargs in ({}, {"admin_state": "Uttar Pradesh", "admin_district": "Hathras"}):
            g = gc.geocode("somewhere", **kwargs)
            if g["geocode_method"] is not None:
                assert g["geocode_method"] in ALLOWED_GEOCODE_METHODS, g["geocode_method"]
            assert g["geocode_qualifier"] in (None, "unanchored", "stateless_hit",
                                              "districtless_hit", "wide_area")


# ---------------------------------------------------------------- geocode anchoring
def test_unanchored_geocode_capped_below_publish_bar(monkeypatch):
    """#276 (2026-07-18): 'near Bisawar village, Vidhipur road,' with NO state geocoded
    to Odisha at 0.80 and published — a Hathras (UP) crash. With no state/district the
    state guard cannot run, so confidence must be capped below the 0.6 gate."""
    from pipeline.processing import geocode as gc
    monkeypatch.setattr(gc, "_resolve", lambda q: {
        "lat": 20.69553, "lon": 86.162129, "state": "Odisha", "display": "Bisawar, Odisha"})
    g = gc.geocode("near Bisawar village, Vidhipur road, ")
    assert g["lat"] is not None
    assert g["geocode_confidence"] <= gc.UNANCHORED_MAX_CONF < 0.6   # -> review, not public
    assert g["geocode_qualifier"] == "unanchored"


def test_anchored_geocode_keeps_full_confidence(monkeypatch):
    from pipeline.processing import geocode as gc
    monkeypatch.setattr(gc, "_resolve", lambda q: {
        "lat": 27.5, "lon": 78.05, "state": "Uttar Pradesh", "district": "Hathras",
        "display": "Bisawar, Hathras, UP"})
    g = gc.geocode("near Bisawar village, Vidhipur road", admin_district="Hathras",
                   admin_state="Uttar Pradesh")
    assert g["geocode_confidence"] >= 0.6
    assert g["geocode_qualifier"] is None


def test_state_guard_still_rejects_wrong_state_when_anchored(monkeypatch):
    from pipeline.processing import geocode as gc
    monkeypatch.setattr(gc, "_resolve", lambda q: {
        "lat": 20.69553, "lon": 86.162129, "state": "Odisha", "display": "Bisawar, Odisha"})
    g = gc.geocode("near Bisawar village", admin_state="Uttar Pradesh")
    assert g["lat"] is None      # every ladder rung rejected — no wrong-state pin


def test_stateless_hit_capped(monkeypatch):
    # anchored query, but the HIT carries no state metadata: guard ran on nothing
    from pipeline.processing import geocode as gc
    monkeypatch.setattr(gc, "_resolve", lambda q: {
        "lat": 27.5, "lon": 78.05, "state": "", "display": "somewhere", "span_km": 0.5})
    g = gc.geocode("near Bisawar village", admin_state="Uttar Pradesh")
    assert g["geocode_confidence"] <= gc.STATELESS_HIT_MAX_CONF < 0.6
    assert g["geocode_qualifier"] == "stateless_hit"


def test_wide_area_hit_capped(monkeypatch):
    # 'Outer Ring Road, Delhi' family: a 47km way must not pin at 0.7
    from pipeline.processing import geocode as gc
    monkeypatch.setattr(gc, "_resolve", lambda q: {
        "lat": 28.55, "lon": 77.19, "state": "Delhi", "district": "South Delhi",
        "display": "Outer Ring Road", "span_km": 47.0})
    g = gc.geocode("Outer Ring Road", admin_state="Delhi", admin_district="South Delhi")
    assert g["geocode_confidence"] <= gc.WIDE_AREA_MAX_CONF < 0.6
    assert g["geocode_qualifier"] == "wide_area"


def test_dedup_without_locality_never_matches():
    # near_duplicate must fail toward KEEPING the article when unscoped (JsonlStore
    # mirrors the DBStore guard: no district & no state -> global compare forbidden)
    from pipeline.store import JsonlStore
    s = JsonlStore.__new__(JsonlStore)
    s._rows = [{"dedup_hash": "6d20e41c77ea5e12", "district": "Pilibhit", "state": "UP"}]
    assert not s.near_duplicate("6d20e41c77ea5e12", district=None, state=None)


# ---------------------------------------------------------------- site watch
from pipeline.processing.watch import name_matches  # noqa: E402


def test_watch_variant_plus_locality_matches():
    t = "तेजाजी नगर चौराहे पर तेज रफ्तार ट्रक ने बाइक सवार को टक्कर मारी, Indore में हादसा"
    assert name_matches(t, None, ["तेजाजी नगर", "Tejaji Nagar"], "Indore", "Indore")


def test_watch_generic_name_needs_locality():
    # 'Medical College Junction' exists in every city — must NOT match without
    # city/district corroboration
    t = "accident at medical college junction leaves two injured"
    assert not name_matches(t, None, ["Medical College Junction"], "Kozhikode", "Kozhikode")
    assert name_matches(t, "Kozhikode", ["Medical College Junction"], "Kozhikode", "Kozhikode")


def test_watch_no_variant_no_match():
    assert not name_matches("a crash on some road in Indore", None,
                            ["Tejaji Nagar"], "Indore", "Indore")


# ---------------------------------------------------------------- incident coercion
def test_coerce_stringly_null_casualties():
    # daily run #9 (2026-07-10) died on injuries="null" (string) hitting an int column
    inc = coerce_incident({"fatalities": "null", "injuries": "None",
                           "extraction_confidence": "null"})
    assert inc["fatalities"] == 0 and inc["injuries"] == 0
    assert inc["extraction_confidence"] == 0.0   # fails gate -> review queue, honest


def test_coerce_numeric_strings_and_real_numbers_survive():
    inc = coerce_incident({"fatalities": "3", "injuries": 2.0,
                           "extraction_confidence": "0.85"})
    assert inc["fatalities"] == 3 and inc["injuries"] == 2
    assert math.isclose(inc["extraction_confidence"], 0.85)


def test_coerce_nullish_text_fields_become_none():
    inc = coerce_incident({"road_name": "null", "admin_district": " N/A ",
                           "fatalities": 1, "injuries": 0})
    assert inc["road_name"] is None and inc["admin_district"] is None


# ---------------------------------------------------------------- content dedup (ingest)
# Real simhashes from production (2026-07-09): distinct-location defect stories that the
# old global Hamming<=6 rule wrongly collapsed. Marauri street-light (Pilibhit) sits at
# Hamming 6 from a Chittorgarh hospital story and 5 from Kota/Pratapgarh crashes.
H_MARAURI = "6d20e41c77ea5e12"      # Pilibhit street lights out on NH
H_CHITTORGARH = "6d20e49d27ea5f1a"  # unrelated hospital story, Hamming 6 from Marauri
H_KOTA = "6f20e49d67ea5f12"         # NH-27 crash, Hamming 5 from Marauri


def test_content_dup_exact_rereport():
    # genuine re-report (Hamming 0) is still a duplicate
    assert is_content_duplicate(H_MARAURI, [H_MARAURI], hamming_max=3)


def test_content_dup_tight_threshold_rejects_distinct_stories():
    # the production false-positive cluster: distinct stories at Hamming 5-6 must NOT
    # dedupe at the new threshold of 3 (old threshold of 6 wrongly collapsed them)
    assert not is_content_duplicate(H_MARAURI, [H_KOTA, H_CHITTORGARH], hamming_max=3)
    assert is_content_duplicate(H_MARAURI, [H_KOTA, H_CHITTORGARH], hamming_max=6)  # old bug


def test_content_dup_empty_hash_never_matches():
    assert not is_content_duplicate("", [H_MARAURI], hamming_max=3)
    assert not is_content_duplicate(H_MARAURI, [""], hamming_max=3)


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


# ------------------------------------------------- process-queue priority (31 Jul 2026)
# The defect-first ordering must rank the WHOLE queue, not the slice already loaded.
# Regression: cmd_process sorted rows AFTER `order by id limit 1000` fetched them, so on
# 31 Jul the window held 21 of 601 defect-vocabulary articles and the other 580 were
# invisible to the budget — it spent the day on the oldest behaviour-crash tail instead.
from pipeline.store import JsonlStore  # noqa: E402

DEFECT = ["pothole", "गड्ढा"]


def _queue(*texts):
    """A JsonlStore over synthetic rows, no filesystem touched."""
    s = object.__new__(JsonlStore)
    s._rows = [{"id": i, "processing_status": "fetched", "clean_text": t}
               for i, t in enumerate(texts)]
    return s


def test_priority_reaches_past_the_window():
    # 3 filler rows then a defect row, window of 3: the defect row must still surface
    q = _queue("bike skidded", "lorry overturned", "driver dozed off", "deep pothole here")
    got = q.articles_by_status("fetched", limit=3, priority_terms=DEFECT)
    assert [a["id"] for a in got][0] == 3, "defect article outside the window was starved"


def test_priority_is_fifo_within_each_group():
    q = _queue("गड्ढा one", "speeding", "pothole two", "overtaking", "गड्ढा three")
    got = [a["id"] for a in q.articles_by_status("fetched", limit=5, priority_terms=DEFECT)]
    assert got == [0, 2, 4, 1, 3]          # defect block id-ascending, then the tail


def test_priority_terms_absent_keeps_plain_order():
    q = _queue("pothole", "speeding", "गड्ढा")
    assert [a["id"] for a in q.articles_by_status("fetched", limit=2)] == [0, 1]


def test_priority_tolerates_missing_clean_text():
    s = object.__new__(JsonlStore)
    s._rows = [{"id": 0, "processing_status": "fetched", "clean_text": None},
               {"id": 1, "processing_status": "fetched", "clean_text": "pothole"}]
    got = [a["id"] for a in s.articles_by_status("fetched", limit=2, priority_terms=DEFECT)]
    assert got == [1, 0]


# ------------------------------------------------ Google News resolver (31 Jul 2026)
# Google throttles the batchexecute decoder partway through a large run; the failure
# used to be swallowed whole, so >half of each day's collection was stored unresolved
# and never fetched or retried. Guards: repeats must never re-hit the network, a
# throttled failure must never be persisted as if it were an answer, and the breaker
# must stop the run from hammering a service already refusing it.
import sys  # noqa: E402
import types  # noqa: E402

import pytest  # noqa: E402

from pipeline.collectors import rss  # noqa: E402

GURL = "https://news.google.com/rss/articles/{}"


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    """Isolated resolver: temp disk cache, stubbed decoder, zeroed counters."""
    monkeypatch.setattr(rss, "CACHE_PATH", tmp_path / "gnews.json")
    monkeypatch.setattr(rss, "_disk", None)
    rss.reset_run_state()
    calls: list[str] = []
    box = {"ok": True}

    def stub(url, interval=1):
        calls.append(url)
        if not box["ok"]:
            return {"status": False}
        return {"status": True, "decoded_url": "https://publisher.example/" + url[-4:]}

    monkeypatch.setitem(sys.modules, "googlenewsdecoder",
                        types.SimpleNamespace(gnewsdecoder=stub))
    yield rss, calls, box
    rss.reset_run_state()


def test_repeat_url_never_rehits_the_network(resolver):
    r, calls, _ = resolver
    u = GURL.format("aaaa")
    assert r.resolve_url(u)[1] is True
    r.save_cache()
    r._resolve_cache.clear()                  # simulate the next day's fresh process
    monkeypatched_disk = r._disk
    r._disk = None                            # force a reload from the temp cache file
    assert r.resolve_url(u) == (monkeypatched_disk[u], True)
    assert len(calls) == 1, "cached resolution still cost a Google round-trip"
    assert r.STATS["hit_disk"] == 1


def test_failures_are_never_persisted(resolver):
    r, _, box = resolver
    box["ok"] = False
    u = GURL.format("bbbb")
    assert r.resolve_url(u) == (u, False)
    r.save_cache()
    assert u not in r._disk, "a throttled miss was cached and would poison the entry"


def test_breaker_trips_and_then_stops_calling(resolver):
    r, calls, box = resolver
    box["ok"] = False
    for i in range(r.FAILURE_STREAK_TRIP):
        r.resolve_url(GURL.format(f"c{i:03d}"))
    assert r.throttled()
    before = len(calls)
    r.resolve_url(GURL.format("dddd"))
    assert len(calls) == before, "kept calling Google after the breaker tripped"
    assert r.STATS["skipped_throttled"] == 1


def test_success_resets_the_failure_streak(resolver):
    r, _, box = resolver
    box["ok"] = False
    for i in range(r.FAILURE_STREAK_TRIP - 1):
        r.resolve_url(GURL.format(f"e{i:03d}"))
    box["ok"] = True
    r.resolve_url(GURL.format("ffff"))
    box["ok"] = False
    for i in range(r.FAILURE_STREAK_TRIP - 1):
        r.resolve_url(GURL.format(f"g{i:03d}"))
    assert not r.throttled(), "streak should have restarted after the success"


def test_reset_breaker_gives_a_fresh_chance(resolver):
    r, _, box = resolver
    box["ok"] = False
    for i in range(r.FAILURE_STREAK_TRIP):
        r.resolve_url(GURL.format(f"h{i:03d}"))
    assert r.throttled()
    r.reset_breaker()
    assert not r.throttled()


def test_corrupt_cache_file_does_not_kill_the_run(resolver, tmp_path):
    r, _, _ = resolver
    (tmp_path / "gnews.json").write_text("{not json")
    r._disk = None
    assert r.resolve_url(GURL.format("iiii"))[1] is True


# ------------------------------------------------------- LLM backend adapter (1 Aug)
# The subscription backend has no dollar cap to stop against — it has an opaque rate
# limit that lands mid-run. The contract that makes a killed session cost nothing is:
# a usage refusal raises RateLimited, run._api_limit_hit() recognises it, the stage
# stops, and unfinished rows keep their processing_status so the next run resumes.
# A missed rate-limit match would instead look like a per-item failure and silently
# drop articles, so these guard the detection as much as the plumbing.
from pipeline import llm  # noqa: E402


def test_backend_defaults_to_api(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    assert llm.backend() == "api", "importing the adapter must not switch backends"


def test_cli_env_strips_api_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-propagate")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "should-not-propagate")
    env = llm._cli_env()
    assert "ANTHROPIC_API_KEY" not in env, "child would bill the API instead of the plan"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


@pytest.mark.parametrize("msg", [
    "Rate limit exceeded", "You have hit your usage limit", "429 Too Many Requests",
    "quota exhausted", "Limit reached — try again later",
])
def test_usage_refusals_are_rate_limited_not_item_failures(msg):
    assert llm._RATE_LIMIT.search(msg), f"{msg!r} would be mistaken for a bad article"


def test_ordinary_errors_are_not_mistaken_for_rate_limits():
    for msg in ["connection reset", "invalid model", "no such file"]:
        assert not llm._RATE_LIMIT.search(msg)


def test_rate_limit_stops_the_stage_and_resumes_next_run():
    """The whole resumability contract in one assertion."""
    from pipeline.run import _api_limit_hit
    assert _api_limit_hit(llm.RateLimited("usage limit")) is True
    assert _api_limit_hit(RuntimeError("connection reset")) is False


def test_json_extraction_tolerates_fences_and_preamble():
    assert llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._extract_json('Sure, here it is: {"a": 2} — hope that helps') == {"a": 2}


def test_json_extraction_refuses_rather_than_guesses():
    import pytest as _p
    for bad in ["", "no object here", "{unclosed", "[1,2,3]"]:
        with _p.raises(llm.BackendError):
            llm._extract_json(bad)


def test_cli_model_override_only_applies_to_cli(monkeypatch):
    monkeypatch.setenv("LLM_CLI_MODEL", "claude-opus-5")
    monkeypatch.setenv("LLM_BACKEND", "cli")
    assert llm.model_for("relevance") == "claude-opus-5"
    monkeypatch.setenv("LLM_BACKEND", "api")
    assert llm.model_for("relevance") != "claude-opus-5"   # config wins on the API path


# ------------------------------------------------ district anchoring (1 Aug 2026)
# The 18 Jul fix anchored geocodes to STATE and stopped a Hathras crash landing in
# Odisha. It could not stop a Gopalganj crash landing in Nawada — both are Bihar.
# Measured: 'NH-531, Gopalganj, Bihar' resolved to a road in NAWADA at 0.80, past the
# 0.6 gate; three such pins from three districts clustered into one phantom hotspot,
# and 34 of 35 escalation candidates were built that way. These cases are the real
# ones from that audit.
from pipeline.processing.geocode import _district_ok  # noqa: E402


def test_confident_district_mismatch_is_rejected():
    assert _district_ok("Nawada", "Gopalganj") is False        # hotspot #377
    assert _district_ok("Purnia", "Munger") is False           # sampled 1 Aug
    assert _district_ok("Gorakhpur", "Ambedkar Nagar") is False


def test_renamed_districts_are_not_false_rejections():
    """A naive equality check would reject correct pins for every renamed district —
    1 of 3 sampled mismatches was this case, not an error."""
    assert _district_ok("Aurangabad", "Chhatrapati Sambhajinagar") is True
    assert _district_ok("Chhatrapati Sambhajinagar", "Aurangabad") is True
    assert _district_ok("Allahabad", "Prayagraj") is True
    assert _district_ok("Faizabad", "Ayodhya") is True
    assert _district_ok("Gurgaon", "Gurugram") is True


def test_district_suffix_and_case_tolerated():
    assert _district_ok("Bareilly district", "Bareilly") is True
    assert _district_ok("BAREILLY", "bareilly") is True


def test_unknown_district_never_rejects_the_article():
    """Missing on either side is handled by the confidence cap in geocode(), not by
    dropping the record — the guard is for a CONFIDENT mismatch only."""
    assert _district_ok("", "Gopalganj") is True
    assert _district_ok("Nawada", None) is True
    assert _district_ok("", None) is True


def test_ladder_skips_a_wrong_district_hit(monkeypatch):
    """The whole bug, end to end: the first ladder variant matches the right state but
    the wrong district, and must be passed over rather than published."""
    from pipeline.processing import geocode as g
    calls = []

    def fake_resolve(query):
        calls.append(query)
        if len(calls) == 1:                       # most-specific variant: wrong district
            return {"lat": 24.9192, "lon": 85.5367, "state": "Bihar",
                    "district": "Nawada", "display": "Nh20 531, Nawada", "span_km": 0.5}
        return {"lat": 26.4641, "lon": 84.4400, "state": "Bihar",
                "district": "Gopalganj", "display": "Gopalganj, Bihar", "span_km": 0.5}

    monkeypatch.setattr(g, "_resolve", fake_resolve)
    out = g.geocode("NH-531, Gopalganj, Bihar", road_name="NH-531",
                    admin_district="Gopalganj", admin_state="Bihar")
    assert len(calls) > 1, "stopped at the wrong-district hit instead of descending"
    assert out["lat"] == 26.4641, "published the Nawada pin"


def test_districtless_hit_is_capped_below_the_publish_bar(monkeypatch):
    """A provider that returns no district leaves the guard unable to run. Unverified
    is not verified — cap it rather than trust it at 0.80."""
    from pipeline.processing import geocode as g
    monkeypatch.setattr(g, "_resolve", lambda q: {
        "lat": 25.0, "lon": 85.0, "state": "Bihar", "district": "",
        "display": "somewhere, Bihar", "span_km": 0.5})
    out = g.geocode("NH-531, Gopalganj, Bihar", road_name="NH-531",
                    admin_district="Gopalganj", admin_state="Bihar")
    assert out["geocode_confidence"] <= 0.6, "a districtless hit reached the publish bar"
    assert out["geocode_qualifier"] == "districtless_hit"


# ---------------------------------------------------- district repair: who stops voting
# The escalation flag counts every incident that CLUSTERS. cluster.py filters on
# geocode_confidence and nothing else, so machine_ok records — never public, kept on
# purpose so the >=3-in-6-months counter stays honest — vote too. The repair therefore
# has to reach records the publish gate never applied to, and the right remedy for those
# is a lower confidence, not a status change.
import importlib.util as _ilu  # noqa: E402
import pathlib as _pl  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "repair_district_pins",
    _pl.Path(__file__).resolve().parent.parent / "scripts" / "repair_district_pins.py")
_repair = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_repair)


def test_published_below_gate_demotes():
    assert _repair.plan(published=True, new_conf=0.55) == "demote"


def test_published_above_gate_keeps_its_pin():
    assert _repair.plan(published=True, new_conf=0.8) == "keep"


def test_nonpublic_at_same_confidence_keeps_voting():
    """0.55 is below the 0.6 publish gate but above the 0.5 cluster floor. Demoting a
    machine_ok record here would delete frequency evidence the escalation counter needs
    — the pin is good enough to cluster on, it was only ever unfit to publish."""
    assert _repair.plan(published=False, new_conf=0.55) == "keep"


def test_nonpublic_below_cluster_floor_stops_voting():
    assert _repair.plan(published=False, new_conf=0.4) == "stop_voting"


def test_no_valid_location_never_leaves_the_record_voting():
    """A re-geocode with no district-valid hit yields no confidence at all. Whatever the
    status, the one outcome that must not happen is the old pin quietly standing."""
    assert _repair.plan(published=True, new_conf=None) == "demote"
    assert _repair.plan(published=False, new_conf=None) == "stop_voting"
    assert _repair.plan(published=False, new_conf=0) == "stop_voting"


def test_cluster_floor_matches_the_clusterer():
    """If cluster.py's threshold moves and this constant does not, the repair starts
    leaving voters behind (or silencing records that still cluster) with no test failing.
    """
    from pipeline.processing.cluster import MIN_GEOCODE_CONF
    assert _repair.CLUSTER_MIN_CONF == MIN_GEOCODE_CONF


# ------------------------------------------------- resolver canary (the breaker's blind spot)
from pipeline.run import resolver_alarm  # noqa: E402

# Verbatim stage_stats.collect.resolver from the 1 Aug 2026 scheduled run (run 35).
RUN_35 = {"ok": 1159, "failed": 27, "hit_mem": 1265, "hit_disk": 0,
          "decoded_b64": 0, "skipped_throttled": 2618}


def test_real_run_that_lost_half_its_collection_raises_the_alarm():
    """The regression this canary exists for. The old test was ok/(ok+failed) = 98% and
    stayed silent while 2,618 of 5,069 items went unresolved."""
    msg = resolver_alarm(RUN_35)
    assert msg is not None, "a run that dropped 52% of its collection reported green"
    assert "2618" in msg and "52%" in msg


def test_breaker_cannot_silence_the_alarm():
    """`failed` is capped by the breaker at ~25. Losing MORE must never read as better:
    holding failures fixed and growing the skipped pile has to keep the alarm on."""
    worse = dict(RUN_35, skipped_throttled=5000)
    assert resolver_alarm(worse) is not None


def test_healthy_warm_cache_run_is_quiet():
    """What a warm URL cache should look like: most items served from disk, few calls,
    nothing skipped. This must not page anyone."""
    assert resolver_alarm({"ok": 300, "failed": 5, "hit_mem": 1200, "hit_disk": 3500,
                           "decoded_b64": 0, "skipped_throttled": 0}) is None


def test_disk_hits_count_as_resolved_not_as_losses():
    """A cache hit IS a resolution — if hit_disk were left out of the numerator the fix
    that makes the pipeline healthy would be the thing that trips the alarm."""
    assert resolver_alarm({"ok": 0, "failed": 0, "hit_mem": 0, "hit_disk": 5000,
                           "decoded_b64": 0, "skipped_throttled": 0}) is None


def test_tiny_or_missing_samples_never_fire():
    assert resolver_alarm({"ok": 1, "failed": 40, "skipped_throttled": 0}) is None
    assert resolver_alarm(None) is None
    assert resolver_alarm({}) is None


# --------------------------------------- geocode_qualifier vocabulary vs its constraint
def _constraint_vocabulary() -> set[str]:
    """The terms the newest migration allows in incident.geocode_qualifier."""
    import re
    migs = sorted((_pl.Path(__file__).resolve().parent.parent / "migrations")
                  .glob("[0-9]*.sql"))
    latest = [m for m in migs if "geocode_qualifier_check" in m.read_text()][-1]
    body = latest.read_text()
    clause = body[body.rindex("incident_geocode_qualifier_check"):]
    return set(re.findall(r"'([a-z_]+)'", clause[:clause.index(")")]))


def test_every_qualifier_the_geocoder_writes_is_allowed_by_the_constraint():
    """The 1 Aug regression. 5db87ee added 'districtless_hit' in code with no migration;
    the column's CHECK still listed three terms, so the first provider response missing a
    district would have aborted the geocode batch — and cmd_geocode commits after the
    loop with no try/except, so the whole batch, every run, until someone noticed."""
    from pipeline.processing.geocode import QUALIFIERS
    missing = QUALIFIERS - _constraint_vocabulary()
    assert not missing, f"geocode writes {missing} but no migration allows it"


def test_declared_qualifiers_match_the_branches_that_set_them():
    """QUALIFIERS is only useful if it is the real list. Read the literals actually
    assigned to `qualifier` in the module and hold the declaration against them."""
    import re
    from pipeline.processing import geocode as g
    assigned: set[str] = set()
    for line in _pl.Path(g.__file__).read_text().splitlines():
        # any line that assigns to `qualifier`, whatever shape the expression takes
        # ('x', or `qualifier or 'y'`, or a future one) — matching the expression itself
        # would just make this test brittle in the same way the code is.
        if re.search(r"\bqualifier\s*=", line) and "QUALIFIERS" not in line:
            assigned |= set(re.findall(r'"([a-z_]+)"', line))
    assert assigned == set(g.QUALIFIERS), (
        f"declared {set(g.QUALIFIERS)} but the code assigns {assigned}")


def test_repair_scripts_qualifier_is_allowed_too():
    """The repair writes its own term when no district-valid location exists at all."""
    assert "no_district_valid_location" in _constraint_vocabulary()


# ------------------------------------------- mapbox cache outliving the code that filled it
# _mapbox caches the NORMALIZED dict, so entries freeze in the shape the code had when
# written. 'district' arrived with 5db87ee AFTER that day's run, so every entry in the CI
# cache lacks it — and a missing district reads as districtless_hit, capping good pins at
# 0.55 and making the new guard look over-strict when the cache is what is stale.
PRE_FIX_ENTRY = {"lat": 26.46, "lon": 84.44, "state": "Bihar",
                 "display": "Gopalganj, Bihar", "span_km": 1.0}          # no 'district'
POST_FIX_ENTRY = dict(PRE_FIX_ENTRY, district="Gopalganj")
PROVIDER_SAID_NO_DISTRICT = dict(PRE_FIX_ENTRY, district="")


def test_pre_fix_cache_entry_is_treated_as_a_miss():
    from pipeline.processing import geocode as g
    assert not g._mb_cache_usable(PRE_FIX_ENTRY)


def test_post_fix_entry_is_served_from_cache():
    from pipeline.processing import geocode as g
    assert g._mb_cache_usable(POST_FIX_ENTRY)


def test_empty_district_is_a_real_answer_not_a_stale_one():
    """"district": "" means the provider genuinely returned none — the case
    districtless_hit exists for. Re-fetching it would misreport what Mapbox said."""
    from pipeline.processing import geocode as g
    assert g._mb_cache_usable(PROVIDER_SAID_NO_DISTRICT)


def test_cached_miss_stays_a_miss():
    """A cached None is a real 'Mapbox found nothing'. Invalidating it would re-buy the
    same negative every run."""
    from pipeline.processing import geocode as g
    assert g._mb_cache_usable(None)


def test_stale_entry_is_refetched_and_overwritten(monkeypatch):
    """End to end: a pre-fix entry must not be returned, and the fresh result replaces it
    so the cache heals in one run."""
    from pipeline.processing import geocode as g
    key = "mb|gopalganj, bihar"
    monkeypatch.setattr(g, "_cache", {key: dict(PRE_FIX_ENTRY)})
    monkeypatch.setattr(g, "_save_cache", lambda: None)
    monkeypatch.setattr(g, "MAPBOX_TOKEN", "test-token")

    class R:
        status_code = 200
        def json(self):
            return {"features": [{"geometry": {"coordinates": [84.44, 26.46]},
                                  "properties": {"context": {
                                      "region": {"name": "Bihar"},
                                      "district": {"name": "Gopalganj"}},
                                  "full_address": "Gopalganj, Bihar"}}]}

    monkeypatch.setattr(g.httpx, "get", lambda *a, **k: R())
    out = g._mapbox("Gopalganj, Bihar")
    assert out["district"] == "Gopalganj", "returned the stale entry instead of refetching"
    assert g._cache[key]["district"] == "Gopalganj", "stale entry was not overwritten"


def test_declared_fields_match_what_mapbox_actually_writes(monkeypatch):
    """If the normalizer gains a field and _MB_FIELDS does not, the staleness check stops
    covering it and this whole class of bug returns silently."""
    from pipeline.processing import geocode as g
    monkeypatch.setattr(g, "_cache", {})
    monkeypatch.setattr(g, "_save_cache", lambda: None)
    monkeypatch.setattr(g, "MAPBOX_TOKEN", "test-token")

    class R:
        status_code = 200
        def json(self):
            return {"features": [{"geometry": {"coordinates": [84.4, 26.4]},
                                  "bbox": [84.3, 26.3, 84.5, 26.5],
                                  "properties": {"context": {"region": {"name": "Bihar"}},
                                                 "full_address": "x"}}]}

    monkeypatch.setattr(g.httpx, "get", lambda *a, **k: R())
    written = g._mapbox("anywhere")
    assert set(written) == set(g._MB_FIELDS), (
        f"_mapbox writes {set(written)} but _MB_FIELDS declares {set(g._MB_FIELDS)}")


# --------------------------------------------- cli credential refusal must stop the run
# Run 36 (2 Aug): a revoked oauth token arrived as BackendError and was WARN'd per
# article — 887 identical 401s over 40 minutes, zero work done, run reported green.
# The classification lives in llm._cli; run._raise_if_credential_error maps it to the
# run-fatal ApiCredentialError every stage already checks for.
from pipeline import llm as _llm  # noqa: E402

# Verbatim from run 36's log.
RUN36_401 = "Failed to authenticate. API Error: 401 OAuth access token is invalid."


def _fake_cli(monkeypatch, stdout="", stderr="", returncode=1):
    class P:
        pass
    P.stdout, P.stderr, P.returncode = stdout, stderr, returncode
    monkeypatch.setattr(_llm, "cli_bin", lambda: "/fake/claude")
    monkeypatch.setattr(_llm.subprocess, "run", lambda *a, **k: P)


TOOL = {"input_schema": {"type": "object"}}


def test_cli_401_raises_credential_error_not_backend_error(monkeypatch):
    import json, pytest
    _fake_cli(monkeypatch,
              stdout=json.dumps({"is_error": True, "result": RUN36_401}))
    with pytest.raises(_llm.CredentialError):
        _llm._cli("sys", TOOL, "content", "model-x", 100)


def test_cli_401_on_stderr_with_empty_stdout_also_credential(monkeypatch):
    import pytest
    _fake_cli(monkeypatch, stdout="", stderr=RUN36_401)
    with pytest.raises(_llm.CredentialError):
        _llm._cli("sys", TOOL, "content", "model-x", 100)


def test_cli_rate_limit_still_rate_limited(monkeypatch):
    """The credential check must not swallow the usage-refusal path — RateLimited keeps
    its stop-and-resume semantics."""
    import json, pytest
    _fake_cli(monkeypatch,
              stdout=json.dumps({"is_error": True,
                                 "result": "usage limit reached, try again later"}))
    with pytest.raises(_llm.RateLimited):
        _llm._cli("sys", TOOL, "content", "model-x", 100)


def test_cli_other_error_stays_per_item(monkeypatch):
    import json, pytest
    _fake_cli(monkeypatch,
              stdout=json.dumps({"is_error": True, "result": "model overloaded"}))
    with pytest.raises(_llm.BackendError):
        _llm._cli("sys", TOOL, "content", "model-x", 100)


def test_credential_error_is_mapped_to_run_fatal():
    """Every WARN site calls _raise_if_credential_error before logging — the mapping is
    what turns 887 warnings into one red run."""
    import pytest
    from pipeline.run import ApiCredentialError, _raise_if_credential_error
    with pytest.raises(ApiCredentialError):
        _raise_if_credential_error(_llm.CredentialError(RUN36_401))


def test_non_credential_errors_pass_through_the_mapper():
    from pipeline.run import _raise_if_credential_error
    _raise_if_credential_error(_llm.BackendError("model overloaded"))   # no raise
    _raise_if_credential_error(ValueError("unrelated"))                 # no raise


# ---------------------------------------------------------------------------
# collect_district log line — fetch-failure regression (runs 44/46, 10-11 Aug 2026)

class _CollectStore:
    def seen_url(self, url): return False
    def near_duplicate(self, *a, **kw): return False
    def insert_article(self, row): return 1


def _feed_item(url, title="feed title"):
    from pipeline.collectors.rss import FeedItem
    return FeedItem(url=url, google_url=url, title=title, published_at=None,
                    source_name="Outlet", source_domain="outlet.example",
                    query="q", language="en", resolved=True)


def _collect_args():
    import argparse
    return argparse.Namespace(lang_terms=3, days=2, max_per_query=30,
                              delay=0, no_fetch=False)


def _patch_collect_env(monkeypatch, items, fetcher):
    from pipeline import run as run_mod
    monkeypatch.setattr(run_mod.configload, "keywords", lambda: {})
    monkeypatch.setattr(run_mod.configload, "settings", lambda: {})
    monkeypatch.setattr(run_mod.configload, "outlet_tiers", lambda: {})
    monkeypatch.setattr(run_mod.rss, "build_queries",
                        lambda d, kw, max_lang_terms: [("q", "en")])
    monkeypatch.setattr(run_mod.rss, "collect", lambda *a, **kw: items)
    monkeypatch.setattr(run_mod, "fetch_article", fetcher)
    return run_mod


def test_collect_survives_first_fetch_failure(monkeypatch, capsys):
    """When the district's FIRST resolved item raises on fetch, the log line used to
    read the never-assigned fetch result — the UnboundLocalError killed the entire
    collect stage for the run. The item must still insert as 'new', logged under its
    feed title."""
    def _boom(url, delay_s):
        raise OSError("connection reset")
    run_mod = _patch_collect_env(monkeypatch, [_feed_item("https://outlet.example/a")], _boom)
    st = run_mod.collect_district({"state": "Bihar", "district": "Supaul"},
                                  _CollectStore(), _collect_args())
    assert st["new"] == 1 and st["errors"] == 1
    assert "feed title" in capsys.readouterr().out


def test_collect_failed_fetch_never_logs_previous_items_title(monkeypatch, capsys):
    """A failed fetch after a successful one used to print the PREVIOUS item's page
    title (f survived the loop iteration) — same root cause, cosmetic symptom."""
    from types import SimpleNamespace
    def _fetch(url, delay_s):
        if url.endswith("/b"):
            raise OSError("connection reset")
        return SimpleNamespace(blocked_by_robots=False, clean_text="body text",
                               dedup_hash=None, url=url, published_at=None,
                               title="PAGE TITLE FROM ITEM A")
    items = [_feed_item("https://outlet.example/a", title="feed title A"),
             _feed_item("https://outlet.example/b", title="feed title B")]
    run_mod = _patch_collect_env(monkeypatch, items, _fetch)
    st = run_mod.collect_district({"state": "Bihar", "district": "Supaul"},
                                  _CollectStore(), _collect_args())
    out = capsys.readouterr().out
    assert st["new"] == 2
    assert any("feed title B" in ln for ln in out.splitlines())
    assert sum("PAGE TITLE FROM ITEM A" in ln for ln in out.splitlines()) == 1


# ---------------------------------------------------------------------------
# resolver canary v2 — per-run loss demotes to a warning unless the aged backlog
# shows the recovery path (nightly Mac-IP sweep) failing (14 Aug 2026)

from pipeline.run import resolver_canary  # noqa: E402

ALARM = "Google News resolver left 2600 of 5100 feed items unresolved (51%)"


def test_resolver_canary_quiet_when_share_healthy():
    # negative control: a growing pile with a HEALTHY resolver is collection volume,
    # not a resolver failure — the canary must not claim otherwise
    assert resolver_canary(None, 25_000, 20_000) is None


def test_resolver_canary_demotes_when_backlog_shrinking():
    assert resolver_canary(ALARM, 20_000, 23_000) is None


def test_resolver_canary_fires_when_backlog_grows():
    msg = resolver_canary(ALARM, 23_000, 20_000)
    assert msg and "not keeping up" in msg and ALARM in msg


def test_resolver_canary_grace_without_history():
    assert resolver_canary(ALARM, 23_000, None) is None
    assert resolver_canary(ALARM, None, 20_000) is None


def test_resolver_canary_floor_ignores_small_piles():
    # 1,900 aged rows is under two days of arrivals — routine churn, not a failure
    assert resolver_canary(ALARM, 1_900, 100) is None


def test_expired_status_is_in_the_migrated_vocabulary():
    """The same-commit rule (migrations 012/013 lesson): code that writes a
    CHECK-constrained term must ship with the migration declaring it."""
    import pathlib
    mig = pathlib.Path(__file__).resolve().parent.parent / "migrations"
    latest = sorted(p for p in mig.glob("*.sql")
                    if "processing_status" in p.read_text())[-1]
    assert "'expired'" in latest.read_text()
