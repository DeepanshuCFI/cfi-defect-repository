"""The exporter is the only bridge from the DB to the public site. It must not issue
per-incident queries: 6,188 public incidents × 2 round trips × ~160 ms to the Tokyo
pooler was the 33-minute export step of 14 Oct 2026 (22 → 26 → 33 min across the
day's three runs, growing with the table). Children are fetched in bulk and grouped.

Run: python3 -m pytest tests/ -q
"""
import json
from datetime import date

from scripts import export_public as ex

TIERS = {"critical": 75, "high": 50, "medium": 25, "watch": 0}


class _Cur:
    """Fake cursor: answers each of the exporter's queries from canned rows and
    counts how many statements it was asked to run."""
    def __init__(self, n_incidents: int, site=None, incidents=None):
        self.n, self.calls, self._last = n_incidents, [], ""
        self.site, self.incidents = site, incidents

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.split()))
        self._last = self.calls[-1]


    def fetchone(self):
        if "priority_tier_quantiles" in self._last:
            return ({"critical": 0.05, "high": 0.15, "medium": 0.40},)
        if "key='ingestion'" in self._last:
            return ({"daily_states": ["Bihar", "Uttar Pradesh", "Rajasthan"]},)
        if "config_district" in self._last:
            return (146,)
        if "key='public_site'" in self._last:
            return (self.site,) if self.site is not None else None
        return (TIERS,)

    def fetchall(self):
        q = self._last
        if "config_defect_taxonomy" in q:
            return [("potholes_or_broken_surface", "Potholes / broken surface")]
        if "config_district" in q:
            return []
        if "public_hotspot" in q:
            # one feature-level hotspot (road with repeat crashes) and one point-level
            return [(1, 82.5, 25.4, "Sandi-Hardoi Road", "Uttar Pradesh", "Hardoi", None, 5, 2, 7,
                     date(2026, 7, 1), date(2026, 9, 30), ["potholes_or_broken_surface"], 61.0,
                     {"location_precision": "feature", "raw": {"distinct_points": 1, "records_total": 99, "records_public": 5, "hidden_casualty_crashes_6mo": 40}}, True, "open"),
                    (2, 82.6, 25.5, "Station Road", "Uttar Pradesh", "Hardoi", None, 3, 1, 2,
                     date(2026, 8, 1), date(2026, 9, 1), [], 40.0,
                     {"location_precision": "point", "raw": {"distinct_points": 3}}, True, "open")]
        if "public_incident_defect" in q:
            if "incident_id=%s" in q:                     # legacy per-incident shape
                return [("potholes_or_broken_surface", 0.9, "a crater")]
            return [(i, "potholes_or_broken_surface", 0.9, "a crater") for i in range(self.n)]
        if "incident_source" in q:
            if "incident_id=%s" in q:
                return [("https://x/1", "Outlet", date(2026, 10, 1))]
            return [(i, "https://x/1", "Outlet", date(2026, 10, 1)) for i in range(self.n)]
        if "from public_incident i" in q:
            if self.incidents is not None:
                return self.incidents
            return [_inc(i) for i in range(self.n)]
        raise AssertionError(f"unexpected query: {q[:80]}")


def _inc(i, hotspot=1, state="Bihar", district="Patna", deaths=1, precision="road",
         pinned_to="Sandi Hardoi Rd", pinned=True):
    """One public_incident row in the exporter's column order."""
    return (i, hotspot, date(2026, 10, 2), "loc", "NH-1", "nh", state, district, deaths, 2,
            ["pedestrian"], "summary", "auto_published", 0.9, 0.8, "road_city",
            precision, pinned_to, pinned)


class _Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _run(monkeypatch, tmp_path, n, **kw):
    cur = _Cur(n, **kw)
    monkeypatch.setattr(ex, "OUT", tmp_path)
    monkeypatch.setattr(ex, "connect", lambda: _Conn(cur))
    ex.main()
    return cur, json.loads((tmp_path / "incidents.json").read_text())


def test_export_query_count_does_not_grow_with_the_incident_table(monkeypatch, tmp_path):
    small, _ = _run(monkeypatch, tmp_path, 3)
    big, data = _run(monkeypatch, tmp_path, 300)
    assert len(big.calls) == len(small.calls) <= 9, (len(small.calls), len(big.calls))
    assert len(data) == 300
    assert data[7]["defects"] == [{"type": "potholes_or_broken_surface",
                                   "label": "Potholes / broken surface",
                                   "conf": 0.9, "evidence": "a crater"}]
    assert data[7]["sources"] == [{"url": "https://x/1", "outlet": "Outlet", "date": "2026-10-01"}]


