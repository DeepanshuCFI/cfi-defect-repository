"""Lean subscription calls (plan item 1.3).

Every `claude -p` call used to carry Claude Code's coding-agent system prompt, its tool
definitions, the skills list and the repo's CLAUDE.md: 31,077 input tokens around a
three-word prompt (measured 18 Nov 2026), 163 with the lean flags. These tests hold the
command line itself, because a flag that quietly goes missing puts the overhead back
without failing anything.
"""
import json

import pytest

from pipeline import llm

TOOL = {"name": "t", "input_schema": {"type": "object",
                                      "properties": {"ok": {"type": "boolean"}}}}
SCHEMA = json.dumps(TOOL["input_schema"], ensure_ascii=False)


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_lean_call_puts_rules_and_schema_in_the_system_prompt():
    argv = llm.cli_argv("/bin/claude", "RULES", TOOL, "ARTICLE TEXT", "m", lean=True)
    assert argv[:3] == ["/bin/claude", "-p", "ARTICLE TEXT"], "the article is the whole user message"
    system = _flag(argv, "--system-prompt")
    assert system.startswith("RULES\n\n") and SCHEMA in system and llm.JSON_ONLY in system
    assert "ARTICLE TEXT" not in system
    assert _flag(argv, "--model") == "m"
    assert _flag(argv, "--output-format") == "json" and _flag(argv, "--max-turns") == "1"


def test_lean_call_switches_off_everything_a_json_call_does_not_use():
    argv = llm.cli_argv("/bin/claude", "RULES", TOOL, "x", "m", lean=True)
    assert _flag(argv, "--tools") == "", "no tool definitions"
    assert _flag(argv, "--setting-sources") == "", "no user/project settings or CLAUDE.md"
    for flag in ("--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    # --bare would be leaner still but reads only an API key, never the subscription
    # login: using it would bill the API or fail. It must never appear.
    assert "--bare" not in argv


def test_full_call_is_exactly_the_call_made_before_the_change():
    argv = llm.cli_argv("/bin/claude", "RULES", TOOL, "ARTICLE TEXT", "m", lean=False)
    assert argv == [
        "/bin/claude", "-p",
        "RULES\n\nReturn a SINGLE JSON object matching this schema exactly. "
        "No prose, no markdown fence, no commentary — the object only.\n"
        f"SCHEMA:\n{SCHEMA}\n\nARTICLE TEXT",
        "--model", "m", "--output-format", "json", "--max-turns", "1"]


@pytest.mark.parametrize("value,expected", [
    (None, True), ("", True), ("1", True), ("yes", True),
    ("0", False), ("false", False), ("OFF", False), (" no ", False)])
def test_lean_is_the_default_and_the_env_var_turns_it_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("LLM_CLI_LEAN", raising=False)
    else:
        monkeypatch.setenv("LLM_CLI_LEAN", value)
    assert llm.cli_lean() is expected


def _fake_cli(monkeypatch, envelope):
    seen = []

    def run(argv, **k):
        seen.append(argv)

        class P:
            stdout, stderr, returncode = json.dumps(envelope), "", 0
        return P

    monkeypatch.setattr(llm, "cli_bin", lambda: "/fake/claude")
    monkeypatch.setattr(llm.subprocess, "run", run)
    monkeypatch.setattr(llm, "_usage", {"calls": 0, **{k: 0 for k in llm._USAGE_KEYS}})
    return seen


def test_the_adapter_sends_the_lean_call_and_still_strips_the_api_key(monkeypatch):
    monkeypatch.delenv("LLM_CLI_LEAN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-reach-the-child")
    envs = []
    seen = _fake_cli(monkeypatch, {"is_error": False, "result": '```json\n{"ok": true}\n```',
                                   "usage": {"input_tokens": 900, "output_tokens": 50}})
    real_run = llm.subprocess.run
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda argv, **k: (envs.append(k["env"]), real_run(argv, **k))[1])
    assert llm._cli("RULES", TOOL, "x", "m", 100) == {"ok": True}
    assert "--system-prompt" in seen[0] and _flag(seen[0], "--tools") == ""
    assert "ANTHROPIC_API_KEY" not in envs[0]


def test_env_var_restores_the_full_call(monkeypatch):
    monkeypatch.setenv("LLM_CLI_LEAN", "0")
    seen = _fake_cli(monkeypatch, {"is_error": False, "result": '{"ok": true}', "usage": {}})
    llm._cli("RULES", TOOL, "x", "m", 100)
    assert "--system-prompt" not in seen[0] and "--tools" not in seen[0]


def test_real_usage_is_recorded_beside_the_notional_charge(monkeypatch):
    from pipeline import llmcost
    monkeypatch.delenv("LLM_CLI_LEAN", raising=False)
    _fake_cli(monkeypatch, {"is_error": False, "result": '{"ok": true}', "usage": {
        "input_tokens": 100, "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 2600, "output_tokens": 400}})
    charged = []
    monkeypatch.setattr(llmcost, "add", lambda model, u: charged.append(u.input_tokens))
    llm._cli("RULES", TOOL, "x", "m", 100)
    llm._cli("RULES", TOOL, "x", "m", 100)
    snap = llm.usage_snapshot()
    assert snap["calls"] == 2
    assert snap["input_per_call"] == 3000, "fresh + cache-written + cache-read input"
    assert snap["output_per_call"] == 400 and snap["cache_read_input_tokens"] == 5200
    assert snap["lean"] is True
    # The meter's per-call weight is deliberately unchanged: it sets how many calls a
    # day the ceiling allows, and that number is the owner's to move.
    assert charged == [100 + llm.CLI_OVERHEAD_TOKENS] * 2


def test_a_malformed_usage_block_never_costs_the_article(monkeypatch):
    _fake_cli(monkeypatch, {"is_error": False, "result": '{"ok": true}',
                            "usage": {"input_tokens": "many", "output_tokens": None}})
    assert llm._cli("RULES", TOOL, "x", "m", 100) == {"ok": True}
    assert llm.usage_snapshot()["calls"] == 1
    _fake_cli(monkeypatch, {"is_error": False, "result": '{"ok": true}', "usage": "n/a"})
    assert llm._cli("RULES", TOOL, "x", "m", 100) == {"ok": True}


def test_an_empty_snapshot_has_no_averages():
    fresh = {"calls": 0, **{k: 0 for k in llm._USAGE_KEYS}}
    old, llm._usage = llm._usage, fresh
    try:
        snap = llm.usage_snapshot()
    finally:
        llm._usage = old
    assert snap["input_per_call"] is None and snap["output_per_call"] is None


def test_ci_pins_the_cli_and_pings_with_the_same_lean_flags():
    """The workflow and the adapter must agree: an unpinned install or a ping without
    the flags would let a CLI release break every call after the checks had passed."""
    from pathlib import Path
    wf = (Path(__file__).resolve().parents[1] / ".github/workflows/daily.yml").read_text()
    assert "npm install -g @anthropic-ai/claude-code@" in wf, "the CLI version must be pinned"
    ping = wf[wf.index('PING=$(timeout 180 claude -p'):wf.index("|| true)", wf.index("PING=$("))]
    for flag in llm.CLI_LEAN_FLAGS:
        if flag:
            assert flag in ping, f"the live ping is missing {flag}"
    assert "--system-prompt" in ping
