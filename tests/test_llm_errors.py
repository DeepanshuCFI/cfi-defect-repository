"""How the subscription backend's refusals are told apart (16 Nov 2026).

Three different things arrive from the Claude Code CLI as an error string, and each
needs a different response:

  a network block      retry, then stop the stage           (TransientBlock)
  a refused credential fail the run                          (CredentialError)
  a usage limit        stop the stage, resume next run       (RateLimited)

Getting the order wrong has cost real days. 13 Nov: a Cloudflare challenge read as a
dead token and failed auto_review. 25, 28 and 30 Oct: "You've hit your session limit"
matched nothing, every call was WARN'd as a bad article, and the runs stayed green.
The strings below are verbatim from those run logs.
"""
import json

import pytest

from pipeline import llm
from pipeline.run import _api_limit_hit, limit_canary

CLOUDFLARE_13_NOV = "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"
RUN36_401 = "Failed to authenticate. API Error: 401 OAuth access token is invalid."
SESSION_LIMIT = "You've hit your session limit"
WEEKLY_LIMIT = "You've hit your weekly limit · resets 7pm (UTC)"
TOOL = {"input_schema": {"type": "object"}}


@pytest.mark.parametrize("text,kind", [
    (CLOUDFLARE_13_NOV, "transient"),
    ("API Error: 529 Overloaded", "transient"),
    ("API Error: 502 Bad Gateway", "transient"),
    ("TypeError: fetch failed", "transient"),
    (RUN36_401, "credential"),
    ("API Error: 403 OAuth token has been revoked", "credential"),
    (SESSION_LIMIT, "rate_limit"),
    (WEEKLY_LIMIT, "rate_limit"),
    ("usage limit reached, try again later", "rate_limit"),
    ("429 Too Many Requests", "rate_limit"),
    ("no JSON object in reply: here is the summary", None),
    ("prompt is too long", None),
    ("", None),
])
def test_refusals_are_classified(text, kind):
    assert llm.classify_cli_error(text) == kind


def test_a_real_403_is_still_a_dead_credential():
    """Negative control for the Cloudflare carve-out: without the challenge page's
    wording, a 403 on authentication stays run-fatal."""
    assert llm.classify_cli_error("Failed to authenticate. API Error: 403 Forbidden") \
        == "credential"


def _cli_replies(monkeypatch, replies):
    """Fake the CLI: each call pops the next (stdout, stderr) reply. Returns the call
    log and the list of sleeps requested."""
    calls, sleeps = [], []

    def run(*a, **k):
        calls.append(a)
        out, err = replies.pop(0)

        class P:
            stdout, stderr, returncode = out, err, 0 if out else 1
        return P

    monkeypatch.setattr(llm, "cli_bin", lambda: "/fake/claude")
    monkeypatch.setattr(llm.subprocess, "run", run)
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    return calls, sleeps


def _err(text):
    return (json.dumps({"is_error": True, "result": text}), "")


def _ok(obj):
    return (json.dumps({"is_error": False, "result": json.dumps(obj), "usage": {}}), "")


def test_a_passing_block_is_retried_and_the_call_succeeds(monkeypatch):
    calls, sleeps = _cli_replies(monkeypatch, [_err(CLOUDFLARE_13_NOV), _ok({"a": 1})])
    assert llm._cli("sys", TOOL, "content", "claude-sonnet-5", 100) == {"a": 1}
    assert len(calls) == 2 and sleeps == [llm.CLI_TRANSIENT_BACKOFF_S[0]]


def test_a_block_that_outlasts_the_retries_stops_the_stage(monkeypatch):
    n = len(llm.CLI_TRANSIENT_BACKOFF_S) + 1
    calls, sleeps = _cli_replies(monkeypatch, [_err(CLOUDFLARE_13_NOV)] * n)
    with pytest.raises(llm.TransientBlock) as exc:
        llm._cli("sys", TOOL, "content", "claude-sonnet-5", 100)
    assert len(calls) == n and sleeps == list(llm.CLI_TRANSIENT_BACKOFF_S)
    # stop-and-resume, not run-fatal and not a per-item failure
    assert _api_limit_hit(exc.value) is True
    assert not isinstance(exc.value, llm.CredentialError)