def test_export_still_reads_only_public_views(monkeypatch, tmp_path):
    cur, _ = _run(monkeypatch, tmp_path, 2)
    child = [q for q in cur.calls if "incident_defect" in q or "incident_source" in q]
    assert child and all("public_incident" in q for q in child)   # §8 gate at the data layer


def test_attach_children_groups_by_incident_and_keeps_source_order():
    incidents = [{"id": 1}, {"id": 2}, {"id": 3}]
    defects = [(1, "potholes_or_broken_surface", 0.9, "s1"), (1, "no_street_lighting", 0.7, "s2")]
    sources = [(2, "https://a", "A", date(2026, 1, 1)), (2, "https://b", "B", None)]
    out = ex.attach_children(incidents, defects, sources, {"no_street_lighting": "No street lighting"})
    assert [d["type"] for d in out[0]["defects"]] == ["potholes_or_broken_surface", "no_street_lighting"]
    assert out[0]["defects"][1]["label"] == "No street lighting"
    assert out[0]["defects"][0]["label"] == "potholes_or_broken_surface"   # unknown code falls back to itself
    assert out[1]["sources"] == [{"url": "https://a", "outlet": "A", "date": "2026-01-01"},
                                 {"url": "https://b", "outlet": "B", "date": None}]
    assert out[2]["defects"] == [] and out[2]["sources"] == []


def test_hotspot_export_carries_precision_and_the_name_it_is_pinned_to(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, 3)                       # 3 road-level reports in hotspot 1
    feats = json.loads((tmp_path / "hotspots.geojson").read_text())["features"]
    p1, p2 = feats[0]["properties"], feats[1]["properties"]
    assert (p1["precision"], p1["place"]) == ("road", "Sandi Hardoi Rd")
    assert (p2["precision"], p2["place"]) == ("unchecked", None)     # no members assessed
    assert "location_precision" not in p1, "the old point/feature label is retired"


def test_a_location_is_as_precise_as_its_least_precise_report():
    m = lambda c, name=None: {"precision": c, "pinned_to": name}        # noqa: E731
    assert ex.hotspot_precision([m("road"), m("town"), m("spot")]) == "town"
    assert ex.hotspot_precision([m("spot"), m("road")]) == "road"
    assert ex.hotspot_precision([m(None), m("locality")]) == "locality"
    assert ex.hotspot_precision([m(None)]) == ex.hotspot_precision([]) == "unchecked"
    assert ex.hotspot_place([m("town", "Bilgram"), m("town", "Bilgram"), m("road", "Sandi Rd")]) == "Bilgram"
    assert ex.hotspot_place([m("town")]) is None


def test_the_repeat_crash_flag_is_withdrawn_unless_the_site_setting_turns_it_on(monkeypatch, tmp_path):
    """16 Nov 2026: of 671 public flags, none could be shown to be three crashes at one
    spot. Both fake hotspots are escalation candidates in the database."""
    _run(monkeypatch, tmp_path, 3)
    feats = json.loads((tmp_path / "hotspots.geojson").read_text())["features"]
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert not any(f["properties"]["escalation"] for f in feats)
    assert {f["properties"]["evidence"] for f in feats} == {"repeat"}
    assert meta["by_evidence"]["escalation"] == 0 and meta["site"]["repeat_crash_flag"] is False
    _run(monkeypatch, tmp_path, 3, site={"repeat_crash_flag": True})
    feats = json.loads((tmp_path / "hotspots.geojson").read_text())["features"]
    assert all(f["properties"]["escalation"] for f in feats)
    assert ex.evidence_class(5, True) == "repeat" and ex.evidence_class(5, True, True) == "escalation"


def test_site_settings_ignore_junk_and_default_safely():
    assert ex.public_site_settings(None) == ex.PUBLIC_SITE_DEFAULTS
    assert ex.public_site_settings({"coverage_only": False, "_note": "x", "other": 1}) == \
        {"coverage_only": False, "repeat_crash_flag": False}
    assert ex.public_site_settings("garbage") == ex.PUBLIC_SITE_DEFAULTS


def test_reports_outside_the_covered_states_stay_off_the_site_and_out_of_the_headline(monkeypatch, tmp_path):
    """297 reports from uncovered states were 4% of reports and 181 of 613 headline
    deaths (16 Nov 2026). The collector does not cover them; the headline must not."""
    rows = [_inc(1, deaths=1), _inc(2, deaths=2),
            _inc(3, state="Maharashtra", district="Pune", deaths=40, hotspot=None)]
    _, data = _run(monkeypatch, tmp_path, 0, incidents=rows)
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert [i["id"] for i in data] == [1, 2]
    assert meta["incidents"] == 2 and meta["fatalities"] == 3
    assert meta["outside_coverage"] == {"reports": 1, "deaths": 40}
    # and it is a setting, not a deletion
    _, data = _run(monkeypatch, tmp_path, 0, incidents=rows, site={"coverage_only": False})
    assert len(data) == 3
    assert ex.in_coverage("Kerala", [], True), "no coverage list must not blank the site"


