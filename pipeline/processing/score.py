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
    cfg = configload.settings()
    weights = cfg["priority_weights"]
    esc = cfg["escalation_rule"]
    today = today or date.today()
    scored = 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select id from hotspot")
        hids = [r[0] for r in cur.fetchall()]
        for hid in hids:
            # NOTE: incident aggregates and source counts computed separately —
            # joining incident_source here multiplies incident rows per source
            # (found live: a 4-source incident scored as 4 crashes).
            cur.execute("""
              select coalesce(sum(i.fatalities),0), coalesce(sum(i.injuries),0),
                     count(*) filter (where i.crash_date >= %s - interval '6 months'),
                     %s - max(i.crash_date),
                     avg(case when i.victim_types && %s::text[] then 1.0 else 0.0 end),
                     (select count(distinct s.source_article_id)
                      from incident_source s
                      join incident i2 on i2.id = s.incident_id
                      where i2.cluster_id = %s),
                     avg(i.geocode_confidence)
              from incident i
              where i.cluster_id = %s""",
              (today, today, list(VULNERABLE), hid, hid))
            (fat, inj, inc6, days_last, vuln, nsrc, gconf) = cur.fetchone()
            cur.execute("""
              select max(t.severity_weight)
              from incident_defect d
              join config_defect_taxonomy t on t.code = d.defect_type
              where d.incident_id in (select id from incident where cluster_id=%s)
                and t.maps_to_defects""", (hid,))
            max_sev = cur.fetchone()[0]
            raw = {"fatalities": int(fat), "injuries": int(inj), "inc_6mo": int(inc6),
                   "days_since_last": days_last if days_last is None else int(days_last),
                   "vulnerable_share": float(vuln or 0), "max_severity": float(max_sev or 0),
                   "n_sources": int(nsrc), "avg_geocode_conf": float(gconf or 0)}
            comp = components(raw, cfg)
            score = total_score(comp, weights)
            t = tier(score, cfg["priority_tiers"])
            escalate = raw["inc_6mo"] >= esc["min_incidents"]
            breakdown = {"raw": raw, "components": {k: round(v, 4) for k, v in comp.items()},
                         "weights": weights, "tier": t, "computed_for": str(today)}
            cur.execute("""
              update hotspot set priority_score=%s, score_breakdown=%s::jsonb,
                escalation_candidate=%s, last_recomputed_at=now() where id=%s""",
                (score, json.dumps(breakdown), escalate, hid))
            scored += 1
        conn.commit()
    return {"hotspots_scored": scored}
