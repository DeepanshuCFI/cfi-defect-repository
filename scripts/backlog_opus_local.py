#!/usr/bin/env python3
"""One-off LOCAL backlog burn: Claude Code headless (Opus, owner's Max plan).

DELIBERATELY NOT part of the daily pipeline and not wired into cmd_daily. The cloud
run stays API-driven and autonomous — that property was bought on 4 Jul 2026 when the
launchd job was retired, and a backlog sweep is not worth trading it for. This is a
manual tool: you run it, you watch it, you stop it.

SCOPE (default): only articles carrying defect vocabulary. The other ~5,500 queued
articles are behaviour-only crash reports that become machine_ok and never publish;
they feed the >=3-in-6mo escalation counter and nothing else, so they are not worth
Opus tokens. --all overrides this if you disagree.

WHY ONE PASS: the API pipeline splits relevance (Haiku) from extraction (Sonnet)
because a cheap classifier filters before an expensive extractor. With Opus doing
both, that split is pure overhead — one call decides in-scope AND extracts.

CREDENTIALS — the important part. Claude Code prefers an API key over the
subscription when one is present, and pipeline.settings calls load_dotenv() at
import, so this process inherits ANTHROPIC_API_KEY from the repo .env. Passed to the
child, the batch would run to completion, look completely successful, and bill the
API account while spending zero Max tokens. Both key vars are stripped from the child
environment below, and _preflight() refuses to start if they would leak.

QUALITY: results go through the SAME guards as the API path — validate_snippets
(verbatim-substring, the anti-fabrication rule), coerce_incident (the null-poisoning
fix from run #9) and store.insert_incident, so the confidence gate, audit log and
crash-only routing all still apply. Nothing here publishes; auto_review still
adjudicates before anything reaches the map.

RESUMABLE: state lives in source_article.processing_status, so an interrupted run
costs nothing — rerun it and the finished articles are simply no longer 'fetched'.

Usage:
    python3 scripts/backlog_opus_local.py --dry-run          # show the plan, spend nothing
    python3 scripts/backlog_opus_local.py --limit 10         # one trial batch
    python3 scripts/backlog_opus_local.py                    # the 601
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

import psycopg
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import configload                              # noqa: E402
from pipeline.processing import auto_review as ar            # noqa: E402
from pipeline.processing import extract as ex                # noqa: E402
from pipeline.processing import prefilter                    # noqa: E402
from pipeline.store import get_store                         # noqa: E402

# Resolved, not hardcoded: an nvm-managed claude is not on a non-login shell's PATH,
# so CLAUDE_BIN is the escape hatch (e.g. ~/.nvm/versions/node/<ver>/bin/claude).
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "").strip()
MODEL = "claude-opus-5"
# Batching is the whole ballgame: every invocation carries Claude Code's system prompt
# and tool definitions. One article per call is ~5x token overhead; ten is ~1.4x.
BATCH = 10
TIMEOUT_S = 600
MAX_CONSECUTIVE_FAILURES = 3

SYSTEM = (
    ex.SYSTEM + "\n\nYou are also the relevance filter. An article is OUT OF SCOPE "
    "unless it reports a specific road-traffic crash in India, or a specific road-"
    "infrastructure defect/hazard in India. Out of scope: crashes outside India, "
    "crime, suicides, rail/air/boat accidents (unless at a road level crossing), "
    "pure statistics/policy pieces with no specific location, and building/roof/"
    "ceiling collapses, premises hazards, lifts, wells or electrocutions UNLESS the "
    "road itself is the hazard (road cave-in, open drain on a road, waterlogged "
    "carriageway)."
)


def _preflight() -> None:
    """Refuse to start if the run would silently bill the API instead of the plan.

    Ask `claude auth status`, which names the auth method outright. Do NOT infer this
    from total_cost_usd: that field reports the NOTIONAL cost of the tokens — what
    they would have cost on the API — and is populated on a subscription too. An
    earlier version of this check aborted a perfectly good subscription run because
    the probe reported $0.10."""
    if not CLAUDE_BIN or not Path(CLAUDE_BIN).exists():
        sys.exit("claude CLI not found on PATH. Install it with\n"
                 "  npm install -g @anthropic-ai/claude-code\n"
                 "or point CLAUDE_BIN at the binary — note an nvm-managed install is "
                 "not on a non-login shell's PATH:\n"
                 "  CLAUDE_BIN=~/.nvm/versions/node/<ver>/bin/claude python3 "
                 "scripts/backlog_opus_local.py")
    try:
        p = subprocess.run([CLAUDE_BIN, "auth", "status"], capture_output=True,
                           text=True, timeout=60, env=_child_env())
        st = json.loads(p.stdout)
    except Exception as e:
        sys.exit(f"could not read `claude auth status`: {e}")
    if not st.get("loggedIn"):
        sys.exit("claude is not logged in. Run the CLI and /login with your CLAUDE "
                 "SUBSCRIPTION (not the Console/API option).")
    if st.get("authMethod") != "claude.ai" or not st.get("subscriptionType"):
        sys.exit(f"ABORT: authMethod={st.get('authMethod')!r} "
                 f"subscriptionType={st.get('subscriptionType')!r} — this would bill an "
                 "API account, not the subscription. Log out and back in via claude.ai.")
    print(f"preflight ok · {CLAUDE_BIN} · {MODEL} · {st['subscriptionType']} plan "
          f"({st.get('email')}) · costs below are notional, not charges")


_STORE = None


def _db(fn, retries: int = 1):
    """Run a store operation, reconnecting once if the connection died under us.

    Supabase's session pooler drops connections that go idle or lose their network,
    and psycopg only discovers it on next use — a documented trap in this project
    (review console 500, 20 Jul: "long-lived Supabase pooler connections must always
    have retry-on-first-failure"). Run 1 of this script died to exactly that: an
    internet drop killed the socket, insert_incident raised, its own rollback() raised
    on the dead socket, and the error handler's set_article_status then raised again —
    taking the process down at batch 32 of 60 with no summary. Every DB call in the
    loop goes through here so a network blip costs one batch, not the run."""
    global _STORE
    for attempt in range(retries + 1):
        try:
            return fn(_STORE)
        except psycopg.OperationalError as e:
            if attempt == retries:
                raise
            print(f"  DB connection lost ({str(e).splitlines()[0][:70]}) — reconnecting")
            try:
                _STORE.close()
            except Exception:
                pass
            time.sleep(2)
            _STORE = get_store()


def _child_env() -> dict:
    env = dict(os.environ)
    # The load-bearing two lines in this file.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _claude(prompt: str) -> dict | None:
    try:
        p = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--model", MODEL,
             "--output-format", "json", "--max-turns", "1"],
            capture_output=True, text=True, timeout=TIMEOUT_S, env=_child_env())
    except subprocess.TimeoutExpired:
        return None
    if p.returncode != 0 and not p.stdout.strip():
        print(f"  claude exited {p.returncode}: {p.stderr[:300]}")
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        print(f"  unparseable envelope: {p.stdout[:300]}")
        return None


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json_array(text: str):
    """Opus is told to emit a bare array; be tolerant of a fence or a stray preamble
    anyway. A parse failure must never corrupt the registry — return None and the
    batch is left 'fetched' for a retry."""
    if not text:
        return None
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e <= s:
        return None
    try:
        out = json.loads(text[s:e + 1])
        return out if isinstance(out, list) else None
    except json.JSONDecodeError:
        return None


def build_prompt(arts: list[dict]) -> str:
    schema = json.dumps(ex.build_tool()["input_schema"]["properties"], ensure_ascii=False)
    parts = [
        SYSTEM,
        "\nFor EACH article below, return one JSON object. Return a BARE JSON ARRAY "
        "and nothing else — no prose, no markdown fence.",
        "\nEach object: {\"article_id\": <int>, \"in_scope\": <bool>, \"incident\": "
        "<object or null>}. Set in_scope=false and incident=null when out of scope. "
        "When in scope, `incident` uses exactly these fields:",
        schema,
        "\nevidence_snippet MUST be copied VERBATIM from that article's text — it is "
        "validated as a substring and silently dropped otherwise. Resolve relative "
        "dates against the article's published date. Never invent a value.",
    ]
    for a in arts:
        parts.append(
            f"\n===== ARTICLE {a['id']} "
            f"(published: {a.get('published_at') or 'unknown'}) =====\n"
            f"{(a.get('clean_text') or '')[:7000]}")
    return "\n".join(parts)


ADJ_REVIEWER = "pipeline:auto_review_opus_local"


def adjudicate_batch(rows: list[tuple]) -> list[dict] | None:
    """Second-pass adjudication for a batch, using auto_review's own SYSTEM and tool
    schema so the local path judges by identical rules to the API path."""
    schema = json.dumps(ar.TOOL["input_schema"]["properties"], ensure_ascii=False)
    parts = [
        ar.SYSTEM,
        "\nYou are reviewing SEVERAL records. Return a BARE JSON ARRAY, one object per "
        "record, nothing else — no prose, no markdown fence.",
        f"\nEach object: {{\"incident_id\": <int>, ...these fields}}:\n{schema}",
    ]
    for (iid, loc, f, i, infra, ec, gc, summary, text, defects) in rows:
        parts.append(
            f"\n===== RECORD {iid} =====\nFIRST-PASS EXTRACTION:\n"
            f"location: {loc}\ncasualties: {f} dead / {i} injured\n"
            f"infra_implicated: {infra} (extraction conf {ec}, geocode conf {gc})\n"
            f"summary: {summary}\ndefects: {defects}\n\n"
            f"FULL ARTICLE:\n{(text or '')[:7000]}")
    env = _claude("\n".join(parts))
    return _extract_json_array((env or {}).get("result", ""))


def cmd_adjudicate(args) -> None:
    """Clear the 'auto' queue locally instead of paying the API's metered daily share.

    Fidelity matters here — this gates publication on a government-facing registry, so
    it reuses auto_review.SYSTEM, auto_review.TOOL and auto_review.decide() verbatim
    rather than restating the policy. The two conservatism rails that live OUTSIDE
    decide() are replicated below: the defect-existence guard (restored after migration
    009 dropped it and put 13 no-defect records on the map) and adjudicate-once, which
    flips undecided items to needs_human so no later run re-judges them.

    Actions are audit-logged as a DISTINCT reviewer (…_opus_local) so this path's
    decisions stay identifiable and reversible on their own."""
    from pipeline.db import connect
    _preflight()
    stats = {"reviewed": 0, "auto_published": 0, "rejected": 0, "machine_ok": 0,
             "left_for_human": 0, "unparsed": 0, "failed_batches": 0}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select r.id, r.location_text_best, r.fatalities, r.injuries, r.infra_implicated,
                 r.extraction_confidence, r.geocode_confidence, r.narrative_summary,
                 a.clean_text
          from incident r join source_article a on a.id = r.primary_source_id
          where a.clean_text is not null and r.verification_status = 'auto'
          order by r.id limit %s""", (args.limit,))
        base = cur.fetchall()
        rows = []
        for r in base:
            cur.execute("""select defect_type, evidence_snippet from incident_defect
                           where incident_id=%s""", (r[0],))
            d = "; ".join(f"{dt}: “{ev[:100]}”" for dt, ev in cur.fetchall()) or "(none)"
            rows.append((*r, d))
        print(f"adjudicating {len(rows)} incident(s) · batch {args.batch}")
        if args.dry_run:
            print("dry run — nothing sent, nothing written")
            return

        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            print(f"\n[batch {i // args.batch + 1}] ids "
                  f"{chunk[0][0]}–{chunk[-1][0]}")
            verdicts = adjudicate_batch(chunk)
            if verdicts is None:
                stats["failed_batches"] += 1
                print("  no usable JSON — left at 'auto' for a retry")
                continue
            by_id = {r[0]: r for r in chunk}
            for v in verdicts:
                iid = v.get("incident_id")
                row = by_id.get(iid)
                if not row:
                    continue
                gc = row[6]
                stats["reviewed"] += 1
                action = ar.decide(v, gc)
                if action == "auto_published":
                    # taxonomy-locked: no real defect tag => not publishable as a defect
                    # dossier however good the verdict (migration 009 regression guard)
                    cur.execute("""select 1 from incident_defect where incident_id=%s
                                   and defect_type not in ('other_infrastructure',
                                       'no_infrastructure_defect_identified') limit 1""",
                                (iid,))
                    if cur.fetchone() is None:
                        action = None
                note_conf = v.get("confidence")
                # NOT-YET-GEOCODED GUARD. decide() reads `geocode_conf or 0`, so a NULL
                # (not yet geocoded) is indistinguishable from a bad pin and blocks
                # confirm_publish. Combined with adjudicate-once that PERMANENTLY demotes
                # a publishable record for a reason that has nothing to do with it — it
                # cost #2609 (0.80) and #2612 (0.88) before this guard existed. cmd_daily
                # never hits this because geocode runs before auto_review; a standalone
                # run can. Leave such items at 'auto' to be re-judged after geocoding.
                if action is None and gc is None and v.get("verdict") == "confirm_publish":
                    print(f"  #{iid} not geocoded yet — left at 'auto' "
                          f"(verdict {v.get('verdict')} {note_conf})")
                    stats["deferred_ungeocoded"] = stats.get("deferred_ungeocoded", 0) + 1
                    continue
                if action is None:
                    cur.execute("update incident set verification_status='needs_human', "
                                "updated_at=now() where id=%s", (iid,))
                    cur.execute("""insert into review_action (entity_type, entity_id,
                                   reviewer, action, after_json, note) values
                                   ('incident',%s,%s,'edit',%s::jsonb,%s)""",
                                (iid, ADJ_REVIEWER, json.dumps(v),
                                 f"auto-review(opus-local) -> needs_human "
                                 f"(conf {note_conf}): {v.get('reason','')[:150]}"))
                    conn.commit()
                    stats["left_for_human"] += 1
                    continue
                cur.execute("update incident set verification_status=%s, updated_at=now() "
                            "where id=%s", (action, iid))
                cur.execute("""insert into review_action (entity_type, entity_id, reviewer,
                               action, after_json, note) values ('incident',%s,%s,%s,
                               %s::jsonb,%s)""",
                            (iid, ADJ_REVIEWER,
                             "approve" if action == "auto_published" else
                             ("reject" if action == "rejected" else "edit"),
                             json.dumps(v),
                             f"auto-review(opus-local) -> {action} (conf {note_conf}): "
                             f"{v.get('reason','')[:150]}"))
                conn.commit()
                stats[action] += 1
                print(f"  #{iid} -> {action} ({note_conf}) {v.get('reason','')[:60]}")
            missing = set(by_id) - {v.get("incident_id") for v in verdicts}
            if missing:
                stats["unparsed"] += len(missing)
                print(f"  {len(missing)} absent from the reply, still 'auto'")
    print(f"\nDONE {stats}")