def test_a_report_without_a_pin_is_listed_by_district_never_under_a_location(monkeypatch, tmp_path):
    rows = [_inc(1),                                                        # on the map
            _inc(2, precision=None, pinned_to=None, pinned=False),          # stale link, no pin
            _inc(3, hotspot=None, precision="highway", pinned_to="NH 31", pinned=False),
            _inc(4, hotspot=None, district="Gaya", precision=None, pinned_to=None, pinned=False),
            _inc(5, hotspot=999)]                                           # location not exported
    _, data = _run(monkeypatch, tmp_path, 0, incidents=rows)
    by_id = {i["id"]: i for i in data}
    assert [i for i in by_id if by_id[i]["mapped"]] == [1]
    assert by_id[2]["hotspot_id"] is None, "a report with no pin must not sit under its old location"
    assert by_id[3]["unmapped_reason"] == "highway" and by_id[4]["unmapped_reason"] == "unplaced"
    assert [m["id"] for m in json.loads((tmp_path / "incidents" / "1.json").read_text())] == [1]
    index = json.loads((tmp_path / "unmapped" / "index.json").read_text())
    assert [(g["state"], g["district"], g["count"], g["highway"]) for g in index] == \
        [("Bihar", "Gaya", 1, 0), ("Bihar", "Patna", 3, 1)]
    listed = [i["id"] for g in index for i in json.loads((tmp_path / "unmapped" / g["file"]).read_text())]
    assert sorted(listed) == [2, 3, 4, 5]
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert (meta["incidents"], meta["mapped"], meta["unmapped"], meta["unmapped_highway"]) == (5, 1, 4, 1)
    # stale district files from an earlier export are removed
    (tmp_path / "unmapped" / "77.json").write_text("[]")
    _run(monkeypatch, tmp_path, 0, incidents=rows)
    assert not (tmp_path / "unmapped" / "77.json").exists()


def test_hotspot_export_tiers_by_quantile_and_states_what_is_hidden(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, 1)
    feats = json.loads((tmp_path / "hotspots.geojson").read_text())["features"]
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["tiers"]["critical"] >= meta["tiers"]["high"] >= meta["tiers"]["medium"] >= 0
    assert meta["tiers"]["critical"] == 61.0 and meta["tier_rule"].startswith("quantiles")   # top score is the cut of two
    assert [f["properties"]["tier"] for f in feats] == ["critical", "watch"]   # with two scores every cut is the top one
    p = feats[0]["properties"]
    assert (p["records_public"], p["records_hidden"], p["hidden_casualty_crashes_6mo"]) == (5, 94, 40)
    assert feats[1]["properties"]["records_hidden"] == 0                         # no raw -> nothing hidden
def test_export_writes_one_member_file_per_hotspot_and_states_coverage(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, 4)                       # 4 incidents, all in hotspot 1
    files = sorted(p.name for p in (tmp_path / "incidents").glob("*.json"))
    assert files == ["1.json", "2.json"]                 # every public hotspot gets a file, even with no members
    assert len(json.loads((tmp_path / "incidents" / "1.json").read_text())) == 4
    assert json.loads((tmp_path / "incidents" / "2.json").read_text()) == []
    feats = json.loads((tmp_path / "hotspots.geojson").read_text())["features"]
    assert feats[0]["properties"]["road_types"] == ["nh"] and feats[1]["properties"]["road_types"] == []
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["road_types"] == ["nh"] and meta["with_casualties"] == 4
    assert meta["coverage"] == {"states": ["Bihar", "Uttar Pradesh", "Rajasthan"], "districts": 146}
    # stale per-hotspot files from a previous export are removed
    (tmp_path / "incidents" / "999.json").write_text("[]")
    _run(monkeypatch, tmp_path, 1)
    assert not (tmp_path / "incidents" / "999.json").exists()


def test_group_by_hotspot_keeps_every_hotspot():
    out = ex.group_by_hotspot([{"hotspot_id": 2, "id": 9}, {"hotspot_id": 2, "id": 10}], [1, 2])
    assert out == {1: [], 2: [{"hotspot_id": 2, "id": 9}, {"hotspot_id": 2, "id": 10}]}
