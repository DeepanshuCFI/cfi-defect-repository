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

from pipeline import configload

DEFAULT_BACKEND = "api"
CLI_TIMEOUT_S = 600
# Measured 31 Jul 2026: a three-word prompt cost $0.10 at Opus input rates, i.e. the
# Claude Code system prompt + tool definitions are ~20k tokens on every invocation.
# Callers that can batch should batch; this module bills it per call.
CLI_OVERHEAD_TOKENS = 20_000


class RateLimited(RuntimeError):
    """The subscription (or API) refused on usage grounds. Never a per-item failure —
    the work is fine, the quota is not. Stop the stage and resume next run."""


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
    r"rate.?limit|usage limit|quota|too many requests|429|"
    r"limit reached|try again (later|in)", re.I)


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


def _cli(system, tool, content, model, max_tokens):
    binary = cli_bin()
    if not binary:
        raise BackendError(
            "LLM_BACKEND=cli but the claude CLI was not found. Install it with "
            "`npm install -g @anthropic-ai/claude-code`, or set CLAUDE_BIN.")
    schema = json.dumps(tool["input_schema"], ensure_ascii=False)
    prompt = (f"{system}\n\nReturn a SINGLE JSON object matching this schema exactly. "
              f"No prose, no markdown fence, no commentary — the object only.\n"
              f"SCHEMA:\n{schema}\n\n{content}")
    try:
        p = subprocess.run(
            [binary, "-p", prompt, "--model", model,
             "--output-format", "json", "--max-turns", "1"],
            capture_output=True, text=True, timeout=CLI_TIMEOUT_S, env=_cli_env())
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"cli timed out after {CLI_TIMEOUT_S}s") from exc

    if not p.stdout.strip():
        err = (p.stderr or "")[:400]
        if _RATE_LIMIT.search(err):
            raise RateLimited(err)
        raise BackendError(f"cli exited {p.returncode}: {err}")
    try:
        env = json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise BackendError(f"unparseable envelope: {p.stdout[:200]}") from exc

    result = env.get("result") or ""
    if env.get("is_error"):
        if _RATE_LIMIT.search(result) or _RATE_LIMIT.search(p.stderr or ""):
            raise RateLimited(result[:300])
        raise BackendError(f"cli error: {result[:300]}")

    # Notional only — a subscription call is not billed. Recording it keeps /qa's
    # spend figure meaningful as a measure of work done, and keeps the existing
    # budget guards from silently becoming no-ops on this backend.
    from pipeline import llmcost
    u = env.get("usage") or {}

    class _U:
        input_tokens = int(u.get("input_tokens") or 0) + CLI_OVERHEAD_TOKENS
        output_tokens = int(u.get("output_tokens") or 0)
    llmcost.add(model, _U)
    return _extract_json(result)


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
