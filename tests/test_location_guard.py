"""The name-evidence guard inside the geocoder, end to end (16 Nov 2026).

test_placematch.py covers the rule; this file covers what geocode() does with it:
which candidate wins, what is stored, and that the stored vocabulary, the confidence
table and the migration cannot drift apart.
"""
import re
from pathlib import Path

import pytest

from pipeline.processing import geocode as g
from pipeline.processing import placematch as pm

ROOT = Path(__file__).resolve().parent.parent
UP = "Uttar Pradesh"


def _hit(name, ftype, district="Hardoi", state=UP, lat=27.4, lon=80.1, ctx=None, alts=None,
         span=0.0):
    return {"lat": lat, "lon": lon, "state": state, "district": district,
            "display": f"{name}, {district}", "span_km": span, "name": name,
            "ftype": ftype, "ctx": ctx or {}, "alts": alts or []}


ROAD_IN_TOWN = _hit("Road", "street", ctx={"street": "Cinema Road", "place": "Hardoi"})
HARDOI_TOWN = _hit("Hardoi", "place", ctx={"place": "Hardoi"}, span=58.0)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(g, "_district_towns", lambda: frozenset({"sitapur", "hardoi"}))


# ------------------------------------------------------------------ the bug itself
def test_a_village_road_the_provider_does_not_know_gets_no_pin(monkeypatch):
    """Verbatim Mapbox behaviour, 16 Nov: every rung of the ladder answers with the
    street called 'Road' in Hardoi town, and the bare district tail answers with the
    town itself. Before the guard this published at 0.80."""
    asked = []

    def resolve(q):
        asked.append(q)
        return HARDOI_TOWN if q.lower().startswith("hardoi") else ROAD_IN_TOWN

    monkeypatch.setattr(g, "_resolve", resolve)
    out = g.geocode("Aureni-Mahapur road, Hardoi district, Uttar Pradesh",
                    admin_district="Hardoi", admin_state=UP)
    assert out["lat"] is None and out["geocode_confidence"] is None
    assert out["geocode_precision"] is None and out["geocode_method"] is None
    assert len(asked) >= 3, "should have descended the whole ladder before giving up"
    assert any(q.lower().startswith("hardoi district") for q in asked), \
        "the district-tail rung (formerly 'road_city' at 0.70) was not exercised"


def test_a_later_candidate_with_the_right_name_beats_a_generic_first_answer(monkeypatch):
    aureni = _hit("Aureni", "locality", lat=27.61, lon=80.33)
    first = dict(ROAD_IN_TOWN, alts=[_hit("Main Road", "street"), aureni])
    monkeypatch.setattr(g, "_resolve", lambda q: first)
    out = g.geocode("near Aureni village, Hardoi district", admin_district="Hardoi",
                    admin_state=UP)
    assert (out["lat"], out["lon"]) == (27.61, 80.33)
    assert out["geocode_precision"] == "locality" and out["geocode_match"] == "Aureni"
    assert out["geocode_confidence"] == g.PRECISION_CONF["locality"]


def test_the_right_road_is_a_road_not_a_spot(monkeypatch):
    hit = _hit("Sahpur Road", "street", district="Mathura",
               ctx={"street": "Sahpur Road", "locality": "Kosi Kalan", "place": "Chhata"})
    monkeypatch.setattr(g, "_resolve", lambda q: hit)
    out = g.geocode("Kosi Kalan-Shahpur road, Mathura, Uttar Pradesh",
                    admin_district="Mathura", admin_state=UP)
    assert out["geocode_precision"] == "road" and out["geocode_confidence"] == 0.80


def test_a_town_level_report_is_publishable_as_a_town(monkeypatch):
    """'Dastoi Road, Hapur': the road is unknown, the town is not. Honest answer: the
    town, labelled as a town — and it clears the publish bar as that."""
    hapur = _hit("Hapur", "place", district="Hapur", ctx={"place": "Hapur"}, span=40.0)
    generic = _hit("Road", "street", district="Hapur", ctx={"street": "Road", "place": "Hapur"})
    monkeypatch.setattr(g, "_resolve",
                        lambda q: hapur if q.lower().startswith("hapur") else generic)
    out = g.geocode("Dastoi Road, Hapur, Uttar Pradesh", admin_city="Hapur",
                    admin_district="Hapur", admin_state=UP)
    assert out["geocode_precision"] == "town" and out["geocode_match"] == "Hapur"
    assert out["geocode_confidence"] >= 0.6
    assert out["geocode_qualifier"] is None, \
        "a town's wide bounding box is what 'town' means, not a wide_area fault"


# ----------------------------------------------------------------- highway-only
NH31 = _hit("National Highway 31", "street", district="Begusarai", state="Bihar",
            ctx={"place": "Begusarai"})


def test_a_numbered_highway_alone_is_recorded_without_a_pin(monkeypatch):
    monkeypatch.setattr(g, "_resolve", lambda q: NH31)
    out = g.geocode("NH-31, Begusarai, Bihar", road_name="NH-31",
                    admin_district="Begusarai", admin_state="Bihar")
    assert out["lat"] is None and out["geocode_confidence"] is None
    assert out["geocode_precision"] == "highway"
    assert out["geocode_match"] == "National Highway 31"


def test_highway_only_does_not_stop_a_later_rung_placing_the_village(monkeypatch):
    bakhri = _hit("Bakhri", "locality", district="Begusarai", state="Bihar", lat=25.6, lon=86.2)
    monkeypatch.setattr(g, "_resolve",
                        lambda q: bakhri if q.lower().startswith("bakhri") else NH31)
    out = g.geocode("Bakhri, NH-31, Begusarai, Bihar", road_name="NH-31",
                    admin_district="Begusarai", admin_state="Bihar")
    assert out["geocode_precision"] == "locality" and out["lat"] == 25.6


