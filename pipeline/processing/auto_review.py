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
import anthropic

from pipeline import configload
from pipeline.settings import ANTHROPIC_API_KEY

_client: anthropic.Anthropic | None = None

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


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


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


def run(limit: int = 200) -> dict:
    """Apply mode: adjudicate the queue, apply decide(), audit-log every action."""
    import json as _json

    from pipeline.db import connect
    stats = {"reviewed": 0, "auto_published": 0, "rejected": 0, "machine_ok": 0,
             "left_for_human": 0, "errors": 0}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select r.id, r.location_text_best, r.fatalities, r.injuries, r.infra_implicated,
                 r.extraction_confidence, r.geocode_confidence, r.narrative_summary,
                 a.clean_text
          from incident r
          join source_article a on a.id = r.primary_source_id
          where a.clean_text is not null and r.verification_status = 'auto'
          order by r.id limit %s""", (limit,))
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
                print(f"  WARN auto-review failed #{iid}: {e}")
                stats["errors"] += 1
                continue
            stats["reviewed"] += 1
            action = decide(verdict, gc)
            if action is None:
                # adjudicate-once: flip to 'needs_human' so tomorrow's run doesn't pay
                # to re-judge the same item; the review_queue view still shows it.
                cur.execute("update incident set verification_status='needs_human', "
                            "updated_at=now() where id=%s", (iid,))
                cur.execute("""insert into review_action (entity_type, entity_id, reviewer,
                               action, after_json, note) values ('incident',%s,
                               'pipeline:auto_review','edit',%s::jsonb,%s)""",
                            (iid, _json.dumps(verdict),
                             f"auto-review -> needs_human (conf {verdict.get('confidence')}): "
                             f"{verdict.get('reason','')[:150]}"))
                conn.commit()
                stats["left_for_human"] += 1
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
    model = configload.settings()["models"]["extraction"]   # strong model — this gates publication
    content = (f"FIRST-PASS EXTRACTION:\n{extraction_summary}\n\n"
               f"FULL ARTICLE:\n{article_text[:7000]}")
    msg = _get_client().messages.create(
        model=model, max_tokens=400, system=SYSTEM,
        tools=[TOOL], tool_choice={"type": "tool", "name": "record_review"},
        messages=[{"role": "user", "content": content}])
    from pipeline import llmcost
    llmcost.add(model, msg.usage)
    for block in msg.content:
        if block.type == "tool_use":
            return dict(block.input)
    return {"verdict": "needs_human", "confidence": 0, "reason": "no tool output",
            "infra_causal": False, "infra_mentioned": False, "extraction_faithful": False}
