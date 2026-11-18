"""One structured-LLM call, over either backend.

Every model call in the pipeline is the same shape: a system prompt, a tool schema,
some content, and a dict back. `structured()` is that call. Two backends implement it:

  api  (default)  Anthropic API with forced tool use — schema-guaranteed output.
  cli             Claude Code headless on the owner's subscription. No API spend.

Set LLM_BACKEND=cli to switch. The default is deliberately `api` so importing this
module changes nothing until someone opts in.

WHY THE BACKENDS DIFFER IN RELIABILITY
The API forces tool use, so the response IS the schema. The CLI returns free text, so
the schema goes into the prompt and the reply is parsed. Parsing can fail; the API path
effectively cannot. Every caller must therefore treat a raised exception as "this item
is not done yet" and leave its row alone — which is what the pipeline already does.

RATE LIMITS AND RESUMABILITY
A subscription has no per-day dollar cap to stop against; it has an opaque rate limit
that arrives mid-run. `RateLimited` is raised for it and `run._api_limit_hit()` treats
it exactly like the API's monthly-limit refusal: stop the stage, leave every unfinished
article at its current processing_status, let the next scheduled run continue. Nothing
is half-written, because callers commit per item, not per batch. A killed session costs
the in-flight item and nothing else.

CREDENTIALS
The CLI child never receives ANTHROPIC_API_KEY: Claude Code prefers an API key when it
sees one, so leaving it in place would silently bill the API while looking like a
successful subscription run. In CI the credential is CLAUDE_CODE_OAUTH_TOKEN (from
`claude setup-token`); locally it is the keychain login.
"""
import json
import os
import re
import shutil
import subprocess
import time

from pipeline import configload

DEFAULT_BACKEND = "api"
CLI_TIMEOUT_S = 600
# Measured 31 Jul 2026: a three-word prompt cost $0.10 at Opus input rates, i.e. the
# Claude Code system prompt + tool definitions are ~20k tokens on every invocation.
# Callers that can batch should batch; this module bills it per call.
CLI_OVERHEAD_TOKENS = 20_000
# 18 Nov 2026: that overhead is not a property of the subscription — it is Claude
# Code's coding-agent system prompt, its tool definitions, the skills list and the
# repo's CLAUDE.md, none of which a JSON extraction call uses. Measured on one
# three-word prompt: 31,077 input tokens with the default flags, 163 with the flags
# below (CLI 2.1.220; 876 on 2.1.326). So the lean call replaces the system prompt
# with ours and switches the rest off.
#
# CLI_OVERHEAD_TOKENS is still what the notional meter charges per call, ON PURPOSE:
# the daily ceiling divided by that charge is the day's call count (~290), and making
# calls cheaper must not silently triple how much of the owner's plan the pipeline
# takes. The real figures are recorded beside it (usage_snapshot); how many calls a
# day to allow, now that each is small, is the owner's decision.
CLI_LEAN_FLAGS = ("--tools", "", "--disable-slash-commands", "--strict-mcp-config",
                  "--setting-sources", "", "--no-session-persistence")
JSON_ONLY = ("Return a SINGLE JSON object matching this schema exactly. "
             "No prose, no markdown fence, no commentary — the object only.")
# Waits before re-trying a call the network refused (see TransientBlock). Three tries
# over ~4 minutes: a passing Cloudflare challenge clears in seconds, and a block that
# outlasts this is treated as "stop the stage", never as a per-item failure.
CLI_TRANSIENT_BACKOFF_S = (20, 60, 180)


class RateLimited(RuntimeError):
    """The subscription (or API) refused on usage grounds. Never a per-item failure —
    the work is fine, the quota is not. Stop the stage and resume next run."""


class TransientBlock(RateLimited):
    """The network between the runner and the model refused the call — a Cloudflare
    challenge, a gateway error, an overloaded upstream. Nothing is wrong with the
    credential or the article. Retried with backoff inside `_cli`; if it outlasts the
    retries it surfaces as a RateLimited, so the stage stops and the next run resumes.

    13 Nov 2026: the CLI reported "Failed to authenticate. API Error: 403 Attention
    Required! | Cloudflare" on one call; 38 calls on the same token had succeeded in
    the ten minutes before and the evening run was fine. It matched the credential
    pattern and killed the auto-review stage as a dead token."""


class CredentialError(RuntimeError):
    """The backend refused the credential itself — dead, revoked, or expired token.
    Never a per-item failure and never worth retrying the next item: run 36 (2 Aug)
    burned 887 identical 401s at ~3s each because this arrived as BackendError and was
    WARN'd per article while the run stayed green. run._raise_if_credential_error maps
    this to ApiCredentialError, which is run-fatal by design (audit, 19 Jul)."""