def test_block_on_stderr_with_empty_stdout_is_also_transient(monkeypatch):
    calls, _ = _cli_replies(monkeypatch, [("", CLOUDFLARE_13_NOV), _ok({"a": 1})])
    assert llm._cli("sys", TOOL, "content", "claude-sonnet-5", 100) == {"a": 1}
    assert len(calls) == 2


@pytest.mark.parametrize("text,exc_type", [
    (RUN36_401, llm.CredentialError),
    (SESSION_LIMIT, llm.RateLimited),
    (WEEKLY_LIMIT, llm.RateLimited),
])
def test_limits_and_dead_credentials_are_never_retried(monkeypatch, text, exc_type):
    calls, sleeps = _cli_replies(monkeypatch, [_err(text), _ok({"a": 1})])
    with pytest.raises(exc_type):
        llm._cli("sys", TOOL, "content", "claude-sonnet-5", 100)
    assert len(calls) == 1 and sleeps == []


def test_subscription_limit_wording_stops_the_stage():
    """The 25/28/30 Oct gap, end to end: the wording becomes RateLimited, and
    RateLimited is what both stages' except-blocks test for."""
    for text in (SESSION_LIMIT, WEEKLY_LIMIT):
        with pytest.raises(llm.RateLimited) as exc:
            llm._raise_classified(text)
        assert _api_limit_hit(exc.value) is True


def test_limit_canary_tells_the_owner_the_right_thing(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "cli")
    msg = limit_canary("auto_review")
    assert "auto_review" in msg and "do NOT refresh" in msg and "console" not in msg
    monkeypatch.setenv("LLM_BACKEND", "api")
    assert "console limit" in limit_canary("process")


# ------------------------------------------------ auto_review obeys the same contract
class _Cur:
    """Just enough cursor for auto_review._run: the queue query returns `rows`, the
    per-record defect lookup returns nothing, writes are recorded."""
    def __init__(self, rows):
        self.rows, self.writes, self._last = rows, [], None

    def execute(self, sql, params=None):
        self._last = sql
        if sql.lstrip().lower().startswith(("update", "insert")):
            self.writes.append(sql)

    def fetchall(self):
        return self.rows if "from incident r" in self._last else []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _queue(n):
    return [(i, "loc", 0, 0, True, 0.9, 0.8, "summary", "article text") for i in range(1, n + 1)]


def _patch_review(monkeypatch, rows, adjudicate):
    from pipeline import db, llmcost
    from pipeline.processing import auto_review as ar
    cur = _Cur(rows)
    monkeypatch.setattr(db, "connect", lambda: _Conn(cur))
    monkeypatch.setattr(llmcost, "over", lambda *a: False)
    monkeypatch.setattr(ar, "adjudicate", adjudicate)
    return ar, cur


def test_auto_review_stops_on_a_usage_limit_instead_of_warning_per_record(monkeypatch):
    seen = []

    def adjudicate(text, ext):
        seen.append(1)
        raise llm.RateLimited(SESSION_LIMIT)

    ar, cur = _patch_review(monkeypatch, _queue(5), adjudicate)
    stats = ar.run(limit=5)
    assert len(seen) == 1, "kept calling after the limit — 200 refused calls on 25 Oct"
    assert stats["api_limit_hit"] is True and stats["errors"] == 0
    # per-record writes only: a by-rule sweep at the top of the stage (if one exists)
    # is not a verdict on the refused record
    per_record = [w for w in cur.writes if "returning id" not in w.lower()]
    assert per_record == [], "a refused record must stay 'auto' for the next run"


def test_auto_review_keeps_its_counters_when_a_credential_dies_mid_stage(monkeypatch):
    from pipeline.run import ApiCredentialError
    n = {"calls": 0}

    def adjudicate(text, ext):
        n["calls"] += 1
        if n["calls"] == 3:
            raise llm.CredentialError(RUN36_401)
        return {"decision": "reject", "confidence": 0.95, "reason": "out of scope"}

    ar, _ = _patch_review(monkeypatch, _queue(5), adjudicate)
    monkeypatch.setattr(ar, "decide", lambda v, gc: "rejected")
    with pytest.raises(ApiCredentialError) as exc:
        ar.run(limit=5)
    assert exc.value.partial_stats["reviewed"] == 2
    assert exc.value.partial_stats["rejected"] == 2
