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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = every eligible article")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--all", action="store_true",
                    help="include the behaviour-crash tail (NOT recommended)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

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
