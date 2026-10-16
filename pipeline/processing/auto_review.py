"""Auto-reviewer (second-pass skeptical adjudicator) for the review queue.

A SECOND, independent model pass re-reads the full article plus the first
extraction and rules on exactly the two questions a human reviewer answers:
  1. causal   — does the article ATTRIBUTE the crash (at least partly) to a road
                infrastructure defect/deficiency?
  2. mention  — does the article report a road infra defect/deficiency at all
                (including demand stories with no crash)?

Verdicts and what the pipeline may do with them (hard safety rails):
  confirm_publish  — infra evidence solid AND extraction faithful. Eligible for
                     auto-approve ONLY if geocode_confidence ≥ 0.6 — the auto-
                     reviewer NEVER overrides the location gate (a wrong public
                     pin is worse than a queued record).
  confirm_reject   — not a road crash / no infra content / extraction unfaithful.
  crash_only_ok    — real crash, correctly infra=false; keep for crash-frequency
                     counting, never publishable, no human needed.
  needs_human      — genuinely borderline. Stays in the queue.

Shadow mode prints verdicts without writing; apply mode writes review_action rows
as reviewer 'pipeline:auto_review' so every machine decision is auditable and
reversible, exactly like a human's.
"""

TOOL = {
    "name": "record_review",
    "description": "Record the second-pass review verdict.",
    "input_schema": {
        "type": "object",
        "properties": {
            "infra_causal": {
                "type": "boolean",
                "description": ("true ONLY if the article's own words attribute the "
                                "crash/hazard at least partly to road infrastructure "
                                "(potholes, missing signage/lighting/barrier, bad "
                                "geometry, waterlogging, unsafe work zone, …)")},
            "infra_mentioned": {
                "type": "boolean",
                "description": ("true if the article reports any road infrastructure "
                                "defect/deficiency at all, even without a crash "
                                "(e.g. residents demanding repairs)")},
            "extraction_faithful": {
                "type": "boolean",
                "description": ("true if the first extraction (location, casualties, "
                                "defects) is consistent with the article — no invented "
                                "or missed-critical facts")},
            "verdict": {
                "type": "string",
                "enum": ["confirm_publish", "confirm_reject", "crash_only_ok", "needs_human"],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "description": "≤25 words"},
        },
        "required": ["infra_causal", "infra_mentioned", "extraction_faithful",
                     "verdict", "confidence", "reason"],
    },
}

SYSTEM = (
    "You are the SKEPTICAL second reviewer for a public road-safety evidence registry "
    "used with the Indian government. A first pass extracted a structured record from "
    "a news article. Your job is to catch mistakes before publication.\n"
    "Rules:\n"
    "1. Judge ONLY from the article text. Never assume beyond its words.\n"
    "2. Distinguish sharply: infrastructure CAUSING/contributing to a crash vs "
    "infrastructure merely present in the scene (a car hitting a divider does not "
    "implicate the divider; 'gravel strewn on the road destabilised the bike' does).\n"
    "3. confirm_publish requires: real Indian road crash or infra-defect report, "
    "infra_mentioned or infra_causal true WITH clear textual support, and the first "
    "extraction faithful.\n"
    "4. confirm_reject: not India, not road-traffic, pure crime/suicide/weather, or "
    "extraction contradicts the article. CRITICAL: this registry is about ROADS ONLY — "
    "building/roof/ceiling/wall collapses, hospital/school/office premises, elevators, "
    "wells, and electrocutions are confirm_reject UNLESS the road itself is the hazard "
    "(e.g. a road cave-in, an open drain ON a road, waterlogged carriageway).\n"
    "5. crash_only_ok: genuine road crash, but cause is purely driver/vehicle "
    "behaviour and no infra deficiency is reported. This is a VALID record for "
    "crash-frequency statistics — not an error — just not publishable as a defect.\n"
    "6. When torn, choose needs_human. A wrong publication damages years of trust."
)


def decide(verdict: dict, geocode_conf: float | None) -> str | None:
    """Pure policy (unit-tested): map a verdict to an applied status, or None to
    leave the item for humans. Conservatism rails live HERE, in one place."""
    v, conf = verdict.get("verdict"), float(verdict.get("confidence") or 0)
    if v == "confirm_publish" and conf >= 0.75 and (geocode_conf or 0) >= 0.6:
        return "auto_published"
    if v == "confirm_reject" and conf >= 0.85:
        return "rejected"
    if v == "crash_only_ok" and conf >= 0.70:
        return "machine_ok"
    return None


def route_unresolved(geocode_conf: float | None, bar: float = 0.6) -> str:
    """Where an item goes when decide() returns None (pure, unit-tested).

    'needs_human' only when a human could actually publish it, i.e. the location bar
    is met; otherwise 'archived' — terminal, never public, still rescuable by the
    geocode rescue pass. 16 Oct 2026: 2,747 of 4,452 needs_human rows were below the
    bar, a queue nobody worked (66 human actions in 3.5 months)."""
    return "needs_human" if (geocode_conf or 0) >= bar else "archived"


