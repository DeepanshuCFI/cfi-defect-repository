"""Health alerts that tell the truth (16 Nov 2026).

On 14 Nov three of the eight daily alerts were wrong, not merely noisy:

  yield_7d          reported 0.0 for days while real yield was at a record, because
                    it divided by articles that ARRIVED this week and the run was
                    reading articles from ten days earlier
  publishing_alive  read the previous finished run, and passed whenever spend was
  extraction_alive  under $0.10 — which is what a day of refused calls looks like
  queue_depth       fired on every run for weeks, so nobody read it

The verdict functions are pure; the scenarios below are the real days.
"""
from datetime import datetime, timedelta, timezone

from pipeline.processing import health

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ extraction lag
def test_lag_is_the_age_of_the_oldest_waiting_article():
    assert health.lag_days(NOW - timedelta(days=10, hours=12), NOW) == 10.5
    assert health.lag_days(NOW - timedelta(hours=6), NOW) == 0.2
    assert health.lag_days(None, NOW) is None, "an empty queue has no lag, not a zero"


# ---------------------------------------------------------------- extraction alive
GOOD_PROCESS = {"extracted": 59, "extracted_light": 5, "irrelevant": 17, "failed": 0,
                "loaded": 1000, "stopped_reason": "extraction_budget"}


def test_a_normal_day_is_quiet():
    ok, detail = health.extraction_verdict(GOOD_PROCESS, queued=13134)
    assert ok and "extracted 64" in detail


def test_30_oct_every_call_refused_is_an_alert():
    """754 relevance calls refused, nothing extracted, spend $0.00, run green. The
    old rule (`extracted >= 1 or fetched == 0`, read from the PREVIOUS run) said
    nothing that day."""
    refused = {"extracted": 0, "extracted_light": 0, "irrelevant": 0, "failed": 754,
               "loaded": 1000, "stopped_reason": None}
    ok, detail = health.extraction_verdict(refused, queued=9000)
    assert not ok and "754" in detail


def test_a_stage_that_raised_is_an_alert_not_a_zero():
    ok, detail = health.extraction_verdict("FAILED", queued=500)
    assert not ok and "did not complete" in detail


def test_a_recognised_usage_limit_is_an_alert():
    ok, _ = health.extraction_verdict({**GOOD_PROCESS, "api_limit_hit": True}, queued=500)
    assert not ok


def test_deliberate_stops_and_empty_queues_are_not_alerts():
    # afternoon run after the morning spent the extraction share: stops at once
    stopped = {"extracted": 0, "extracted_light": 0, "irrelevant": 0, "failed": 0,
               "loaded": 1000, "stopped_reason": "extraction_budget"}
    assert health.extraction_verdict(stopped, queued=9000)[0]
    empty = {"extracted": 0, "extracted_light": 0, "irrelevant": 0, "failed": 0,
             "loaded": 0, "stopped_reason": None}
    assert health.extraction_verdict(empty, queued=0)[0]


def test_loaded_but_idle_with_no_reason_is_an_alert():
    idle = {"extracted": 0, "extracted_light": 0, "irrelevant": 3, "failed": 0,
            "loaded": 1000, "stopped_reason": None}
    assert not health.extraction_verdict(idle, queued=9000)[0]


# ---------------------------------------------------------------- publishing alive
GOOD_REVIEW = {"reviewed": 149, "auto_published": 34, "rejected": 14, "errors": 0}


def test_a_normal_review_day_is_quiet():
    ok, detail = health.publishing_verdict(GOOD_REVIEW, waiting=300)
    assert ok and "published 34" in detail


def test_13_nov_stage_failure_is_an_alert():
    ok, detail = health.publishing_verdict("FAILED", waiting=428)
    assert not ok and "428" in detail


