"""One model call per article (plan item 1.1).

Until Nov 2026 every article cost two calls: relevance.classify, then extract. On the
subscription backend each call carries the same fixed overhead, so the screening call
bought nothing and halved what a day's allowance could read (55-63 articles a day
against ~125 defect-vocabulary arrivals). extract.read_article asks both questions at
once. These tests hold the three things that can go wrong: the call count, the scope
rule drifting between the two paths, and an in-scope answer with a hollow incident.
"""
import argparse
import json

import pytest

from pipeline.processing import extract as ex
from pipeline.processing import relevance as rel

INCIDENT = {
    "location_text_best": "near Baghi village, NH-27, Supaul district, Bihar",
    "road_type": "NH", "fatalities": 1, "injuries": 2,
    "narrative_summary": "A bike hit a pothole on NH-27.",
    "infra_implicated": True,
    "defects": [{"defect_type": "potholes_or_broken_surface",
                 "evidence_snippet": "deep pothole on the highway", "confidence": 0.9}],
    "extraction_confidence": 0.9,
}


# ------------------------------------------------------------------ split_scope
def test_out_of_scope_carries_no_incident():
    scope, inc = ex.split_scope({"in_scope": False, "kind": "out_of_scope",
                                 "scope_reason": "a train derailment"})
    assert scope == {"in_scope": False, "kind": "out_of_scope", "reason": "a train derailment"}
    assert inc is None


def test_out_of_scope_ignores_any_incident_the_model_volunteered():
    scope, inc = ex.split_scope({"in_scope": False, "kind": "out_of_scope",
                                 "scope_reason": "x", **INCIDENT})
    assert scope["in_scope"] is False and inc is None


@pytest.mark.parametrize("verdict", [
    {"in_scope": True, "kind": "out_of_scope"},     # the two fields disagree
    {"in_scope": "true", "kind": "both"},           # a string is not a yes
    {"kind": "both"},                               # no verdict at all
    {},
])
def test_a_contradicted_or_missing_verdict_is_out_of_scope(verdict):
    scope, inc = ex.split_scope({**verdict, **INCIDENT})
    assert scope["in_scope"] is False and scope["kind"] == "out_of_scope"
    assert inc is None


def test_in_scope_returns_the_incident_without_the_scope_fields():
    scope, inc = ex.split_scope({"in_scope": True, "kind": "both",
                                 "scope_reason": "pothole crash", **INCIDENT})
    assert scope == {"in_scope": True, "kind": "both", "reason": "pothole crash"}
    assert inc == INCIDENT
    assert not set(ex.SCOPE_FIELDS) & set(inc)


@pytest.mark.parametrize("infra,expected", [(True, "both"), (False, "crash")])
def test_an_unknown_kind_is_decided_by_the_incident_itself(infra, expected):
    for kind in (None, "", "accident"):
        scope, inc = ex.split_scope({"in_scope": True, "kind": kind,
                                     **INCIDENT, "infra_implicated": infra})
        assert scope["in_scope"] is True and scope["kind"] == expected
        assert inc is not None


def test_in_scope_with_a_hollow_incident_is_an_error_not_a_record():
    hollow = {"in_scope": True, "kind": "crash", "scope_reason": "a crash"}
    with pytest.raises(ValueError, match="missing"):
        ex.split_scope(hollow)
    # one required field short is still short
    partial = {**hollow, **{k: v for k, v in INCIDENT.items() if k != "defects"}}
    with pytest.raises(ValueError, match="defects"):
        ex.split_scope(partial)


# ------------------------------------------------------------- schema and prompt
def test_both_paths_quote_the_same_scope_rule():
    assert rel.SCOPE_RULES in rel.SYSTEM
    assert rel.SCOPE_RULES in ex.single_system()
    assert ex.SYSTEM in ex.single_system(), "the extraction rules must survive intact"


def test_single_tool_is_the_extraction_schema_plus_the_verdict():
    single = ex.build_single_tool()["input_schema"]
    plain = ex.build_tool()["input_schema"]
    assert set(single["properties"]) == set(plain["properties"]) | set(ex.SCOPE_FIELDS)
    for name, spec in plain["properties"].items():
        assert single["properties"][name] == spec, f"{name} drifted from the extraction schema"
    # Only the verdict is required: an out-of-scope article must not be made to
    # describe an incident it does not contain.
    assert single["required"] == list(ex.SCOPE_FIELDS)
    assert tuple(single["properties"]["kind"]["enum"]) == rel.KINDS
    assert tuple(rel.TOOL["input_schema"]["properties"]["kind"]["enum"]) == rel.KINDS


