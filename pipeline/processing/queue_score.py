"""Score-ordered reading queue (plan item 1.2).

The daily allowance reads a few hundred articles; about 840 arrive. Which ones get
read is therefore the largest lever on what the registry learns. Until Nov 2026 the
order was a two-class rule — any defect-vocabulary term first, then by age — and one
article in three it put at the head of the line turned out not to report a road
defect.

This module replaces the rule with a weight table: a short list of terms, each with a
weight learned from the verdicts our own pipeline has already recorded. An article's
score is the sum of the weights of the terms its text contains. It is deliberately not
a model file: the table is JSON in config/, a person can read every line of it, and
the same sum runs in Postgres (strpos) and in Python (`in`) with identical results, so
the queue is ranked across its WHOLE length in one query and nothing has to be stored
per article or recomputed when the table is retrained.

Training: scripts/train_queue_weights.py. Matching is case-sensitive substring on the
full clean_text, exactly as trained.

THE RANDOM SLICE
A queue that only ever reads what it already scores highly can never learn that it is
wrong about the rest: the verdicts it trains on next month would all come from its own
top picks. So a fixed share of every run's reads is drawn at random from the whole
queue, interleaved through the run (not appended at the end, where a budget stop would
drop it). Those reads are the unbiased sample the next table is trained and judged on.
"""
import json
from functools import lru_cache
from pathlib import Path

from pipeline.settings import CONFIG_DIR

WEIGHTS_FILE = "queue_weights.json"
DEFAULT_RANDOM_SHARE = 0.08


def parse_weights(raw: dict) -> dict:
    """Validate a weight table and return {terms, weights, threshold, meta}. Pure.

    Raises ValueError on anything that would make the ranking silently wrong — an
    empty table, a non-numeric weight, a duplicate or blank term."""
    table = raw.get("terms")
    if not isinstance(table, dict) or not table:
        raise ValueError("queue weights: 'terms' must be a non-empty object")
    terms, weights = [], []
    for term, w in table.items():
        if not isinstance(term, str) or not term.strip():
            raise ValueError("queue weights: blank term")
        if isinstance(w, bool) or not isinstance(w, (int, float)):
            raise ValueError(f"queue weights: weight for {term!r} is not a number")
        terms.append(term)
        weights.append(float(w))
    thr = raw.get("likely_threshold")
    if isinstance(thr, bool) or not isinstance(thr, (int, float)):
        raise ValueError("queue weights: 'likely_threshold' must be a number")
    return {"terms": terms, "weights": weights, "threshold": float(thr),
            "meta": {k: v for k, v in raw.items() if k != "terms"}}


@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict | None:
    """The weight table from config/, or None when there is none (the caller then
    falls back to the vocabulary-first rule). A table that exists but is malformed
    raises: ranking the queue on a broken table is worse than not ranking it."""
    p = Path(path) if path else CONFIG_DIR / WEIGHTS_FILE
    if not p.exists():
        return None
    return parse_weights(json.loads(p.read_text(encoding="utf-8")))


def score(text: str | None, table: dict) -> float:
    """Sum of the weights of the terms present. The Python twin of SCORE_SQL."""
    hay = text or ""
    return sum(w for t, w in zip(table["terms"], table["weights"]) if t in hay)


# One expression, used by the queue query and by health's lag check, so the line a
# report waits in and the alert that watches the line cannot disagree. Parameters:
# (terms text[], weights float8[]).
SCORE_SQL = ("(select coalesce(sum(t.w), 0) from unnest(%s::text[], %s::float8[]) "
             "as t(term, w) where strpos(clean_text, t.term) > 0)")


def interleave(ranked: list, random_rows: list, share: float) -> tuple[list, set]:
    """Merge the random slice into the ranked list at an even spacing. Pure.

    Every `round(1/share)`-th read is a random one, starting part-way into the first
    stride so the very first reads of a run are the top-ranked ones. Rows already in
    the ranked list are skipped (by id) — a random pick that the ranking would have
    read anyway is still counted as ranked. Returns (order, ids_from_the_random_slice).
    """
    if share <= 0 or not random_rows:
        return list(ranked), set()
    stride = max(2, round(1 / share))
    ranked_ids = {r["id"] for r in ranked}
    extras = [r for r in random_rows if r["id"] not in ranked_ids]
    out, picked, k = [], set(), 0
    for i, row in enumerate(ranked):
        if i % stride == stride // 2 and k < len(extras):
            out.append(extras[k])
            picked.add(extras[k]["id"])
            k += 1
        out.append(row)
    return out, picked


def random_share(proc_cfg: dict) -> float:
    """processing.queue_random_share, clamped to [0, 0.5]. Pure."""
    try:
        v = float(proc_cfg.get("queue_random_share", DEFAULT_RANDOM_SHARE))
    except (TypeError, ValueError):
        v = DEFAULT_RANDOM_SHARE
    return min(0.5, max(0.0, v))
