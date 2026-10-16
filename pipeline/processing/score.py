"""Priority engine (BUILD_SPEC §9) — per-hotspot 0–100 score, transparent breakdown.

score = 100 * Σ wᵢ·componentᵢ, weights from config_setting.priority_weights.

Components (each 0–1, saturating normalisations so no single monster hotspot
flattens the rest; every raw value is stored in score_breakdown — never a black box):
  casualties  1 - exp(-weighted/10)   weighted = 5·fatalities + 1·injuries (all
                                      injuries treated as minor — CONSERVATIVE, since
                                      news rarely distinguishes grievous)
  frequency   1 - exp(-inc_6mo/3)     trailing 6 months
  recency     exp(-days_since_last/half_life)      (config recency_half_life_days)
  vulnerable  share of member incidents involving pedestrian/cyclist/two-wheeler
  defect_sev  max taxonomy severity_weight among dominant defects / 5
  evidence    min(1, distinct_sources/3) · avg geocode_confidence

Headline rule: ≥ escalation_rule.min_incidents in escalation_rule.window_months
→ escalation_candidate = true.
"""
import json
import math
from datetime import date

from pipeline import configload
from pipeline.db import connect

VULNERABLE = {"pedestrian", "cyclist", "two_wheeler"}


# ------------------------------------------------------------- escalation flag
# Which incidents VOTE toward the headline rule (>= min_incidents in window_months).
# 16 Oct 2026 audit of the 1,770 flagged hotspots: 1,729 sat on one or two identical
# coordinates (avg 1.2 distinct points) because every story naming the same road or
# landmark resolves to the same feature point; 948 held no defect report at all; wide-
# area features ('Itawa-Bareilly Highway', 'Ganga Expressway') contributed 35-86
# "incidents at one location". Two changes, and one honest label:
#   1. only CRASHES WITH CASUALTIES vote — a pothole or waterlogging complaint is
#      evidence of a defect, not an incident at a blackspot (87% of published pins are
#      casualty-free defect reports, and they were being counted);
#   2. only TIGHT pins vote — geocode_qualifier is null means the hit was anchored,
#      verified and spans < WIDE_AREA_KM; a pin somewhere on 47 km of expressway says
#      nothing about a location;
#   3. distinct_points is recorded so the surface can say what it has: >= 3 distinct
#      coordinates is a point-level pattern, anything less is a named feature (a road)
#      with repeat crashes — real, useful to a PWD/NHAI engineer, but road-level.
# Scoring components (frequency, casualties, ...) are unchanged; only the flag and the
# breakdown metadata move.
ESCALATION_INCIDENT_SQL = "(i.fatalities > 0 or i.injuries > 0) and i.geocode_qualifier is null"
POINT_PRECISION_MIN_DISTINCT = 3

# Scoring inputs come from PUBLIC members only (launch review, 16 Oct 2026). The score
# and tier used to be computed over every record clustered at a location — including
# the two thirds that are crash-only and never publish — while the dossier showed only
# the public rows. Readers saw "5 reports, 0 deaths, Critical 95.7" at the top of the
# rankings because the score rested on rows they could not see. Now:
#   - casualties, frequency, recency, vulnerable share, geocode confidence, sources and
#     defect severity are aggregated over public members (pi.id is not null);
#   - frequency and recency use a REPORT date every row has — crash_date when known,
#     else the article's published date, else the row's created date — because 85% of
#     public entries are casualty-free defect reports with no crash date;
#   - records_total / records_public / hidden_casualty_crashes_6mo ride in the
#     breakdown so the dossier can say "also N crash records here not shown publicly";
#   - the escalation flag is unchanged: it votes over all casualty crashes on tight
#     pins (the unpublished crash records ARE the repeat-crash evidence).
# LEFT JOIN from hotspot (hotspots with no incidents still score — see run()); params
# from agg_params(today).
REPORT_DATE_SQL = "coalesce(i.crash_date, a.published_at::date, i.created_at::date)"
HOTSPOT_AGG_SQL = f"""
  select h.id,
         coalesce(sum(i.fatalities) filter (where pi.id is not null), 0),
         coalesce(sum(i.injuries) filter (where pi.id is not null), 0),
         count(i.id) filter (where pi.id is not null
                               and {REPORT_DATE_SQL} >= %s::date - interval '6 months'),
         %s::date - max({REPORT_DATE_SQL}) filter (where pi.id is not null),
         avg(case when i.victim_types && %s::text[] then 1.0 else 0.0 end)
             filter (where pi.id is not null),
         avg(i.geocode_confidence) filter (where pi.id is not null),
         count(i.id) filter (where i.crash_date >= %s::date - interval '6 months'
                               and {ESCALATION_INCIDENT_SQL}),
         count(distinct ST_AsText(i.geom::geometry))
               filter (where i.crash_date >= %s::date - interval '6 months'
                         and {ESCALATION_INCIDENT_SQL}),
         count(i.id),
         count(i.id) filter (where pi.id is not null),
         count(i.id) filter (where pi.id is null
                               and i.crash_date >= %s::date - interval '6 months'
                               and (i.fatalities > 0 or i.injuries > 0))
  from hotspot h
  left join incident i on i.cluster_id = h.id
  left join public_incident pi on pi.id = i.id
  left join source_article a on a.id = i.primary_source_id
  group by h.id"""