def test_25_oct_two_hundred_refused_verdicts_is_an_alert():
    refused = {"reviewed": 0, "auto_published": 0, "errors": 200}
    assert not health.publishing_verdict(refused, waiting=1400)[0]
    assert not health.publishing_verdict({**refused, "errors": 0}, waiting=1400)[0], \
        "judging nothing while records wait is an alert even with no errors counted"


def test_rejecting_everything_is_an_alert_but_a_small_day_is_not():
    assert not health.publishing_verdict({"reviewed": 25, "auto_published": 0, "errors": 0},
                                         waiting=0)[0]
    assert health.publishing_verdict({"reviewed": 6, "auto_published": 0, "errors": 0},
                                     waiting=0)[0]


def test_nothing_waiting_and_nothing_judged_is_a_quiet_day():
    assert health.publishing_verdict({"reviewed": 0, "auto_published": 0, "errors": 0},
                                     waiting=0)[0]


def test_usage_limit_in_review_is_an_alert():
    assert not health.publishing_verdict({**GOOD_REVIEW, "api_limit_hit": True}, waiting=50)[0]


# ------------------------------------------------- the checks, wired, on real days
class _Cur:
    """Scripted cursor: answers collect_checks' queries in the order it asks them."""
    def __init__(self, answers):
        self.answers, self.sql = list(answers), []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchone(self):
        return self.answers.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _checks(monkeypatch, answers, current):
    from pipeline import configload
    cur = _Cur(answers)
    monkeypatch.setattr(health, "connect", lambda: _Conn(cur))
    monkeypatch.setattr(configload, "settings", lambda: {"health": {}})
    monkeypatch.setattr(configload, "keywords", lambda: {"hi": {"infra_defect": ["गड्ढे"]}})
    return {name: (ok, detail) for name, ok, detail in health.collect_checks(current)}, cur


def test_14_nov_the_lag_alerts_and_the_yield_does_not(monkeypatch):
    """The day the old alerts said: queue over 4,000 (as every day) and yield 0.0.
    The truth: the run was 10 days behind, and yield was at a record."""
    current = {"process": GOOD_PROCESS, "auto_review": GOOD_REVIEW}
    answers = [
        (625,),                                        # database MB
        (13134, 0, 0),                                 # fetched, new, stranded
        (NOW - timedelta(days=10), 1394, NOW),         # oldest defect article, count, now
        (428,),                                        # publishable records waiting
        (484,),                                        # read by finished runs, 7 days
        (250,),                                        # published from those runs
        (5775, 866),                                   # arrivals, with defect vocabulary
    ]
    checks, cur = _checks(monkeypatch, answers, current)
    assert set(checks) == {"db_size", "extraction_lag", "new_stuck", "stranded_new",
                           "publishing_alive", "extraction_alive", "yield_7d",
                           "defect_vocab_share_7d", "queue_weights_age"}
    assert "queue_depth" not in checks
    ok, detail = checks["extraction_lag"]
    assert not ok and "10.0 days" in detail and "1394" in detail and "13134" in detail
    ok, detail = checks["yield_7d"]
    # 484 read by earlier runs + this run's 59 + 5 + 17 = 565; 250 published
    assert ok and "250 published / 565 read" in detail and "442.5" in detail
    assert checks["publishing_alive"][0] and checks["extraction_alive"][0]
    assert checks["defect_vocab_share_7d"][0]
    # the yield numerator must be tied to pipeline runs, or a backlog burn inflates it
    assert any("exists (select 1 from pipeline_run" in q for q in cur.sql)


def test_yield_gives_no_verdict_on_a_thin_week(monkeypatch):
    answers = [(625,), (10, 0, 0), (None, 0, NOW), (0,), (12,), (0,), (900, 100)]
    quiet = {"process": {"extracted": 3, "extracted_light": 0, "irrelevant": 1, "failed": 0,
                         "loaded": 4, "stopped_reason": None},
             "auto_review": {"reviewed": 3, "auto_published": 1, "errors": 0}}
    checks, _ = _checks(monkeypatch, answers, quiet)
    ok, detail = checks["yield_7d"]
    assert ok and "n/a" in detail, "16 articles read is noise, not a 0-yield alarm"
    assert checks["extraction_lag"][0], "nothing waiting is not a lag"


