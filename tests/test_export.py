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
    def __init__(self, n_incidents: int):
        self.n, self.calls, self._last = n_incidents, [], ""

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.split()))
        self._last = self.calls[-1]

    def fetchone(self):
        if "priority_tier_quantiles" in self._last:
            return ({"critical": 0.05, "high": 0.15, "medium": 0.40},)
        return (TIERS,)

    def fetchall(self):
        q = self._last
        if "config_defect_taxonomy" in q:
            return [("potholes_or_broken_surface", "Potholes / broken surface")]
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
            return [(i, 1, date(2026, 10, 2), "loc", "NH-1", "nh", "Bihar", "Patna", 1, 2,
                     ["pedestrian"], "summary", "auto_published", 0.9, 0.8, "road_city")
                    for i in range(self.n)]
        raise AssertionError(f"unexpected query: {q[:80]}")


class _Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _run(monkeypatch, tmp_path, n):
    cur = _Cur(n)
    monkeypatch.setattr(ex, "OUT", tmp_path)
    monkeypatch.setattr(ex, "connect", lambda: _Conn(cur))
    ex.main()
    return cur, json.loads((tmp_path / "incidents.json").read_text())


def test_export_query_count_does_not_grow_with_the_incident_table(monkeypatch, tmp_path):
    small, _ = _run(monkeypatch, tmp_path, 3)
    big, data = _run(monkeypatch, tmp_path, 300)
    assert len(big.calls) == len(small.calls) <= 8, (len(small.calls), len(big.calls))
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


def test_hotspot_export_carries_the_location_precision_label(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, 1)
    feats = json.loads((tmp_path / "hotspots.geojson").read_text())["features"]
    assert [f["properties"]["location_precision"] for f in feats] == ["feature", "point"]
    assert all(f["properties"]["escalation"] for f in feats)


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