VERIFY_REVIEWER = "pipeline:blind_verify_opus_local"

VERIFY_SYSTEM = (
    "You are auditing a public road-safety registry used with the Indian government. "
    "You are shown ONLY a news article. Judge it from scratch — you are NOT being asked "
    "to agree with anything.\n"
    "Answer three things:\n"
    "1. in_scope: does this article report a specific road crash in India, or a specific "
    "road-infrastructure defect/hazard in India? Building/premises collapses, wells, "
    "lifts and electrocutions are NOT in scope unless the road itself is the hazard.\n"
    "2. infra_implicated: do the article's OWN WORDS attribute the crash or hazard, at "
    "least partly, to road infrastructure? Infrastructure merely present in the scene "
    "does not count — a car striking a divider does not implicate the divider.\n"
    "3. defect_types: which defect codes the article actually supports, from the list "
    "given. Empty if none.\n"
    "Be strict. This audit exists to find records that should never have been published."
)


def cmd_verify(args) -> None:
    """Blind second opinion on records this machine already published.

    Tonight's adjudication rejected 0 of 374 against the API path's 7.6% baseline,
    because Opus extracted these records AND then adjudicated them while being shown
    its own first-pass answer. That is not the independent check auto_review's design
    assumes. This pass removes the anchor: the model sees the article and nothing else,
    and its verdict is compared to the stored record in code rather than by self-report.

    Caveat worth keeping: the same model blind is still more correlated than a different
    model would be. This removes the anchoring effect, not the shared-prior effect."""
    from pipeline.db import connect
    _preflight()
    codes = ex.taxonomy_codes()
    stats = {"checked": 0, "agree": 0, "demoted_infra": 0, "demoted_scope": 0,
             "flagged_defects": 0, "failed_batches": 0, "unparsed": 0}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select distinct i.id, i.infra_implicated, a.clean_text
          from incident i
          join source_article a on a.id = i.primary_source_id
          join review_action ra on ra.entity_id = i.id and ra.entity_type='incident'
          where ra.reviewer = %s and ra.action = 'approve'
            and i.verification_status = 'auto_published' and a.clean_text is not null
          order by i.id limit %s""", (ADJ_REVIEWER, args.limit))
        rows = cur.fetchall()
        print(f"blind-verifying {len(rows)} self-approved record(s) · batch {args.batch}")
        if args.dry_run:
            print("dry run — nothing sent, nothing written")
            return
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            print(f"\n[batch {i // args.batch + 1}] ids {chunk[0][0]}–{chunk[-1][0]}")
            parts = [VERIFY_SYSTEM,
                     f"\nValid defect codes: {', '.join(codes)}",
                     "\nReturn a BARE JSON ARRAY, one object per article, nothing else: "
                     "{\"article_ref\": <int>, \"in_scope\": <bool>, "
                     "\"infra_implicated\": <bool>, \"defect_types\": [<code>...], "
                     "\"confidence\": <0-1>, \"reason\": \"<=25 words\"}"]
            for (iid, _, text) in chunk:
                parts.append(f"\n===== ARTICLE {iid} =====\n{(text or '')[:7000]}")
            verdicts = _extract_json_array((_claude("\n".join(parts)) or {}).get("result", ""))
            if verdicts is None:
                stats["failed_batches"] += 1
                print("  no usable JSON — left as-is")
                continue
            stored = {r[0]: r[1] for r in chunk}
            seen = set()
            for v in verdicts:
                iid = v.get("article_ref")
                if iid not in stored:
                    continue
                seen.add(iid)
                stats["checked"] += 1
                blind_scope = bool(v.get("in_scope"))
                blind_infra = bool(v.get("infra_implicated"))
                reason = (v.get("reason") or "")[:150]
                demote = None
                if not blind_scope:
                    demote, key = "blind pass judged it OUT OF SCOPE", "demoted_scope"
                elif stored[iid] and not blind_infra:
                    demote, key = ("blind pass found NO infrastructure attribution, "
                                   "record published as infra-implicated"), "demoted_infra"
                if demote:
                    cur.execute("update incident set verification_status='needs_human', "
                                "updated_at=now() where id=%s", (iid,))
                    cur.execute("""insert into review_action (entity_type, entity_id,
                                   reviewer, action, after_json, note) values
                                   ('incident',%s,%s,'edit',%s::jsonb,%s)""",
                                (iid, VERIFY_REVIEWER, json.dumps(v),
                                 f"blind verify DISAGREES: {demote}. {reason}"))
                    conn.commit()
                    stats[key] += 1
                    print(f"  #{iid} DEMOTED — {demote[:60]}")
                    continue
                stats["agree"] += 1
            missing = set(stored) - seen
            if missing:
                stats["unparsed"] += len(missing)
                print(f"  {len(missing)} absent from the reply, left as-is")
    print(f"\nDONE {stats}")
    if stats["checked"]:
        d = stats["demoted_infra"] + stats["demoted_scope"]
        print(f"disagreement rate: {d}/{stats['checked']} = {d/stats['checked']*100:.1f}% "
              f"(API adjudication rejects ~7.6% on comparable material)")


AUDIT_SYSTEM = (
    "You are auditing a public road-safety registry used with the Indian government. "
    "Each item below is one map location, with the news-derived records the pipeline "
    "clustered into it. You are shown ONLY those records — no scores, no flags, no "
    "prior verdict. Judge from scratch.\n"
    "For each item answer:\n"
    "1. same_location: do these records describe ONE specific place a road crew could "
    "be sent to? False if they are different places that merely fall near each other, "
    "or if the location is only a district or a whole highway.\n"
    "2. distinct_events: how many GENUINELY DIFFERENT events are here. Several outlets "
    "covering one crash, or one ongoing complaint reported repeatedly, is ONE event. "
    "Count events, not records.\n"
    "3. location_specific: is the location precise enough to act on (a named junction, "
    "a village stretch, a marked km point)? False for 'National Highway, <district>' or "
    "a bare road name spanning many kilometres.\n"
    "4. repeat_pattern_supported: do the records together evidence a place that "
    "REPEATEDLY causes harm, as opposed to a single report?\n"
    "Be strict. A government official acting on a false repeat-pattern claim is the "
    "failure this audit exists to prevent."
)


def cmd_audit_hotspots(args) -> None:
    """Blind audit of the map locations that claim a REPEAT PATTERN.

    Scoped deliberately. Whether an individual record is sound was already answered by
    --verify (181 of 182 held, with a negative control flagging 10/10 known-bad), so
    re-checking all 645 would mostly re-confirm it. What a per-record check CANNOT
    validate is a claim built from several records: that they are one place, that they
    are distinct events, and that together they show a repeat pattern. That claim is
    what an official acts on first, and the 1 Aug duplicate analysis found Mirganj-
    Samaur sitting at exactly the escalation threshold of 3 partly on duplicates.

    Reports; does not mutate. A hotspot is derived from its incidents, so it cannot be
    'demoted' directly — the finding tells you which incidents to look at."""
    from pipeline.db import connect
    _preflight()
    findings, stats = [], {"audited": 0, "not_one_place": 0, "vague_pin": 0,
                           "inflated_count": 0, "false_repeat": 0, "sound": 0,
                           "failed_batches": 0, "unparsed": 0}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select id, road_name, admin_district, admin_state, n_public_incidents,
                 escalation_candidate, priority_score, dominant_defects
          from public_hotspot
          where escalation_candidate or n_public_incidents >= %s
          order by escalation_candidate desc, n_public_incidents desc""",
                    (args.min_incidents,))
        spots = cur.fetchall()
        print(f"auditing {len(spots)} location(s) that claim a repeat pattern "
              f"· batch {args.batch}")
        if args.dry_run:
            print("dry run — nothing sent, nothing written")
            return
        detail = {}
        for s in spots:
            cur.execute("""
              select i.id, i.location_text_best, i.crash_date, i.fatalities, i.injuries,
                     i.narrative_summary, a.url
              from public_incident i join source_article a on a.id = i.primary_source_id
              where i.cluster_id = %s order by i.crash_date nulls last, i.id""", (s[0],))
            incs = cur.fetchall()
            rows = []
            for inc in incs:
                cur.execute("""select defect_type, evidence_snippet from incident_defect
                               where incident_id=%s""", (inc[0],))
                rows.append((inc, cur.fetchall()))
            detail[s[0]] = rows

        for i in range(0, len(spots), args.batch):
            chunk = spots[i:i + args.batch]
            print(f"\n[batch {i // args.batch + 1}] hotspots "
                  f"{chunk[0][0]}–{chunk[-1][0]}")
            parts = [AUDIT_SYSTEM,
                     "\nReturn a BARE JSON ARRAY, one object per item, nothing else: "
                     '{"hotspot_ref": <int>, "same_location": <bool>, '
                     '"distinct_events": <int>, "location_specific": <bool>, '
                     '"repeat_pattern_supported": <bool>, "confidence": <0-1>, '
                     '"reason": "<=30 words"}']
            for s in chunk:
                parts.append(f"\n===== ITEM {s[0]} =====")
                for (inc, defects) in detail[s[0]]:
                    _, loc, dt, f, inj, summ, url = inc
                    d = "; ".join(f"{c}: “{e[:120]}”" for c, e in defects) or "(none)"
                    parts.append(
                        f"  - date={dt or 'unknown'} deaths={f} injured={inj}\n"
                        f"    location: {loc}\n    summary: {summ}\n    evidence: {d}")
            verdicts = _extract_json_array(
                (_claude("\n".join(parts)) or {}).get("result", ""))
            if verdicts is None:
                stats["failed_batches"] += 1
                print("  no usable JSON — skipped")
                continue
            by_id = {s[0]: s for s in chunk}
            seen = set()
            for v in verdicts:
                hid = v.get("hotspot_ref")
                s = by_id.get(hid)
                if not s:
                    continue
                seen.add(hid)
                stats["audited"] += 1
                stored_n, escalated = s[4], s[5]
                blind_n = int(v.get("distinct_events") or 0)
                problems = []
                if not v.get("same_location"):
                    problems.append("not one place"); stats["not_one_place"] += 1
                if not v.get("location_specific"):
                    problems.append("pin too vague"); stats["vague_pin"] += 1
                if blind_n < stored_n:
                    problems.append(f"counts {stored_n} but only {blind_n} distinct "
                                    f"event(s)"); stats["inflated_count"] += 1
                if escalated and not v.get("repeat_pattern_supported"):
                    problems.append("ESCALATION FLAG UNSUPPORTED")
                    stats["false_repeat"] += 1
                if not problems:
                    stats["sound"] += 1
                findings.append({
                    "hotspot_id": hid, "road": s[1], "district": s[2], "state": s[3],
                    "stored_incidents": stored_n, "escalation_candidate": escalated,
                    "priority_score": float(s[6] or 0), "blind_distinct_events": blind_n,
                    "problems": problems, "reason": v.get("reason", ""),
                    "confidence": v.get("confidence")})
                if problems:
                    print(f"  #{hid} {(s[1] or '?')[:32]:<32} {'; '.join(problems)[:70]}")
            missing = set(by_id) - seen
            if missing:
                stats["unparsed"] += len(missing)
                print(f"  {len(missing)} absent from the reply")

    out = Path(args.out)
    out.write_text(json.dumps({"findings": findings, "stats": stats}, indent=2,
                              ensure_ascii=False))
    print(f"\nDONE {stats}")
    print(f"report: {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = every eligible article")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--all", action="store_true",
                    help="include the behaviour-crash tail (NOT recommended)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit-hotspots", action="store_true",
                    help="blind audit of locations claiming a repeat pattern")
    ap.add_argument("--min-incidents", type=int, default=3)
    ap.add_argument("--out", default="docs/HOTSPOT_AUDIT.json")
    ap.add_argument("--verify", action="store_true",
                    help="blind second opinion on self-approved records")
    ap.add_argument("--adjudicate", action="store_true",
                    help="second-pass review of the 'auto' queue "
                         "instead of extraction")
    args = ap.parse_args()
    if args.audit_hotspots:
        return cmd_audit_hotspots(args)
    if args.verify:
        return cmd_verify(args)
    if args.adjudicate:
        return cmd_adjudicate(args)

    defect_terms = [t for lang in configload.keywords().values()
                    for t in lang.get("infra_defect", [])]
    global _STORE
    _STORE = get_store()
    store = _STORE
    try:
        rows = store.articles_by_status("fetched", limit=100_000,
                                        priority_terms=defect_terms)
        if not args.all:
            rows = [a for a in rows
                    if any(t in (a.get("clean_text") or "") for t in defect_terms)]
        rows = [a for a in rows if (a.get("clean_text") or "").strip()
                and prefilter.passes(a["clean_text"])]
        if args.limit:
            rows = rows[:args.limit]
        print(f"eligible: {len(rows)} article(s) · batch {args.batch} "
              f"· {(len(rows) + args.batch - 1) // args.batch} invocation(s)")
        if args.dry_run:
            for a in rows[:5]:
                print(f"  #{a['id']}  {(a.get('clean_text') or '')[:90]!r}")
            print("dry run — nothing sent, nothing written")
            return
        if not rows:
            return

        _preflight()
        stats = {"in": 0, "irrelevant": 0, "incidents": 0, "machine_ok": 0,
                 "snippets_dropped": 0, "failed_batches": 0, "unparsed": 0}
        t0 = time.time()
        consecutive_failures = 0
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            n = i // args.batch + 1
            print(f"\n[batch {n}] {len(chunk)} article(s) "
                  f"(ids {chunk[0]['id']}–{chunk[-1]['id']})")
            env = _claude(build_prompt(chunk))
            results = _extract_json_array((env or {}).get("result", ""))
            if results is None:
                stats["failed_batches"] += 1
                consecutive_failures += 1
                print(f"  no usable JSON — left queued "
                      f"(consecutive failures: {consecutive_failures})")
                # Rate limit or dead auth: stop rather than churn the remaining
                # invocations. Everything is still 'fetched', so a rerun resumes.
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"  STOPPING after {consecutive_failures} consecutive failed "
                          f"batches — rerun to resume where this left off")
                    break
                continue
            consecutive_failures = 0
            by_id = {a["id"]: a for a in chunk}
            seen = set()
            for r in results:
                a = by_id.get(r.get("article_id"))
                if not a:
                    continue
                seen.add(a["id"])
                stats["in"] += 1
                if not r.get("in_scope") or not r.get("incident"):
                    _db(lambda s: s.set_article_status(a["id"], "irrelevant"))
                    stats["irrelevant"] += 1
                    continue
                inc, dropped = ex.validate_snippets(dict(r["incident"]),
                                                    a.get("clean_text") or "")
                stats["snippets_dropped"] += len(dropped)
                inc["location_text_raw"] = inc.get("location_text_best")
                inc["primary_source_id"] = a["id"]
                defects = inc.pop("defects", [])
                try:
                    iid = _db(lambda s: s.insert_incident(inc, defects, a["id"]))
                except Exception as e:
                    print(f"  WARN insert failed #{a['id']}: {e}")
                    try:
                        _db(lambda s: s.set_article_status(a["id"], "failed"))
                    except Exception as e2:
                        print(f"  could not even mark it failed: {e2}")
                    continue
                _db(lambda s: s.set_article_status(a["id"], "extracted"))
                stats["incidents"] += 1
                if not inc["infra_implicated"]:
                    _db(lambda s: s.set_incident_status(
                        iid, "machine_ok",
                        "crash-only at extraction (infra_implicated=false) -> "
                        "machine_ok; kept for crash-frequency counts"))
                    stats["machine_ok"] += 1
                print(f"  + incident #{iid} <- art #{a['id']} "
                      f"F{inc['fatalities']}/I{inc['injuries']} "
                      f"conf={inc['extraction_confidence']:.2f} "
                      f"infra={inc['infra_implicated']}")
            missing = set(by_id) - seen
            if missing:
                stats["unparsed"] += len(missing)
                print(f"  {len(missing)} article(s) absent from the reply, still queued")
            u = (env or {}).get("usage", {}) or {}
            print(f"  usage in/out {u.get('input_tokens')}/{u.get('output_tokens')} "
                  f"· cost ${(env or {}).get('total_cost_usd')}")
        print(f"\nDONE in {time.time() - t0:.0f}s {stats}")
        print("Nothing is public yet — auto_review still adjudicates on the next run.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