class BackendError(RuntimeError):
    """The backend could not produce a usable structured result for this item."""


def backend() -> str:
    return (os.environ.get("LLM_BACKEND") or DEFAULT_BACKEND).strip().lower()


# --------------------------------------------------------------------------- api
_client = None


def _api(system, tool, content, model, max_tokens):
    global _client
    if _client is None:
        import anthropic
        from pipeline.settings import ANTHROPIC_API_KEY
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = _client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": content}])
    from pipeline import llmcost
    llmcost.add(model, msg.usage)
    for block in msg.content:
        if block.type == "tool_use":
            return dict(block.input)
    raise BackendError("no tool output")


# --------------------------------------------------------------------------- cli
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# Matched against the CLI's own error text. Deliberately broad: a missed rate-limit
# match degrades into a per-item failure that silently drops an article, so err toward
# catching too much rather than too little.
_RATE_LIMIT = re.compile(
    r"rate.?limit|usage limit|quota|too many requests|\b429\b|"
    r"limit reached|try again (later|in)|"
    # The subscription's own wording, verbatim from the 25/28/30/31 Oct 2026 logs:
    # "You've hit your session limit", "You've hit your weekly limit · resets 7pm
    # (UTC)". None of the phrases above match it, so for three days every call was
    # refused, WARN'd per article, and the run stayed green.
    r"hit your [a-z0-9 \-]{0,30}limit|limit\W{0,6}resets|out of (extra )?usage",
    re.I)
# A refusal by the network path, not by the model's account. Checked FIRST: the CLI
# wraps a Cloudflare challenge page as "Failed to authenticate … 403", which would
# otherwise read as a dead credential (see TransientBlock).
_TRANSIENT = re.compile(
    r"cloudflare|attention required|just a moment|cf-ray|"
    r"\b(500|502|503|504|520|521|522|523|524|529)\b|bad gateway|"
    r"service unavailable|gateway time-?out|overloaded|internal server error|"
    r"econnreset|etimedout|enotfound|eai_again|socket hang up|fetch failed|"
    r"connection (reset|refused|error)", re.I)
# Auth failure as the CLI reports it (verbatim from run 36: "Failed to authenticate.
# API Error: 401 OAuth access token is invalid."). Checked BEFORE _RATE_LIMIT: a
# credential refusal must stop the run, and nothing in these phrases overlaps the
# usage-limit vocabulary above. Matched only against the CLI's error text, never
# against article content.
_AUTH_FAIL = re.compile(
    r"failed to authenticate|\b401\b|\b403\b|invalid (api key|x-api-key)|"
    r"oauth.{0,40}(invalid|expired|revoked)|authentication_error", re.I)


def classify_cli_error(text: str) -> str | None:
    """Name the kind of refusal in the CLI's error text (pure, unit-tested).

    'transient'  network block — retry, then stop the stage
    'credential' the token itself was refused — run-fatal
    'rate_limit' the account's usage limit — stop the stage, resume next run
    None         none of these: a per-item failure

    Order is the whole point. Transient before credential, because a Cloudflare 403
    arrives dressed as an authentication failure. Credential before rate limit, as
    before."""
    text = text or ""
    if _TRANSIENT.search(text):
        return "transient"
    if _AUTH_FAIL.search(text):
        return "credential"
    if _RATE_LIMIT.search(text):
        return "rate_limit"
    return None


def _raise_classified(text: str) -> None:
    kind = classify_cli_error(text)
    if kind == "transient":
        raise TransientBlock(text[:300])
    if kind == "credential":
        raise CredentialError(text[:300])
    if kind == "rate_limit":
        raise RateLimited(text[:300])


def cli_bin() -> str:
    return (os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "").strip()


def _cli_env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)      # see CREDENTIALS in the module docstring
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _extract_json(text: str) -> dict:
    if not text:
        raise BackendError("empty reply")
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        raise BackendError(f"no JSON object in reply: {text[:200]}")
    try:
        out = json.loads(text[s:e + 1])
    except json.JSONDecodeError as exc:
        raise BackendError(f"unparseable JSON: {exc}") from exc
    if not isinstance(out, dict):
        raise BackendError("reply was not a JSON object")
    return out