def pending_query(gate: dict, limit: int) -> tuple[str, tuple]:
    """The adjudication queue: rows that can PUBLISH first, then the rest; oldest
    first within each group.

    Oldest-first alone starved publishing (14 Oct 2026): 2,064 older pending rows
    sat ahead of the day's fresh candidates and only 73 of them could pass the
    publish gate — the rest are low-confidence (landmark/district-level) or missing
    geocodes whose verdict can only ever be 'needs_human' (decide() never publishes
    below the geocode floor). At ~400 verdicts/day the fresh publishable rows would
    have waited ~5 days while the budget bought nothing the site could show.

    The gate expression mirrors gate.publishable() for the fields known at row
    level (located, infra implicated, both confidences at/above the config
    minimums); the defect-tag check still happens after the verdict. coalesce()
    matters: ORDER BY … DESC puts NULLs FIRST in Postgres, so a row with a NULL
    confidence would otherwise jump the queue. LIMIT stays in SQL so a 2k-row
    backlog never streams 2k article bodies to the runner."""
    infra = "and r.infra_implicated" if gate.get("require_infra_implicated") else ""
    sql = f"""
      select r.id, r.location_text_best, r.fatalities, r.injuries, r.infra_implicated,
             r.extraction_confidence, r.geocode_confidence, r.narrative_summary,
             a.clean_text
      from incident r
      join source_article a on a.id = r.primary_source_id
      where a.clean_text is not null and r.verification_status = 'auto'
      order by coalesce(r.geom is not null {infra}
                        and r.extraction_confidence >= %s
                        and r.geocode_confidence >= %s, false) desc,
               r.id
      limit %s"""
    return sql, (gate["extraction_confidence_min"], gate["geocode_confidence_min"], limit)


def run(limit: int = 200) -> dict:
    """Apply mode: adjudicate the queue, apply decide(), audit-log every action."""
    import json as _json

    from pipeline import configload
    from pipeline.db import connect
    stats = {"reviewed": 0, "auto_published": 0, "rejected": 0, "machine_ok": 0,
             "left_for_human": 0, "archived": 0, "errors": 0}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(*pending_query(configload.settings()["confidence_gate"], limit))
        rows = cur.fetchall()
        from pipeline import llmcost
        for (iid, loc, f, i, infra, ec, gc, summary, text) in rows:
            if llmcost.over():
                print(f"  BUDGET STOP in auto-review: ${llmcost.spent():.2f} >= "
                      f"${llmcost.budget():.2f} — remaining items wait for tomorrow")
                break
            cur.execute("""select defect_type, evidence_snippet from incident_defect
                           where incident_id=%s""", (iid,))
            defects = "; ".join(f"{d}: “{e[:100]}”" for d, e in cur.fetchall()) or "(none)"
            ext = (f"location: {loc}\ncasualties: {f} dead / {i} injured\n"
                   f"infra_implicated: {infra} (extraction conf {ec}, geocode conf {gc})\n"
                   f"summary: {summary}\ndefects: {defects}")
            try:
                verdict = adjudicate(text, ext)
            except Exception as e:
                from pipeline.run import _raise_if_credential_error
                _raise_if_credential_error(e)   # dead key must fail the run, not the item
                if "reached your specified api usage limits" in str(e).lower():
                    print("  API MONTHLY LIMIT hit — stopping; queue waits for tomorrow")
                    stats["api_limit_hit"] = True
                    break
                print(f"  WARN auto-review failed #{iid}: {e}")
                stats["errors"] += 1
                continue
            stats["reviewed"] += 1
            action = decide(verdict, gc)
            if action == "auto_published":
                # taxonomy-locked principle: no real defect tag => not publishable as a
                # defect dossier, however good the verdict (guard restored after
                # migration 009 dropped the old view's defect-existence check)
                cur.execute("""select 1 from incident_defect where incident_id=%s
                               and defect_type not in ('other_infrastructure',
                                   'no_infrastructure_defect_identified') limit 1""", (iid,))
                if cur.fetchone() is None:
                    action = None
            if action is None:
                # adjudicate-once: route out of 'auto' so tomorrow's run doesn't pay to
                # re-judge the same item. Humans get it only if they could publish it
                # (location bar met); below the bar it is archived — see route_unresolved.
                dest = route_unresolved(gc)
                cur.execute("update incident set verification_status=%s, "
                            "updated_at=now() where id=%s", (dest, iid))
                cur.execute("""insert into review_action (entity_type, entity_id, reviewer,
                               action, after_json, note) values ('incident',%s,
                               'pipeline:auto_review','edit',%s::jsonb,%s)""",
                            (iid, _json.dumps(verdict),
                             f"auto-review -> {dest} (conf {verdict.get('confidence')}"
                             f"{'' if dest == 'needs_human' else ', geocode below publish bar'}): "
                             f"{verdict.get('reason','')[:150]}"))
                conn.commit()
                stats["left_for_human" if dest == "needs_human" else "archived"] += 1
                continue
            cur.execute("update incident set verification_status=%s, updated_at=now() "
                        "where id=%s", (action, iid))
            cur.execute("""insert into review_action (entity_type, entity_id, reviewer,
                           action, after_json, note) values ('incident',%s,
                           'pipeline:auto_review',%s,%s::jsonb,%s)""",
                        (iid, "approve" if action == "auto_published" else
                              ("reject" if action == "rejected" else "edit"),
                         _json.dumps(verdict),
                         f"auto-review -> {action} (conf {verdict.get('confidence')}): "
                         f"{verdict.get('reason','')[:150]}"))
            conn.commit()
            stats[action] += 1
            print(f"  #{iid} -> {action} ({verdict.get('confidence'):.2f}) {verdict.get('reason','')[:70]}")
    return stats


def adjudicate(article_text: str, extraction_summary: str) -> dict:
    from pipeline import llm
    content = (f"FIRST-PASS EXTRACTION:\n{extraction_summary}\n\n"
               f"FULL ARTICLE:\n{article_text[:7000]}")
    # strong model — this gates publication
    return llm.structured(SYSTEM, TOOL, content,
                          model=llm.model_for("extraction"), max_tokens=400)