# ---------------------------------------------------- the older guards still bite
def test_right_name_in_the_wrong_district_is_still_rejected(monkeypatch):
    wrong = _hit("Aureni", "locality", district="Sitapur")
    monkeypatch.setattr(g, "_resolve", lambda q: wrong)
    out = g.geocode("near Aureni village, Hardoi district", admin_district="Hardoi",
                    admin_state=UP)
    assert out["lat"] is None


def test_a_long_named_road_is_still_capped_as_wide_area(monkeypatch):
    ring = _hit("Outer Ring Road", "street", district="South Delhi", state="Delhi", span=47.0)
    monkeypatch.setattr(g, "_resolve", lambda q: ring)
    out = g.geocode("Outer Ring Road", admin_state="Delhi", admin_district="South Delhi")
    assert out["geocode_qualifier"] == "wide_area" and out["geocode_confidence"] < 0.6


# ------------------------------------------------- vocabulary, table and migration
def _precision_constraint() -> set[str]:
    body = (ROOT / "migrations" / "018_geocode_precision.sql").read_text()
    clause = body[body.index("add constraint incident_geocode_precision_check"):]
    return set(re.findall(r"'([a-z_]+)'", clause[:clause.index(";")]))


def test_every_precision_the_geocoder_can_write_is_allowed_by_the_constraint():
    """The same-commit rule, enforced: this vocabulary has a CHECK constraint, and a
    term written without its migration aborts the whole geocode batch (18 Jul, 1 Aug)."""
    assert set(pm.PRECISIONS) == _precision_constraint()


def test_the_guard_returns_only_declared_precisions():
    src = (ROOT / "pipeline" / "processing" / "placematch.py").read_text()
    body = src[src.index("def evidence("):]
    returned = set(re.findall(r'"(spot|road|locality|town|highway|area)"', body))
    assert returned == set(pm.PRECISIONS), f"evidence() can return {returned}"


def test_every_pinned_class_has_a_confidence_that_clears_both_bars():
    from pipeline.processing import auto_review, cluster
    assert set(g.PRECISION_CONF) == set(pm.PINNED) == set(pm.PRECISIONS) - {"highway"}
    for cls, conf in g.PRECISION_CONF.items():
        assert conf >= auto_review.GEOCODE_FLOOR, f"{cls} could never be published"
        assert conf >= cluster.MIN_GEOCODE_CONF, f"{cls} would never appear on the map"
    ranked = [g.PRECISION_CONF[c] for c in ("spot", "road", "locality", "town")]
    assert ranked == sorted(ranked, reverse=True), "tighter evidence must not score lower"


def test_the_view_is_recreated_so_the_new_columns_are_public():
    body = (ROOT / "migrations" / "018_geocode_precision.sql").read_text().lower()
    assert "create or replace view public_incident" in body
    assert "alter view public_incident set (security_invoker = true)" in body, \
        "CREATE OR REPLACE VIEW resets invoker rights (migration 007's gotcha)"
    # the gate itself must be 010's, unchanged
    ten = (ROOT / "migrations" / "010_auto_publish_needs_defect.sql").read_text().lower()
    gate = lambda sql: " ".join(sql[sql.index("where i.verification_status in"):
                                    sql.index("alter view public_incident")].split()).split("--")[0]
    assert gate(body) == gate(ten)


def test_the_geocode_stage_stores_what_the_geocoder_returns():
    import inspect

    from pipeline import run
    src = inspect.getsource(run.cmd_geocode)
    assert src.count("geocode_precision=%s") == 3, "main pass, highway-only, rescue pass"
    assert src.count("geocode_match=%s") == 3


def test_mapbox_is_asked_for_several_candidates_and_keeps_them(monkeypatch):
    monkeypatch.setattr(g, "_cache", {})
    monkeypatch.setattr(g, "_save_cache", lambda: None)
    monkeypatch.setattr(g, "MAPBOX_TOKEN", "test-token")
    seen = {}

    def feat(name, ftype, lon):
        return {"geometry": {"coordinates": [lon, 27.4]},
                "properties": {"name": name, "feature_type": ftype, "full_address": name,
                               "context": {"region": {"name": UP}, "district": {"name": "Hardoi"},
                                           "place": {"name": "Hardoi"},
                                           "locality": {"name": "Kurseli"}}}}

    class R:
        status_code = 200

        def json(self):
            return {"features": [feat("Road", "street", 80.1), feat("Aureni", "locality", 80.3)]}

    def get(url, params=None, **k):
        seen.update(params)
        return R()

    monkeypatch.setattr(g.httpx, "get", get)
    top = g._mapbox("Aureni road, Hardoi")
    assert seen["limit"] == g.MAPBOX_CANDIDATES > 1
    assert (top["name"], top["ftype"]) == ("Road", "street")
    assert top["ctx"] == {"street": "", "neighborhood": "", "locality": "Kurseli", "place": "Hardoi"}
    assert [a["name"] for a in top["alts"]] == ["Aureni"]
    assert [c["name"] for c in g._candidates("Aureni road, Hardoi")] == ["Road", "Aureni"]


def test_unlinking_and_rejected_votes():
    import inspect

    from pipeline.processing import cluster, score
    assert "set cluster_id = null" in " ".join(inspect.getsource(cluster.run).split())
    assert "verification_status <> 'rejected'" in score.ESCALATION_INCIDENT_SQL
