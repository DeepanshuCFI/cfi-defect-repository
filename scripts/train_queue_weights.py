"""Train the reading queue's weight table from our own verdicts (plan item 1.2).

    python -m scripts.train_queue_weights --since 2026-09-20            # report only
    python -m scripts.train_queue_weights --since 2026-09-20 --write    # + config/queue_weights.json

Read-only on the database. No model calls. Run monthly (health alerts when the table
is older than `health.queue_weights_max_age_days`) and commit the new table through a
pull request like any other change to what the pipeline reads.

WHAT IT LEARNS FROM
Every article a model has judged: 'extracted' (label = did the article's own words
implicate the road, i.e. incident.infra_implicated) and 'irrelevant' articles that pass
the free prefilter (label 0; the ones failing it were never shown to a model). Split by
time — the oldest three quarters train, the newest quarter tests — because the table
will always be used on articles newer than the ones it was fitted to.

WHAT IT PRODUCES
A list of at most --max-terms terms with weights. Candidates are words and word pairs
from the training text plus every infra_defect keyword; an L1-penalised logistic
regression on term PRESENCE picks the few that carry the signal, and the survivors are
refitted so the weights are not shrunk. Presence is a case-sensitive substring test on
the whole text, the same test Postgres applies at read time (queue_score.SCORE_SQL).

Place names are excluded on purpose (district and state names, and anything in
--exclude): a table that learns "Hardoi" is learning where we happened to find reports
last month, and would starve every other district.

KNOWN LIMIT, STATED: until the random slice has run for a few weeks, most verdicts come
from articles the old vocabulary-first rule chose to read, so the table is judged on a
population tilted toward defect vocabulary. The report gives precision inside and
outside that vocabulary separately for this reason.
"""
import argparse
import csv
import json
import math
import pickle
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.settings import CONFIG_DIR  # noqa: E402

_SPLIT = re.compile(r"[\s,.;:!?()\[\]{}\"'“”‘’|/\\<>—–\-।•·…*#=+_~`@^&%$]+")


def tokens(text: str) -> list[str]:
    """Whitespace/punctuation split. Not \\w: Devanagari vowel signs are combining
    marks, and \\w would cut every Hindi word at its first matra."""
    return [t for t in _SPLIT.split(text or "") if t]


def candidate_terms(texts: list[str], labels: list[int], *, min_df: int, max_df_share: float,
                    keep: int, banned: set[str], always: list[str]) -> list[str]:
    """Words and adjacent word pairs worth offering to the regression. Pure.

    Ranked by how far a term's presence moves the odds of an infra-implicated article,
    weighted by how often it appears, so a rare perfect predictor does not crowd out a
    common good one."""
    n = len(texts)
    df, df_pos = Counter(), Counter()
    for text, y in zip(texts, labels):
        toks = tokens(text)
        seen = set(toks)
        seen.update(f"{a} {b}" for a, b in zip(toks, toks[1:]))
        for t in seen:
            df[t] += 1
            if y:
                df_pos[t] += 1
    pos = sum(labels)
    base = math.log((pos + 1) / (n - pos + 1))
    scored = []
    for t, d in df.items():
        if d < min_df or d > max_df_share * n:
            continue
        if len(t) < 3 or t.isdigit() or any(part.lower() in banned for part in t.split(" ")):
            continue
        p = df_pos[t]
        lift = math.log((p + 1) / (d - p + 1)) - base
        scored.append((abs(lift) * math.sqrt(d), t))
    scored.sort(reverse=True)
    out = [t for _, t in scored[:keep]]
    have = set(out)
    for t in always:                          # the keyword pack always gets a hearing
        if t not in have and not any(part.lower() in banned for part in t.split(" ")):
            out.append(t)
            have.add(t)
    return out


def presence(texts: list[str], terms: list[str]):
    """Sparse 0/1 matrix: does text i contain term j (case-sensitive substring)?"""
    from scipy.sparse import csr_matrix
    rows, cols = [], []
    for i, text in enumerate(texts):
        for j, t in enumerate(terms):
            if t in text:
                rows.append(i)
                cols.append(j)
    return csr_matrix(([1.0] * len(rows), (rows, cols)), shape=(len(texts), len(terms)))


def drop_nested(terms: list[str], coefs: list[float]) -> list[int]:
    """Indices to keep. Presence is a substring test, so a kept term that contains
    another kept term would count the shorter one twice on every match; when both
    survive with the same sign, the longer one goes. Pure."""
    keep = []
    order = sorted(range(len(terms)), key=lambda i: len(terms[i]))
    for i in order:
        if any(terms[k] in terms[i] and (coefs[k] > 0) == (coefs[i] > 0) for k in keep):
            continue
        keep.append(i)
    return sorted(keep)