def cli_lean() -> bool:
    """LLM_CLI_LEAN=0 restores the full Claude Code call — the rollback lever if a CLI
    release changes one of the lean flags. Default on."""
    return (os.environ.get("LLM_CLI_LEAN") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def cli_argv(binary: str, system: str, tool: dict, content: str, model: str,
             lean: bool) -> list[str]:
    """The command line for one call (pure, unit-tested).

    Lean: our rules and the schema ARE the system prompt, the article is the whole
    user message, and nothing else rides along. The schema sits in the system prompt
    because it is identical on every call of a stage, which is what prompt caching
    needs. Not lean: the call exactly as it was before 18 Nov 2026."""
    schema = json.dumps(tool["input_schema"], ensure_ascii=False)
    base = ["--model", model, "--output-format", "json", "--max-turns", "1"]
    if lean:
        return [binary, "-p", content, *base,
                "--system-prompt", f"{system}\n\n{JSON_ONLY}\nSCHEMA:\n{schema}",
                *CLI_LEAN_FLAGS]
    return [binary, "-p", f"{system}\n\n{JSON_ONLY}\nSCHEMA:\n{schema}\n\n{content}", *base]


_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
               "output_tokens")
_usage = {"calls": 0, **{k: 0 for k in _USAGE_KEYS}}


def _count(v) -> int:
    """A token count as the CLI reported it; 0 when it is not a number. A malformed
    usage block must never cost the article it came with."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _record_usage(u: dict) -> None:
    _usage["calls"] += 1
    for k in _USAGE_KEYS:
        _usage[k] += _count(u.get(k))


def usage_snapshot() -> dict:
    """What the CLI calls of this process really used, as the CLI reported it.

    `input_per_call` counts every input token the model was sent — fresh, written to
    cache and read from cache — which is the number the lean flags exist to shrink."""
    snap = dict(_usage)
    sent = sum(snap[k] for k in _USAGE_KEYS[:3])
    n = snap["calls"]
    snap["input_per_call"] = round(sent / n) if n else None
    snap["output_per_call"] = round(snap["output_tokens"] / n) if n else None
    snap["lean"] = cli_lean()
    return snap


def _cli_once(system, tool, content, model, max_tokens):
    binary = cli_bin()
    if not binary:
        raise BackendError(
            "LLM_BACKEND=cli but the claude CLI was not found. Install it with "
            "`npm install -g @anthropic-ai/claude-code`, or set CLAUDE_BIN.")
    try:
        p = subprocess.run(
            cli_argv(binary, system, tool, content, model, cli_lean()),
            capture_output=True, text=True, timeout=CLI_TIMEOUT_S, env=_cli_env())
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"cli timed out after {CLI_TIMEOUT_S}s") from exc

    if not p.stdout.strip():
        err = (p.stderr or "")[:400]
        _raise_classified(err)
        raise BackendError(f"cli exited {p.returncode}: {err}")
    try:
        env = json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise BackendError(f"unparseable envelope: {p.stdout[:200]}") from exc

    result = env.get("result") or ""
    if env.get("is_error"):
        _raise_classified(f"{result}\n{p.stderr or ''}")
        raise BackendError(f"cli error: {result[:300]}")

    # Notional only — a subscription call is not billed. Recording it keeps /qa's
    # spend figure meaningful as a measure of work done, and keeps the existing
    # budget guards from silently becoming no-ops on this backend.
    from pipeline import llmcost
    u = env.get("usage") or {}
    if not isinstance(u, dict):
        u = {}
    _record_usage(u)

    class _U:
        input_tokens = _count(u.get("input_tokens")) + CLI_OVERHEAD_TOKENS
        output_tokens = _count(u.get("output_tokens"))
    llmcost.add(model, _U)
    return _extract_json(result)


def _cli(system, tool, content, model, max_tokens):
    """One CLI call, re-tried through a passing network block.

    Only TransientBlock is retried. A usage limit or a refused credential will not
    change in three minutes, and a per-item BackendError is the caller's to skip."""
    for wait in (*CLI_TRANSIENT_BACKOFF_S, None):
        try:
            return _cli_once(system, tool, content, model, max_tokens)
        except TransientBlock as exc:
            if wait is None:
                raise
            print(f"  network block ({str(exc)[:80]!r}) — retrying in {wait}s")
            time.sleep(wait)


# ------------------------------------------------------------------------ public
def structured(system: str, tool: dict, content: str, *,
               model: str, max_tokens: int = 1000) -> dict:
    """Run one structured call on the configured backend and return the tool input.

    Raises RateLimited (stop the stage, resume next run) or BackendError (skip this
    item). Callers must not mark an item done when either is raised."""
    if backend() == "cli":
        return _cli(system, tool, content, model, max_tokens)
    return _api(system, tool, content, model, max_tokens)


def model_for(key: str) -> str:
    """Resolve a configured model, allowing the CLI backend to be pinned separately
    (LLM_CLI_MODEL) — the subscription's economics differ from the API's."""
    if backend() == "cli":
        override = os.environ.get("LLM_CLI_MODEL")
        if override:
            return override.strip()
    models = configload.settings()["models"]
    return models.get(key, models["extraction"])