@pytest.mark.parametrize("setting,backend,expected", [
    (None, "cli", True), (None, "api", False),          # the default: subscription only
    ("cli", "cli", True), ("cli", "api", False),
    (True, "api", True), (True, "cli", True),
    (False, "cli", False), (False, "api", False),
    ("api", "api", True), ("off", "cli", False),
])
def test_single_call_setting(setting, backend, expected):
    cfg = {} if setting is None else {"single_call_extraction": setting}
    assert ex.single_call_enabled(cfg, backend) is expected


# ------------------------------------------------------------------ read_article
class _Calls:
    """Stands in for llm.structured: counts calls, answers from a script."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def __call__(self, system, tool, content, *, model, max_tokens=1000):
        self.seen.append({"system": system, "tool": tool["name"], "content": content})
        out = self.answer(tool["name"], content) if callable(self.answer) else self.answer
        if isinstance(out, Exception):
            raise out
        return json.loads(json.dumps(out))      # a fresh copy per call, like a real reply


def test_read_article_is_one_call_and_validates_snippets(monkeypatch):
    from pipeline import llm
    text = "Supaul: a deep pothole on the highway threw a biker off on Friday."
    answer = {"in_scope": True, "kind": "both", "scope_reason": "pothole crash", **INCIDENT,
              "defects": INCIDENT["defects"] + [
                  {"defect_type": "no_street_lighting",
                   "evidence_snippet": "the stretch has no lights",     # not in the article
                   "confidence": 0.8}]}
    calls = _Calls(answer)
    monkeypatch.setattr(llm, "structured", calls)
    scope, inc, dropped = ex.read_article("t", text, published_at="2026-11-17")
    assert len(calls.seen) == 1
    assert calls.seen[0]["tool"] == "record_article"
    assert "ARTICLE PUBLISHED: 2026-11-17" in calls.seen[0]["content"]
    assert scope["in_scope"] and scope["kind"] == "both"
    assert dropped == ["no_street_lighting"], "a non-verbatim snippet must still be dropped"
    assert [d["defect_type"] for d in inc["defects"]] == ["potholes_or_broken_surface"]


def test_read_article_out_of_scope_and_hollow_answers(monkeypatch):
    from pipeline import llm
    monkeypatch.setattr(llm, "structured", _Calls(
        {"in_scope": False, "kind": "out_of_scope", "scope_reason": "politics"}))
    assert ex.read_article("t", "text") == (
        {"in_scope": False, "kind": "out_of_scope", "reason": "politics"}, None, [])
    monkeypatch.setattr(llm, "structured", _Calls(
        {"in_scope": True, "kind": "crash", "scope_reason": "a crash"}))
    with pytest.raises(llm.BackendError, match="missing"):
        ex.read_article("t", "text")


# -------------------------------------------------- the stage: calls per article
ARTICLES = [
    (1, "Road accident near Patna: minister addresses a rally about elections."),
    (2, "Road accident: a speeding truck hit a car near Gaya, two injured."),
    (3, "Road accident: deep pothole on the highway near Baghi threw a biker off."),
]


def _answer(tool_name, content):
    """A scripted model: article 1 is out of scope, 2 a plain crash, 3 a defect crash."""
    if "rally" in content:
        verdict = {"in_scope": False, "kind": "out_of_scope", "reason": "politics",
                   "scope_reason": "politics"}
        return verdict
    crash_only = "truck" in content
    incident = ({**INCIDENT, "infra_implicated": False, "defects": []} if crash_only
                else dict(INCIDENT))
    kind = "crash" if crash_only else "both"
    if tool_name == "record_classification":
        return {"in_scope": True, "kind": kind, "reason": "r"}
    if tool_name == "record_incident":
        return incident
    return {"in_scope": True, "kind": kind, "scope_reason": "r", **incident}


def _run_stage(monkeypatch, tmp_path, backend, answer=_answer, settings_patch=None):
    from pipeline import configload, llm
    from pipeline import db as db_mod
    from pipeline import run as run_mod
    from pipeline import store as store_mod

    data = tmp_path / "data"
    data.mkdir()
    (data / "source_article.jsonl").write_text("".join(
        json.dumps({"id": i, "url": f"https://example.org/{i}", "clean_text": t,
                    "processing_status": "fetched", "published_at": "2026-11-17"}) + "\n"
        for i, t in ARTICLES))

    def _no_db():
        raise RuntimeError("unit tests have no DB")

    calls = _Calls(answer)
    monkeypatch.setenv("LLM_BACKEND", backend)
    monkeypatch.delenv("LLM_CLI_MODEL", raising=False)
    monkeypatch.setattr(llm, "structured", calls)
    monkeypatch.setattr(store_mod, "ROOT", tmp_path)
    monkeypatch.setattr(db_mod, "connect", _no_db)
    monkeypatch.setattr(configload, "_db_available", lambda: False)
    configload.settings.cache_clear()
    configload.keywords.cache_clear()
    if settings_patch:
        real = configload.settings()
        patched = {**real, "processing": {**real.get("processing", {}), **settings_patch}}
        monkeypatch.setattr(configload, "settings", lambda: patched)
    try:
        stats = run_mod.cmd_process(argparse.Namespace(limit=10, jsonl=True))
    finally:
        monkeypatch.undo()
        configload.settings.cache_clear()
        configload.keywords.cache_clear()
    rows = [json.loads(l) for l in (data / "source_article.jsonl").read_text().splitlines()]
    status = {r["id"]: r["processing_status"] for r in rows}
    inc_path = data / "incident.jsonl"
    incidents = ([json.loads(l) for l in inc_path.read_text().splitlines()]
                 if inc_path.exists() else [])
    return stats, status, incidents, calls


def test_subscription_backend_makes_one_call_per_article(monkeypatch, tmp_path):
    stats, status, incidents, calls = _run_stage(monkeypatch, tmp_path, "cli")
    assert stats["single_call"] is True
    assert len(calls.seen) == len(ARTICLES) == stats["model_calls"]
    assert {c["tool"] for c in calls.seen} == {"record_article"}
    assert status == {1: "irrelevant", 2: "extracted", 3: "extracted"}
    assert stats["irrelevant"] == 1 and stats["extracted"] == 2 and stats["failed"] == 0
    assert stats["extracted_light"] == 0, "one call has no earlier verdict to tier on"
    # the defect-vocabulary article is read first, so order is not article order
    assert sorted(i["infra_implicated"] for i in incidents) == [False, True]
    assert not any(set(ex.SCOPE_FIELDS) & set(i) for i in incidents)


def test_api_backend_keeps_the_screen_then_extract_path(monkeypatch, tmp_path):
    """The negative control: same three articles, two-call path, five calls."""
    stats, status, incidents, calls = _run_stage(monkeypatch, tmp_path, "api")
    assert stats["single_call"] is False
    # article 1 is screened out after one call; 2 and 3 take a screen and an extraction
    assert sorted(c["tool"] for c in calls.seen) == (
        ["record_classification"] * 3 + ["record_incident"] * 2)
    assert stats["model_calls"] == 5
    assert status == {1: "irrelevant", 2: "extracted", 3: "extracted"}
    assert stats["extracted_light"] == 1 and stats["extracted"] == 1
    assert sorted(i["infra_implicated"] for i in incidents) == [False, True]


def test_the_setting_can_turn_single_call_off_on_the_subscription(monkeypatch, tmp_path):
    stats, _, _, calls = _run_stage(monkeypatch, tmp_path, "cli",
                                    settings_patch={"single_call_extraction": False})
    assert stats["single_call"] is False and len(calls.seen) == 5


def test_a_hollow_in_scope_answer_fails_the_article_and_inserts_nothing(monkeypatch, tmp_path):
    def hollow(tool_name, content):
        return {"in_scope": True, "kind": "crash", "scope_reason": "r"}
    stats, status, incidents, calls = _run_stage(monkeypatch, tmp_path, "cli", answer=hollow)
    assert incidents == []
    assert set(status.values()) == {"failed"} and stats["failed"] == 3
    assert stats["model_calls"] == 3


def test_a_usage_limit_stops_the_stage_and_leaves_the_queue_alone(monkeypatch, tmp_path):
    from pipeline import llm

    def limited(tool_name, content):
        return llm.RateLimited("You've hit your weekly limit")
    stats, status, incidents, calls = _run_stage(monkeypatch, tmp_path, "cli", answer=limited)
    assert len(calls.seen) == 1, "the first refusal must stop the stage"
    assert stats["stopped_reason"] == "api_monthly_limit" and stats["api_limit_hit"] is True
    assert set(status.values()) == {"fetched"}, "a refused call is not a failed article"
    assert incidents == [] and stats["failed"] == 0