def fit_table(texts, labels, terms, max_terms: int):
    """L1 picks at most max_terms terms; an L2 refit on the survivors sets the weights.

    The only part that needs scikit-learn and scipy (requirements-train.txt). The daily
    pipeline never imports them: it reads the finished table."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    X = presence(texts, terms)
    y = np.array(labels)
    chosen = None
    for C in (0.3, 0.2, 0.12, 0.08, 0.05, 0.035, 0.025, 0.018, 0.012, 0.008, 0.005, 0.003):
        m = LogisticRegression(l1_ratio=1.0, C=C, solver="liblinear",
                               class_weight="balanced", max_iter=400)
        m.fit(X, y)
        nz = [i for i, c in enumerate(m.coef_[0]) if abs(c) > 1e-6]
        nz = [nz[k] for k in drop_nested([terms[i] for i in nz], [m.coef_[0][i] for i in nz])]
        if len(nz) <= max_terms:
            chosen = nz
            break
    if not chosen:
        raise SystemExit("no regularisation strength left a usable table")
    sel = [terms[i] for i in chosen]
    m2 = LogisticRegression(C=1.0, class_weight="balanced", max_iter=1000)
    m2.fit(X[:, chosen], y)
    table = {t: round(float(w), 2) for t, w in zip(sel, m2.coef_[0]) if abs(w) >= 0.05}
    return table, float(m2.intercept_[0])


def score_all(texts, table: dict) -> list[float]:
    items = list(table.items())
    return [sum(w for t, w in items if t in text) for text in texts]


def precision_at(scores, labels, share: float, reps: int = 50) -> tuple[float, float]:
    """(precision, recall) in the top `share`, ties broken at random and averaged —
    the old rule has only two score values, so a fixed tie order would flatter or
    cheat it. Pure Python: the evaluation must run without the training extras."""
    import random
    n = len(labels)
    k = max(1, int(round(n * share)))
    rng = random.Random(0)
    hits = []
    for _ in range(reps if len(set(scores)) < 50 else 1):
        order = sorted(range(n), key=lambda i: (-scores[i], rng.random()))
        hits.append(sum(labels[i] for i in order[:k]))
    h = sum(hits) / len(hits)
    return round(h / k, 3), round(h / max(1, sum(labels)), 3)


def auc(scores, labels) -> float | None:
    """Area under the ROC curve by the rank-sum identity, ties given their average
    rank. None when one class is absent. Pure Python."""
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    rank_sum, i = 0.0, 0
    while i < len(order):
        j = i
        while j < len(order) and scores[order[j]] == scores[order[i]]:
            j += 1
        avg_rank = (i + j + 1) / 2                      # ranks are 1-based
        rank_sum += avg_rank * sum(labels[order[k]] for k in range(i, j))
        i = j
    return round((rank_sum - pos * (pos + 1) / 2) / (pos * neg), 3)


def likely_threshold(scores, labels, min_precision: float = 0.8) -> tuple[float, float, float]:
    """The lowest score at which everything at or above it is still, taken together,
    at least `min_precision` infra-implicated. Returns (threshold, precision, recall).
    health.extraction_lag watches the oldest unread article at or above this line."""
    pairs = sorted(zip(scores, labels), reverse=True)
    total = max(1, sum(labels))
    best, hit, i = None, 0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:   # whole tie group at once
            hit += pairs[j][1]
            j += 1
        if hit / j >= min_precision:
            best = (round(pairs[i][0], 2), round(hit / j, 3), round(hit / total, 3))
        i = j
    if best is None:
        raise SystemExit(f"no threshold reaches precision {min_precision}")
    return best


def load_rows(since: str, cache: str | None) -> list[dict]:
    if cache and Path(cache).exists():
        return pickle.load(open(cache, "rb"))
    from pipeline.db import connect
    from pipeline.processing import prefilter
    sql = """select a.id, a.clean_text, a.processing_status, a.created_at::date::text,
                    (select bool_or(i.infra_implicated) from incident_source s
                       join incident i on i.id = s.incident_id
                      where s.source_article_id = a.id)
             from source_article a
             where a.id > %s and a.created_at >= %s and a.clean_text is not null
               and a.processing_status in ('extracted', 'irrelevant')
             order by a.id limit 1500"""
    rows, last = [], 0
    with connect() as conn:
        while True:
            with conn.cursor() as cur:
                cur.execute(sql, (last, since))
                page = cur.fetchall()
            if not page:
                break
            last = page[-1][0]
            for aid, text, status, day, infra in page:
                if status == "irrelevant" and not prefilter.passes(text):
                    continue              # the free filter retired it; no model saw it
                rows.append({"id": aid, "txt": text, "status": status, "day": day,
                             "infra": int(bool(infra)) if status == "extracted" else 0})
    if cache:
        pickle.dump(rows, open(cache, "wb"))
    return rows


def banned_place_names(extra: list[str]) -> set[str]:
    banned = {e.strip().lower() for e in extra if e.strip()}
    with open(CONFIG_DIR / "config_districts_master.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for key in ("district", "state"):
                for part in tokens(row.get(key) or ""):
                    if len(part) >= 4:
                        banned.add(part.lower())
    return banned


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", required=True, help="first created_at day to learn from, YYYY-MM-DD")
    ap.add_argument("--cache", help="pickle path: read rows from it if present, else save them")
    ap.add_argument("--max-terms", type=int, default=60)
    ap.add_argument("--test-share", type=float, default=0.25)
    ap.add_argument("--exclude", default="", help="comma-separated extra terms never to learn")
    ap.add_argument("--likely-precision", type=float, default=0.8,
                    help="precision that defines a 'likely defect report' (the lag alert's class)")
    ap.add_argument("--write", action="store_true", help="write config/queue_weights.json")
    ap.add_argument("--report", help="also write the full report JSON here")
    args = ap.parse_args()

    rows = sorted(load_rows(args.since, args.cache), key=lambda r: r["id"])
    cut = int(len(rows) * (1 - args.test_share))
    tr, te = rows[:cut], rows[cut:]
    kw = json.load(open(CONFIG_DIR / "config_keyword_pack.json", encoding="utf-8"))
    kw = kw.get("languages", kw)
    vocab = sorted({t for lang in kw.values() if isinstance(lang, dict)
                    for t in lang.get("infra_defect", [])})
    banned = banned_place_names(args.exclude.split(","))
    print(f"{len(rows)} judged articles ({rows[0]['day']} to {rows[-1]['day']}): "
          f"train {len(tr)}, test {len(te)} (from {te[0]['day']}); "
          f"infra share train {sum(r['infra'] for r in tr)/len(tr):.3f}, "
          f"test {sum(r['infra'] for r in te)/len(te):.3f}")

    cands = candidate_terms([r["txt"] for r in tr], [r["infra"] for r in tr],
                            min_df=max(40, len(tr) // 300), max_df_share=0.35,
                            keep=1200, banned=banned, always=vocab)
    table, bias = fit_table([r["txt"] for r in tr], [r["infra"] for r in tr], cands, args.max_terms)

    y = [r["infra"] for r in te]
    s_new = score_all([r["txt"] for r in te], table)
    in_vocab = [any(t in r["txt"] for t in vocab) for r in te]
    s_rule = [1.0 if v else 0.0 for v in in_vocab]
    report = {"rows": len(rows), "train": len(tr), "test": len(te),
              "from": rows[0]["day"], "to": rows[-1]["day"], "test_from": te[0]["day"],
              "base_rate_test": round(sum(y) / len(y), 3), "terms": len(table)}
    for name, s in (("rule", s_rule), ("table", s_new)):
        report[name] = {"auc": auc(s, y)}
        for share in (0.1, 0.2, 0.3):
            p, r = precision_at(s, y, share)
            report[name][f"top_{int(share*100)}pct"] = {"precision": p, "recall": r}
    for label, mask in (("inside_vocabulary", in_vocab), ("outside_vocabulary", [not v for v in in_vocab])):
        ys = [a for a, m in zip(y, mask) if m]
        ss = [a for a, m in zip(s_new, mask) if m]
        report[label] = {"n": len(ys), "infra": sum(ys),
                         "auc": auc(ss, ys)}
    thr, thr_p, thr_r = likely_threshold(s_new, y, args.likely_precision)
    report["thresholds"] = {}
    for bar in (0.9, 0.8, 0.7, 0.6):
        try:
            t, tp, trc = likely_threshold(s_new, y, bar)
        except SystemExit:
            continue
        report["thresholds"][str(bar)] = {
            "score": t, "precision": tp, "recall": trc,
            "share_of_test_above": round(sum(1 for v in s_new if v >= t) / len(te), 3)}
    report["likely_threshold"] = {"score": thr, "precision": thr_p, "recall": thr_r,
                                  "share_of_test_above": round(sum(1 for v in s_new if v >= thr) / len(te), 3)}
    print(json.dumps(report, indent=1))
    print("\nweight  term")
    for t, w in sorted(table.items(), key=lambda kv: -kv[1]):
        print(f"{w:+6.2f}  {t}")

    out = {"_comment": ("Reading-queue weight table. Trained by scripts/train_queue_weights.py "
                        "on our own verdicts; do not edit by hand — retrain and review the diff."),
           "trained_on": date.today().isoformat(),
           "training_rows": len(tr), "rows_from": rows[0]["day"], "rows_to": rows[-1]["day"],
           "test": {k: report[k] for k in ("test", "test_from", "base_rate_test", "rule", "table")},
           "bias": round(bias, 3), "likely_threshold": thr,
           "terms": dict(sorted(table.items(), key=lambda kv: -kv[1]))}
    if args.report:
        Path(args.report).write_text(json.dumps({"report": report, "table": out},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    if args.write:
        from pipeline.processing import queue_score
        queue_score.parse_weights(out)                    # refuse to write a table we would refuse to load
        (CONFIG_DIR / queue_score.WEIGHTS_FILE).write_text(
            json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"\nwrote {CONFIG_DIR / queue_score.WEIGHTS_FILE}")


if __name__ == "__main__":
    main()
