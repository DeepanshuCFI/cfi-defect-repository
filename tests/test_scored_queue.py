"""Score-ordered reading queue (plan item 1.2).

What these hold: the score is the same number in Python as the SQL expression is built
to compute; the ranking covers the whole queue, not a window of it; the random slice is
spread through a run rather than left where a budget stop would drop it; the committed
table is one a person can read and contains no place names; and the lag alert watches
the same line the queue reads from.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from pipeline.processing import health
from pipeline.processing import queue_score as qs
from pipeline.settings import CONFIG_DIR
from scripts import train_queue_weights as tw

TABLE = qs.parse_weights({"likely_threshold": 1.0, "trained_on": "2026-11-18",
                          "terms": {"गड्ढे": 2.0, "मरम्मत": 0.5, "टक्कर": -1.0}})


# ----------------------------------------------------------------- the table
def test_score_is_the_sum_of_the_weights_of_the_terms_present():
    assert qs.score("सड़क पर गड्ढे, मरम्मत की मांग", TABLE) == 2.5
    assert qs.score("बाइक की टक्कर", TABLE) == -1.0
    assert qs.score("गड्ढे गड्ढे गड्ढे", TABLE) == 2.0, "presence, not frequency"
    assert qs.score("", TABLE) == 0 and qs.score(None, TABLE) == 0


def test_the_sql_expression_is_the_same_sum():
    """SCORE_SQL cannot be run here, so its shape is pinned: a case-sensitive
    substring test per term, weights summed, zero when nothing matches — the three
    things qs.score does."""
    sql = " ".join(qs.SCORE_SQL.split())
    assert "strpos(clean_text, t.term) > 0" in sql
    assert "coalesce(sum(t.w), 0)" in sql
    assert "unnest(%s::text[], %s::float8[])" in sql
    assert "lower(" not in sql and "ilike" not in sql.lower()


@pytest.mark.parametrize("raw,msg", [
    ({"likely_threshold": 1.0}, "non-empty"),
    ({"likely_threshold": 1.0, "terms": {}}, "non-empty"),
    ({"likely_threshold": 1.0, "terms": {"x": "1.5"}}, "not a number"),
    ({"likely_threshold": 1.0, "terms": {"x": True}}, "not a number"),
    ({"likely_threshold": 1.0, "terms": {" ": 1.0}}, "blank"),
    ({"terms": {"x": 1.0}}, "likely_threshold"),
    ({"likely_threshold": "high", "terms": {"x": 1.0}}, "likely_threshold"),
])
def test_a_malformed_table_is_refused(raw, msg):
    with pytest.raises(ValueError, match=msg):
        qs.parse_weights(raw)


def test_a_missing_table_means_the_old_rule_and_a_broken_one_raises(tmp_path):
    qs.load.cache_clear()
    try:
        assert qs.load(str(tmp_path / "absent.json")) is None
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"terms": {}}))
        with pytest.raises(ValueError):
            qs.load(str(bad))
    finally:
        qs.load.cache_clear()


def test_the_committed_table_is_small_readable_and_free_of_place_names():
    raw = json.loads((CONFIG_DIR / qs.WEIGHTS_FILE).read_text(encoding="utf-8"))
    table = qs.parse_weights(raw)
    assert 10 <= len(table["terms"]) <= 60
    date.fromisoformat(raw["trained_on"])                      # health dates the table by this
    assert min(table["weights"]) < 0 < max(table["weights"]), "it must push down as well as up"
    assert min(table["weights"]) < table["threshold"] < sum(w for w in table["weights"] if w > 0)
    banned = tw.banned_place_names([])
    for term in table["terms"]:
        assert not any(part.lower() in banned for part in term.split(" ")), (
            f"{term!r} is a place name: the queue would starve every other district")
    kept = tw.drop_nested(table["terms"], table["weights"])
    assert len(kept) == len(table["terms"]), "a term nested in another would count twice"
    # the back-test the table was accepted on travels with it
    assert raw["test"]["table"]["top_10pct"]["precision"] > raw["test"]["rule"]["top_10pct"]["precision"]


# ------------------------------------------------------------- the random slice
def _rows(ids):
    return [{"id": i} for i in ids]


def test_random_reads_are_spread_through_the_run_not_left_at_the_end():
    ranked, extra = _rows(range(1, 101)), _rows(range(1001, 1009))
    order, picked = qs.interleave(ranked, extra, 0.08)
    assert picked == set(range(1001, 1009)) and len(order) == 108
    pos = [i for i, r in enumerate(order) if r["id"] in picked]
    assert pos[0] >= 5, "the first reads of a run are the top-ranked ones"
    gaps = {b - a for a, b in zip(pos, pos[1:])}
    assert gaps == {13}, "one random read after every twelve ranked ones"
    # the property that matters: stop the run anywhere and the share so far is right
    for stop in (30, 60, 100):
        got = sum(1 for r in order[:stop] if r["id"] in picked)
        assert abs(got - stop * 0.08) <= 1.5
    assert [r["id"] for r in order if r["id"] not in picked] == list(range(1, 101)), (
        "ranked rows keep their order")


def test_a_random_pick_the_ranking_already_holds_is_not_read_twice():
    order, picked = qs.interleave(_rows([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]), _rows([3, 99]), 0.25)
    ids = [r["id"] for r in order]
    assert ids.count(3) == 1 and picked == {99}
    assert len(ids) == len(set(ids)) == 11


def test_no_share_or_no_rows_means_no_slice():
    ranked = _rows([1, 2, 3])
    assert qs.interleave(ranked, _rows([9]), 0.0) == (ranked, set())
    assert qs.interleave(ranked, [], 0.08) == (ranked, set())


@pytest.mark.parametrize("cfg,expected", [
    ({}, qs.DEFAULT_RANDOM_SHARE), ({"queue_random_share": 0.05}, 0.05),
    ({"queue_random_share": 0}, 0.0), ({"queue_random_share": 3}, 0.5),
    ({"queue_random_share": -1}, 0.0), ({"queue_random_share": "lots"}, qs.DEFAULT_RANDOM_SHARE)])
def test_random_share_setting(cfg, expected):
    assert qs.random_share(cfg) == expected


# ------------------------------------------------------------------- the store
def _store(monkeypatch, tmp_path, texts):
    from pipeline import store as store_mod
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "source_article.jsonl").write_text("".join(
        json.dumps({"id": i, "url": f"https://example.org/{i}", "clean_text": t,
                    "processing_status": "fetched"}, ensure_ascii=False) + "\n"
        for i, t in texts))
    monkeypatch.setattr(store_mod, "ROOT", tmp_path)
    return store_mod.JsonlStore()


def test_the_store_ranks_the_whole_queue_not_the_oldest_window(monkeypatch, tmp_path):
    """The 31 Jul lesson, kept: 40 low-scoring articles sit ahead of the two that
    matter. A window of the oldest 5 would never reach them."""
    texts = [(i, "बाइक की टक्कर") for i in range(1, 41)]
    texts += [(41, "सड़क पर गड्ढे"), (42, "सड़क पर गड्ढे, मरम्मत की मांग"), (43, "नगर निगम की बैठक")]
    s = _store(monkeypatch, tmp_path, texts)
    got = s.articles_by_status("fetched", limit=5, weights=TABLE)
    assert [a["id"] for a in got] == [42, 41, 43, 1, 2], "score first, then oldest first"
    assert [a["queue_score"] for a in got] == [2.5, 2.0, 0, -1.0, -1.0]


def test_the_random_draw_covers_the_whole_queue_and_is_bounded(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path, [(i, "x") for i in range(1, 201)])
    a = s.articles_random("fetched", limit=20, seed=7)
    assert len(a) == 20 == len({r["id"] for r in a})
    assert max(r["id"] for r in a) > 100 and min(r["id"] for r in a) < 100
    assert s.articles_random("fetched", limit=0) == []
    assert len(s.articles_random("fetched", limit=500, seed=1)) == 200


# ------------------------------------------------------------------- the stage
QUEUE = ([(i, f"Road accident: बाइक की टक्कर, report {i}") for i in range(1, 31)]
         + [(i, f"Road accident: सड़क पर गड्ढे, मरम्मत की मांग, report {i}") for i in range(31, 41)])


def _answer(tool_name, content):
    from tests.test_single_call import INCIDENT
    infra = "गड्ढे" in content
    inc = dict(INCIDENT) if infra else {**INCIDENT, "infra_implicated": False, "defects": []}
    if infra:
        inc["defects"] = [{"defect_type": "potholes_or_broken_surface",
                           "evidence_snippet": "सड़क पर गड्ढे", "confidence": 0.9}]
    return {"in_scope": True, "kind": "both" if infra else "crash", "scope_reason": "r", **inc}


def _run(monkeypatch, tmp_path, settings_patch=None, table=TABLE, drawn=()):
    """Drive the real stage (limit 10) over QUEUE. `drawn` scripts the random draw so
    the slice is the same on every run of the test."""
    from pipeline import store as store_mod
    from tests import test_single_call as h
    monkeypatch.setattr(h, "ARTICLES", QUEUE)
    monkeypatch.setattr(qs, "load", lambda path=None: table)
    asked = []

    def scripted(self, status, limit, seed=None):
        asked.append(limit)
        return [dict(r) for r in self._rows if r["id"] in drawn][:limit]
    monkeypatch.setattr(store_mod.JsonlStore, "articles_random", scripted)
    out = h._run_stage(monkeypatch, tmp_path, "cli", answer=_answer,
                       settings_patch=settings_patch)
    return (*out, asked)


def _read_order(calls):
    return [int(c["content"].split("report ")[1].split("\n")[0]) for c in calls.seen]


def test_the_stage_reads_in_score_order_and_counts_both_slices(monkeypatch, tmp_path):
    # the draw returns one crash article (5) and one the ranking already holds (35)
    stats, status, incidents, calls, asked = _run(
        monkeypatch, tmp_path, settings_patch={"queue_random_share": 0.2}, drawn=(5, 35))
    assert stats["queue_mode"] == "scored"
    assert asked == [2], "20% of the 10 loaded"
    order = _read_order(calls)
    assert [i for i in order if i != 5] == list(range(31, 41)), (
        "the ten defect reports are read, best first; 35 is not read twice")
    assert 5 in order and order.index(5) >= 2, "the random read sits inside the run"
    assert stats["random_ids"] == [5]
    assert stats["read_ranked"] == 10 and stats["read_random"] == 1
    assert stats["infra_ranked"] == 10 and stats["infra_random"] == 0
    assert stats["model_calls"] == len(calls.seen) == 11
    assert status[5] == "extracted" and status[1] == "fetched"


def test_the_setting_turns_score_order_off(monkeypatch, tmp_path):
    stats, _, _, calls, asked = _run(monkeypatch, tmp_path, drawn=(5,),
                                     settings_patch={"scored_queue": False})
    assert stats["queue_mode"] == "vocabulary" and asked == []
    assert stats["read_random"] == 0 and stats["random_ids"] == []
    assert stats["read_ranked"] == len(calls.seen) == 10


def test_no_table_falls_back_to_the_vocabulary_rule(monkeypatch, tmp_path):
    stats, _, _, calls, asked = _run(monkeypatch, tmp_path, table=None, drawn=(5,))
    assert stats["queue_mode"] == "vocabulary" and asked == []
    assert stats["read_ranked"] == len(calls.seen) == 10


def test_a_zero_share_draws_nothing(monkeypatch, tmp_path):
    stats, _, _, calls, asked = _run(monkeypatch, tmp_path, drawn=(5,),
                                     settings_patch={"queue_random_share": 0})
    assert stats["queue_mode"] == "scored" and asked == []
    assert _read_order(calls) == list(range(31, 41)) and stats["read_random"] == 0


# --------------------------------------------------------------- the lag alert
NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("trained,ok,needle", [
    (None, True, "vocabulary-first"),
    ("2026-11-18", True, "0 days old"),
    ("2026-10-04", True, "45 days old"),
    ("2026-10-03", False, "46 days old"),
    ("last month", False, "unreadable"),
])
def test_weight_table_age(trained, ok, needle):
    got_ok, detail = health.weights_age_verdict(trained, NOW.date(), 45)
    assert got_ok is ok and needle in detail


def _lag_check(monkeypatch, settings):
    from pipeline import configload
    from tests.test_health_checks import _Conn, _Cur
    cur = _Cur([(625,), (9593, 13, 1), (NOW - timedelta(days=21), 2338, NOW),
                (0,), (0,), (0, 0)])           # read, published, arrivals — not under test
    monkeypatch.setattr(health, "connect", lambda: _Conn(cur))
    monkeypatch.setattr(configload, "settings", lambda: settings)
    monkeypatch.setattr(configload, "keywords", lambda: {"hi": {"infra_defect": ["गड्ढे"]}})
    monkeypatch.setattr(qs, "load", lambda path=None: TABLE)
    checks = {name: (ok, detail) for name, ok, detail in health.collect_checks(None)}
    return checks, cur


def test_the_lag_alert_watches_the_line_the_queue_reads_from(monkeypatch):
    checks, cur = _lag_check(monkeypatch, {"health": {}})
    ok, detail = checks["extraction_lag"]
    assert not ok and "21.0 days" in detail and "2338" in detail
    assert "likely-defect article (score 1.0 or more)" in detail
    lag_sql = next(q for q in cur.sql if "min(created_at)" in q)
    assert " ".join(qs.SCORE_SQL.split()) in lag_sql and "~*" not in lag_sql
    assert checks["queue_weights_age"][0] is True


def test_without_score_order_the_lag_alert_is_the_vocabulary_one(monkeypatch):
    checks, cur = _lag_check(monkeypatch, {"health": {}, "processing": {"scored_queue": False}})
    assert "defect-vocabulary article" in checks["extraction_lag"][1]
    lag_sql = next(q for q in cur.sql if "min(created_at)" in q)
    assert "~*" in lag_sql and "unnest" not in lag_sql
    assert "vocabulary-first" in checks["queue_weights_age"][1]


# ------------------------------------------------------------------ the trainer
def test_hindi_words_are_not_cut_at_their_vowel_signs():
    assert tw.tokens("सड़क पर गड्ढे, मरम्मत की मांग।") == ["सड़क", "पर", "गड्ढे", "मरम्मत", "की", "मांग"]
    assert tw.tokens("NH-27 | Road Accident") == ["NH", "27", "Road", "Accident"]


def test_candidates_leave_out_place_names_rare_words_and_boilerplate():
    texts = (["गड्ढे सड़क Hardoi खबर"] * 30 + ["टक्कर बाइक Hardoi खबर"] * 30
             + ["गड्ढे दुर्लभ खबर"] * 2)
    labels = [1] * 30 + [0] * 30 + [1, 1]
    got = tw.candidate_terms(texts, labels, min_df=5, max_df_share=0.9, keep=50,
                             banned={"hardoi"}, always=["open manhole", "Hardoi road"])
    assert "गड्ढे" in got and "टक्कर" in got
    assert "खबर" not in got, "a word in every article says nothing"
    assert "दुर्लभ" not in got, "too rare to trust"
    assert not any("Hardoi" in t for t in got), "place names are never learned"
    assert "open manhole" in got, "keyword-pack terms always get a hearing"


def test_a_term_nested_in_another_of_the_same_sign_is_dropped():
    terms, coefs = ["सड़क", "सड़क की", "टक्कर", "सड़क दुर्घटना"], [1.0, 0.8, -0.5, -0.7]
    assert [terms[i] for i in tw.drop_nested(terms, coefs)] == ["सड़क", "टक्कर", "सड़क दुर्घटना"]


def test_auc_and_top_share_precision_handle_ties_honestly():
    assert tw.auc([3, 2, 1, 0], [1, 1, 0, 0]) == 1.0
    assert tw.auc([0, 1, 2, 3], [1, 1, 0, 0]) == 0.0
    assert tw.auc([1, 1, 1, 1], [1, 0, 1, 0]) == 0.5, "a constant score knows nothing"
    assert tw.auc([1, 2], [1, 1]) is None
    # a two-valued rule: its top half is all ties, so precision there is the class's own rate
    scores = [1.0] * 10 + [0.0] * 10
    labels = [1, 1, 1, 1, 1, 1, 0, 0, 0, 0] + [0] * 10
    p, r = tw.precision_at(scores, labels, 0.25)
    assert 0.5 <= p <= 0.7 and r < 1


def test_the_likely_line_is_the_lowest_score_that_still_holds_the_precision():
    scores = [5, 4, 3, 2, 1, 0]
    labels = [1, 1, 1, 0, 0, 0]
    assert tw.likely_threshold(scores, labels, 0.75) == (2.0, 0.75, 1.0)
    assert tw.likely_threshold(scores, labels, 1.0) == (3.0, 1.0, 1.0)
    # a tie group is taken whole: it cannot be split to flatter the line
    assert tw.likely_threshold([2, 2, 2, 2], [1, 0, 0, 0], 0.25) == (2.0, 0.25, 1.0)
    with pytest.raises(SystemExit):
        tw.likely_threshold([1, 1], [0, 0], 0.5)