def test_run_on_its_own_skips_liveness_rather_than_reading_yesterday(monkeypatch):
    answers = [(625,), (10, 0, 0), (None, 0, NOW), (120,), (30,), (900, 100)]
    checks, _ = _checks(monkeypatch, answers, None)
    assert "publishing_alive" not in checks and "extraction_alive" not in checks
    assert "yield_7d" in checks


def test_cmd_daily_hands_health_this_runs_counters():
    import inspect

    from pipeline import run as run_mod
    assert "health.run(stats)" in inspect.getsource(run_mod.cmd_daily)


# ------------------------------------- a second full run in one day (16 Nov 2026)
SPENT = {"reviewed": 0, "auto_published": 0, "errors": 0, "stopped_reason": "budget"}


def test_a_second_run_whose_ceiling_was_already_spent_is_not_a_fault():
    """16 Nov: the site was redeployed by a manual run in the afternoon. The morning
    run had used the day's ceiling, so the review stage stopped at its first record
    — and the alert read that as a broken adjudicator."""
    ok, detail = health.publishing_verdict(SPENT, waiting=428, ceiling_spent_before_run=True)
    assert ok and "already spent" in detail and "428" in detail


def test_the_same_zero_on_a_run_that_began_with_budget_is_still_the_july_freeze():
    """Negative control. 11-14 Jul: extraction consumed the whole ceiling inside the
    run and nothing was judged. That must keep alerting."""
    assert not health.publishing_verdict(SPENT, waiting=428, ceiling_spent_before_run=False)[0]


def test_a_spent_ceiling_excuses_only_a_budget_stop():
    """With the ceiling spent, zero verdicts for any OTHER reason is still a fault:
    refused calls, a stage that judged nothing without saying why, a usage limit."""
    spent = dict(ceiling_spent_before_run=True)
    assert not health.publishing_verdict({**SPENT, "stopped_reason": None}, 428, **spent)[0]
    assert not health.publishing_verdict({**SPENT, "errors": 200, "stopped_reason": None}, 428, **spent)[0]
    assert not health.publishing_verdict({**SPENT, "stopped_reason": "usage_limit",
                                          "api_limit_hit": True}, 428, **spent)[0]
    assert not health.publishing_verdict("FAILED", 428, **spent)[0]


def test_the_checks_read_the_spend_the_run_started_with(monkeypatch):
    from pipeline import llmcost
    monkeypatch.setattr(llmcost, "budget", lambda: 20.0)
    answers = lambda: [(625,), (13134, 0, 0), (NOW - timedelta(days=1), 10, NOW), (428,),   # noqa: E731
                       (484,), (250,), (5775, 866)]
    second = {"process": {**GOOD_PROCESS, "extracted": 0, "extracted_light": 0, "irrelevant": 0},
              "auto_review": SPENT, "llm_spend_at_start_usd": 20.06}
    checks, _ = _checks(monkeypatch, answers(), second)
    assert checks["publishing_alive"][0], checks["publishing_alive"][1]
    first = {**second, "llm_spend_at_start_usd": 0.0}
    checks, _ = _checks(monkeypatch, answers(), first)
    assert not checks["publishing_alive"][0], "a run that began with budget must still alert"


def test_the_review_stage_says_why_it_stopped_and_cmd_daily_records_the_starting_spend():
    import inspect

    from pipeline import run as run_mod
    from pipeline.processing import auto_review as ar
    src = inspect.getsource(ar._run) if hasattr(ar, "_run") else inspect.getsource(ar.run)
    assert 'stats["stopped_reason"] = "budget"' in src
    assert 'stats["stopped_reason"] = "usage_limit"' in src
    assert 'stats["llm_spend_at_start_usd"]' in inspect.getsource(run_mod.cmd_daily)
