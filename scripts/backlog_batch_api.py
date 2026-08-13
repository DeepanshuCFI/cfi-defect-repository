#!/usr/bin/env python3
"""One-off backlog burn: clear the 'fetched' pile via the Message Batches API.

The daily pipeline drains ~200 articles/day (the CLI backend's ceiling) while
~700-1,400 arrive; by 3 Aug 2026 the fetched pile stood at ~9,000 and growing.
Batches process the same requests at 50% off and finish within 24h — measured
economics (1 Aug, count_tokens on a real sample) put the full clear at ~$30-40,
approved by the owner on 3 Aug. This is a one-off drain, not a pipeline stage:
the daily run keeps its own economics.

Mirrors cmd_process EXACTLY — same prefilter, same relevance SYSTEM/TOOL, same
tiered extraction (light Haiku for pure-behaviour crashes, strong model for
infra-implicated), same status transitions and insert path — so a batch-processed
article is indistinguishable from a pipeline-processed one.

Stages (resumable; state in data/batch_backlog_state.json, gitignored):
    submit-relevance [--limit N]   free prefilter locally, then one relevance batch
    status                         poll the pending batch
    apply-relevance                mark irrelevant / stage in-scope for extraction
    submit-extraction              extraction batch (light/full per relevance kind)
    apply-extraction               validate snippets, insert incidents, set statuses

Concurrency guard: every apply re-checks the article is still 'fetched' before
writing — if the daily run got there first, the batch result is discarded. The
two paths can therefore overlap a cron window safely, at worst wasting a few
already-discounted calls.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline import configload  # noqa: E402
from pipeline.processing import extract as ex  # noqa: E402
from pipeline.processing import prefilter  # noqa: E402
from pipeline.processing import relevance as rel  # noqa: E402
from pipeline.store import get_store  # noqa: E402

STATE_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "batch_backlog_state.json"

# Measured 1 Aug 2026 (count_tokens on a real backlog sample), halved for batch.
EST_RELEVANCE_USD = 0.0022 / 2
EST_LIGHT_USD = 0.0064 / 2
EST_FULL_USD = 0.0127 / 2


class ResilientStore:
    """Retry-once-on-fresh-connection around the store.

    The Supabase pooler kills idle connections, and psycopg's .closed stays False
    until the next use fails — the exact trap that 500'd the review console
    (a55d299). This script idles its DB connection for minutes at a time while
    batch results stream from Anthropic, so the first store call after a quiet
    spell dies. Every operation here is an idempotent single statement, so one
    retry on a rebuilt connection is safe.
    """

    def __init__(self):
        self._store = get_store()

    def __getattr__(self, name):
        def call(*a, **kw):
            import psycopg
            try:
                return getattr(self._store, name)(*a, **kw)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # psycopg reports a dead connection as OperationalError OR
                # InterfaceError ("the connection is lost") depending on where in
                # the statement lifecycle it died — catch both. A connection that
                # dies before commit returns rolls the transaction back server-side,
                # so re-running the whole call on a fresh connection is safe.
                try:
                    self._store.close()
                except Exception:
                    pass
                self._store = get_store()
                return getattr(self._store, name)(*a, **kw)
        return call


def _client():
    import anthropic
    from pipeline.settings import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY or "REPLACE_ME" in ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY missing from .env")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def _title(text: str) -> str:
    return text.split("\n", 1)[0][:140]


def _model(key: str) -> str:
    # Deliberately NOT llm.model_for: that consults LLM_BACKEND, and this script is
    # always the metered API path regardless of what the environment says.
    models = configload.settings()["models"]
    return models.get(key, models["extraction"])


def cmd_submit_relevance(args) -> None:
    store = get_store()
    state = _load_state()
    if state.get("relevance_batch_id") and not args.force:
        sys.exit(f"relevance batch already submitted ({state['relevance_batch_id']}); "
                 "run 'status' / 'apply-relevance', or pass --force to start over")
    proc_cfg = configload.settings().get("processing", {})
    defect_terms = [t for lang in configload.keywords().values()
                    for t in lang.get("infra_defect", [])]
    arts = store.articles_by_status("fetched", limit=args.limit or 100_000,
                                    priority_terms=defect_terms)
    print(f"loaded {len(arts)} fetched article(s)")

    requests, prefiltered, empty = [], 0, 0
    for a in arts:
        text = a.get("clean_text") or ""
        if not text.strip():
            store.set_article_status(a["id"], "failed")
            empty += 1
            continue
        if proc_cfg.get("body_prefilter", True) and not prefilter.passes(text):
            store.set_article_status(a["id"], "irrelevant")
            prefiltered += 1
            continue                              # zero tokens spent — same as cmd_process
        content = f"TITLE: {_title(text)}\n\nTEXT:\n{text[:2500]}"
        requests.append({
            "custom_id": f"rel-{a['id']}",
            "params": {
                "model": _model("relevance"),
                "max_tokens": 200,
                "system": rel.SYSTEM,
                "tools": [rel.TOOL],
                "tool_choice": {"type": "tool", "name": rel.TOOL["name"]},
                "messages": [{"role": "user", "content": content}],
            },
        })
    store.close()

    est = len(requests) * EST_RELEVANCE_USD
    print(f"prefilter killed {prefiltered} free; {empty} empty -> failed; "
          f"{len(requests)} relevance calls to submit (~${est:.2f} at batch rates)")
    if not requests:
        sys.exit("nothing to submit")

    client = _client()
    batch = client.messages.batches.create(requests=requests)
    # Fresh state, not update(): a new relevance round invalidates everything
    # downstream (applied flags, in_scope, any extraction batch). Merging left the
    # previous round's relevance_applied=True in place, which made `status` skip the
    # new batch and print nothing (found 13 Aug, round 2 submit).
    _save_state({"relevance_batch_id": batch.id,
                 "relevance_submitted": len(requests)})
    print(f"SUBMITTED relevance batch {batch.id} ({batch.processing_status}); "
          f"poll with: python3 scripts/backlog_batch_api.py status")


def _batch_status(client, batch_id: str):
    b = client.messages.batches.retrieve(batch_id)
    c = b.request_counts
    print(f"{batch_id}: {b.processing_status} — processing {c.processing}, "
          f"succeeded {c.succeeded}, errored {c.errored}, "
          f"canceled {c.canceled}, expired {c.expired}")
    return b


def cmd_status(args) -> None:
    state = _load_state()
    client = _client()
    for key in ("relevance_batch_id", "extraction_batch_id"):
        if state.get(key) and not state.get(key.replace("_batch_id", "_applied")):
            _batch_status(client, state[key])


def cmd_apply_relevance(args) -> None:
    state = _load_state()
    bid = state.get("relevance_batch_id") or sys.exit("no relevance batch in state")
    client = _client()
    if _batch_status(client, bid).processing_status != "ended":
        sys.exit("batch not finished — try again later")

    # Drain ALL results into memory before touching the DB. The first version
    # interleaved two Tokyo round trips per result with the Anthropic stream —
    # ~36 minutes of pure RTT for 6,543 results, and a single mid-stream network
    # stall (Operation timed out, on the retry too) killed the whole apply.
    verdicts: dict[int, dict] = {}
    errored = 0
    for result in client.messages.batches.results(bid):
        aid = int(result.custom_id.removeprefix("rel-"))
        cls = None
        if result.result.type == "succeeded":
            cls = next((b.input for b in result.result.message.content
                        if b.type == "tool_use"), None)
        if cls is None:
            errored += 1
            continue
        verdicts[aid] = cls
    print(f"drained {len(verdicts)} verdict(s), {errored} errored")

    store = ResilientStore()
    try:
        # One bulk read decides scope; the guarded bulk write IS the concurrency
        # check — an article the daily run already processed no longer has
        # status 'fetched' and is left alone by the WHERE clause.
        statuses = store.article_statuses(list(verdicts))
        out_ids = [a for a, c in verdicts.items()
                   if not c.get("in_scope") and statuses.get(a) == "fetched"]
        in_scope = {str(a): (c.get("kind") or "both") for a, c in verdicts.items()
                    if c.get("in_scope") and statuses.get(a) == "fetched"}
        skipped = len(verdicts) - len(out_ids) - len(in_scope)
        n = store.set_articles_status(out_ids, "irrelevant", only_if="fetched")
    finally:
        store.close()
    state.update({"relevance_applied": True, "in_scope": in_scope})
    _save_state(state)
    print(f"DONE irrelevant={n} in_scope={len(in_scope)} "
          f"skipped_status_changed={skipped} errored={errored}")
    print("next: python3 scripts/backlog_batch_api.py submit-extraction")


def cmd_submit_extraction(args) -> None:
    state = _load_state()
    in_scope = state.get("in_scope") or sys.exit("run apply-relevance first")
    if state.get("extraction_batch_id") and not args.force:
        sys.exit(f"extraction batch already submitted ({state['extraction_batch_id']})")
    proc_cfg = configload.settings().get("processing", {})
    tiered = proc_cfg.get("tiered_extraction", True)

    store = ResilientStore()
    requests, n_light = [], 0
    try:
        arts = {a["id"]: a for a in store.get_articles([int(s) for s in in_scope])}
        for aid_s, kind in in_scope.items():
            a = arts.get(int(aid_s))
            if not a or a.get("processing_status") != "fetched":
                continue
            text = a.get("clean_text") or ""
            light = (kind == "crash") and tiered
            n_light += light
            pub = a.get("published_at")
            pub_line = f"ARTICLE PUBLISHED: {pub}\n" if pub else ""
            content = (f"{pub_line}TITLE: {_title(text)}\n\nARTICLE:\n{text[:7000]}\n\n"
                       "Resolve relative dates (e.g. 'on Friday', 'yesterday', weekday "
                       "names with no year) against the published date above. If the "
                       "article clearly reports an OLD crash (an anniversary/"
                       "retrospective), keep the old date. If no date is derivable, "
                       "use null.")
            tool = ex.build_tool()
            requests.append({
                "custom_id": f"ext-{aid_s}-{'l' if light else 'f'}",
                "params": {
                    "model": _model("extraction_light" if light else "extraction"),
                    "max_tokens": 2000,
                    "system": ex.SYSTEM,
                    "tools": [tool],
                    "tool_choice": {"type": "tool", "name": tool["name"]},
                    "messages": [{"role": "user", "content": content}],
                },
            })
    finally:
        store.close()

    est = n_light * EST_LIGHT_USD + (len(requests) - n_light) * EST_FULL_USD
    print(f"{len(requests)} extractions ({n_light} light, {len(requests)-n_light} full), "
          f"~${est:.2f} at batch rates")
    if not requests:
        sys.exit("nothing to extract")
    client = _client()
    batch = client.messages.batches.create(requests=requests)
    # Same reason as the relevance submit: a resubmit must clear its round's
    # applied flag or `status` goes silent on the new batch.
    state.update({"extraction_batch_id": batch.id,
                  "extraction_submitted": len(requests)})
    state.pop("extraction_applied", None)
    _save_state(state)
    print(f"SUBMITTED extraction batch {batch.id}")


def cmd_apply_extraction(args) -> None:
    state = _load_state()
    bid = state.get("extraction_batch_id") or sys.exit("no extraction batch in state")
    client = _client()
    if _batch_status(client, bid).processing_status != "ended":
        sys.exit("batch not finished — try again later")

    # Drain results first (same rationale as apply-relevance), then one bulk
    # article fetch; only the inserts are per-row — they are multi-statement
    # writes and must stay individual.
    drained: list[tuple[int, bool, dict]] = []
    stats = {"extracted": 0, "extracted_light": 0, "machine_ok": 0, "failed": 0,
             "skipped_status_changed": 0, "errored": 0, "snippets_dropped": 0}
    for result in client.messages.batches.results(bid):
        aid_s, flag = result.custom_id.removeprefix("ext-").rsplit("-", 1)
        raw = None
        if result.result.type == "succeeded":
            raw = next((b.input for b in result.result.message.content
                        if b.type == "tool_use"), None)
        if raw is None:
            stats["errored"] += 1                 # stays 'fetched' — daily run retries
            continue
        drained.append((int(aid_s), flag == "l", dict(raw)))
    print(f"drained {len(drained)} extraction(s), {stats['errored']} errored")

    store = ResilientStore()
    try:
        arts = {a["id"]: a for a in store.get_articles([d[0] for d in drained])}
        for aid, light, raw in drained:
            a = arts.get(aid)
            if not a or a.get("processing_status") != "fetched":
                stats["skipped_status_changed"] += 1
                continue
            text = a.get("clean_text") or ""
            try:
                inc, dropped = ex.validate_snippets(dict(raw), text)
            except Exception as e:
                # Malformed model output (e.g. defects as strings, not objects).
                # cmd_process marks these 'failed' and moves on — same here; one
                # bad extraction must never kill the remaining thousands.
                print(f"  WARN malformed extraction #{aid}: {e}")
                store.set_article_status(aid, "failed")
                stats["failed"] += 1
                continue
            stats["snippets_dropped"] += len(dropped)
            inc["location_text_raw"] = inc.get("location_text_best")
            inc["primary_source_id"] = aid
            defects = inc.pop("defects", [])
            try:
                iid = store.insert_incident(inc, defects, aid)
            except Exception as e:
                print(f"  WARN insert failed #{aid}: {e}")
                store.set_article_status(aid, "failed")
                stats["failed"] += 1
                continue
            store.set_article_status(aid, "extracted")
            # .get, not [] — 1 of 4,311 batch results omitted the key despite the
            # forced tool schema. Missing = crash-only (fail-closed: machine_ok can
            # never publish; treating it as infra-implicated could).
            if not inc.get("infra_implicated"):
                store.set_incident_status(iid, "machine_ok",
                    "crash-only at extraction (infra_implicated=false) -> machine_ok; "
                    "kept for crash-frequency counts")
                stats["machine_ok"] += 1
            stats["extracted_light" if light else "extracted"] += 1
    finally:
        store.close()
    state["extraction_applied"] = True
    _save_state(state)
    print(f"DONE {stats}")
    print("New incidents carry NULL geocode — the daily run's geocode + auto_review "
          "stages pick them up, or run `python3 -m pipeline.run geocode` now.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit-relevance")
    s.add_argument("--limit", type=int, default=0, help="0 = whole queue")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_submit_relevance)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("apply-relevance").set_defaults(func=cmd_apply_relevance)
    s = sub.add_parser("submit-extraction")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_submit_extraction)
    sub.add_parser("apply-extraction").set_defaults(func=cmd_apply_extraction)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