def agg_params(today: date) -> tuple:
    """Positional params for HOTSPOT_AGG_SQL, in order of its %s placeholders."""
    return (today, today, list(VULNERABLE), today, today, today)


# Evidence strength and defect severity: public members only, same reason.
PUBLIC_SOURCES_SQL = """
  select i2.cluster_id, count(distinct s.source_article_id)
  from incident_source s
  join public_incident i2 on i2.id = s.incident_id
  where i2.cluster_id is not null group by i2.cluster_id"""

PUBLIC_SEVERITY_SQL = """
  select i.cluster_id, max(t.severity_weight)
  from incident_defect d
  join config_defect_taxonomy t on t.code = d.defect_type
  join public_incident i on i.id = d.incident_id
  where t.maps_to_defects and i.cluster_id is not null
  group by i.cluster_id"""


# Tiers are QUANTILES of the public score distribution, not fixed cut-offs: with fixed
# thresholds 17% of locations were Critical and a third sat in the top two tiers, so the
# badge told a reader nothing. Shares are cumulative from the top; the rest is watch.
TIER_QUANTILES = {"critical": 0.05, "high": 0.15, "medium": 0.40}
FIXED_TIERS_FALLBACK = {"critical": 75.0, "high": 50.0, "medium": 25.0, "watch": 0.0}


def quantile_thresholds(scores, quantiles: dict | None = None) -> dict:
    """Score cut-offs such that the top `critical` share is critical, the top `high`
    share is at least high, the top `medium` share at least medium (pure, unit-tested).
    Ties at a cut all qualify. Empty input falls back to the fixed thresholds."""
    q = quantiles or TIER_QUANTILES
    s = sorted((float(x) for x in scores if x is not None), reverse=True)
    if not s:
        return dict(FIXED_TIERS_FALLBACK)

    def cut(share: float) -> float:
        k = max(1, math.ceil(float(share) * len(s)))
        return s[min(k, len(s)) - 1]
    return {"critical": cut(q["critical"]), "high": cut(q["high"]),
            "medium": cut(q["medium"]), "watch": 0.0}


def escalation_flag(esc_incidents_6mo: int, esc_cfg: dict) -> bool:
    """The headline rule, over voting incidents only (pure, unit-tested)."""
    return int(esc_incidents_6mo or 0) >= int(esc_cfg["min_incidents"])


def location_precision(distinct_points: int | None) -> str:
    """'point' when >= POINT_PRECISION_MIN_DISTINCT distinct coordinates stand behind the
    flag, else 'feature' — the hotspot is a named road/landmark with repeat crashes."""
    return "point" if (distinct_points or 0) >= POINT_PRECISION_MIN_DISTINCT else "feature"


def components(raw: dict, cfg: dict) -> dict:
    """Pure scoring math — unit-tested. raw: fatalities, injuries, inc_6mo,
    days_since_last, vulnerable_share, max_severity, n_sources, avg_geocode_conf."""
    cw = cfg.get("casualty_weights", {"fatality": 5, "minor_injury": 1})
    weighted_cas = cw["fatality"] * raw["fatalities"] + cw.get("minor_injury", 1) * raw["injuries"]
    half_life = cfg.get("recency_half_life_days", 90)
    return {
        "casualties": 1 - math.exp(-weighted_cas / 10),
        "frequency": 1 - math.exp(-raw["inc_6mo"] / 3),
        "recency": math.exp(-max(raw["days_since_last"], 0) / half_life)
                   if raw["days_since_last"] is not None else 0.0,
        "vulnerable": raw["vulnerable_share"],
        "defect_sev": (raw["max_severity"] or 0) / 5.0,
        "evidence": min(1.0, raw["n_sources"] / 3.0) * (raw["avg_geocode_conf"] or 0),
    }


def total_score(comp: dict, weights: dict) -> float:
    w = {"casualties": weights["w1_fatalities_weighted"],
         "frequency": weights["w2_crash_frequency"],
         "recency": weights["w3_recency"],
         "vulnerable": weights["w4_vulnerable_user_share"],
         "defect_sev": weights["w5_defect_severity"],
         "evidence": weights["w6_evidence_strength"]}
    return round(100 * sum(w[k] * comp[k] for k in w), 1)


def tier(score: float, tiers: dict) -> str:
    for name in ("critical", "high", "medium", "watch"):
        if score >= tiers[name]:
            return name
    return "watch"


def run(today: date | None = None) -> dict:
    """Score every hotspot.

    SET-BASED, not per-hotspot. The original looped over hotspots issuing three
    statements each. That was fine at 45 hotspots and is not at 1,917: DATABASE_URL
    points at the Tokyo session pooler (GitHub runners are IPv4-only), measured 168ms
    round-trip from India, so ~5,750 sequential statements cost ~16 minutes of pure
    network wait with the database sitting idle in transaction. It grows linearly with
    hotspot count, inside the same 360-minute ceiling as collect and extract — and a
    timeout cancels the job, skipping export and deploy.

    Now: three aggregate reads, scoring in Python (the pure, unit-tested functions are
    untouched), one batched write. Four round trips regardless of hotspot count.

    Two semantics that are easy to lose here and are deliberately preserved:
      - LEFT JOIN from hotspot, so the 24 hotspots with no incidents are still scored.
        A plain GROUP BY over incident would silently skip them.
      - n_sources stays a SEPARATE query. Joining incident_source into the aggregate
        multiplies incident rows per source — that bug shipped once, and a 4-source
        incident scored as 4 crashes.
    """
    cfg = configload.settings()
    weights = cfg["priority_weights"]
    esc = cfg["escalation_rule"]
    today = today or date.today()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(HOTSPOT_AGG_SQL, agg_params(today))
        agg = {r[0]: r[1:] for r in cur.fetchall()}
        cur.execute(PUBLIC_SOURCES_SQL)
        sources = dict(cur.fetchall())
        cur.execute(PUBLIC_SEVERITY_SQL)
        severity = dict(cur.fetchall())

        rows = []
        for hid, (fat, inj, inc6, days_last, vuln, gconf, esc6, pts,
                  n_total, n_public, hidden_cas) in agg.items():
            raw = {"fatalities": int(fat), "injuries": int(inj), "inc_6mo": int(inc6),
                   "days_since_last": days_last if days_last is None else int(days_last),
                   "vulnerable_share": float(vuln or 0),
                   "max_severity": float(severity.get(hid) or 0),
                   "n_sources": int(sources.get(hid, 0)),
                   "avg_geocode_conf": float(gconf or 0),
                   "esc_incidents_6mo": int(esc6), "distinct_points": int(pts),
                   "records_total": int(n_total), "records_public": int(n_public),
                   "hidden_casualty_crashes_6mo": int(hidden_cas)}
            comp = components(raw, cfg)
            rows.append((hid, raw, comp, total_score(comp, weights)))

        # tiers: quantiles of the PUBLIC score distribution (hotspots with >= 1 public
        # record are the ones the map shows); non-public hotspots take the same cuts
        thresholds = quantile_thresholds(
            [sc for _, raw, _, sc in rows if raw["records_public"] > 0],
            cfg.get("priority_tier_quantiles"))

        ids, scores, breakdowns, escalates = [], [], [], []
        for hid, raw, comp, score in rows:
            ids.append(hid)
            scores.append(score)
            breakdowns.append(json.dumps({
                "raw": raw, "components": {k: round(v, 4) for k, v in comp.items()},
                "weights": weights, "tier": tier(score, thresholds),
                "tier_thresholds": thresholds,
                "location_precision": location_precision(raw["distinct_points"]),
                "computed_for": str(today)}))
            escalates.append(escalation_flag(raw["esc_incidents_6mo"], esc))

        if ids:
            cur.execute("""
              update hotspot h
              set priority_score = d.score, score_breakdown = d.bd::jsonb,
                  escalation_candidate = d.esc, last_recomputed_at = now()
              from (select unnest(%s::int[]) id, unnest(%s::float8[]) score,
                           unnest(%s::text[]) bd, unnest(%s::bool[]) esc) d
              where h.id = d.id""", (ids, scores, breakdowns, escalates))
        conn.commit()
    return {"hotspots_scored": len(ids)}
