"""Per-process LLM spend meter with a hard per-run budget (owner cap: $2/day).

Every Anthropic call in the pipeline reports its usage here via add(); cmd_process and
auto_review check over() and stop gracefully when the budget is hit — unprocessed
articles keep status 'fetched' and roll into the next day's budget. Prices are USD per
million tokens, overridable in config settings under "models_pricing" so a price change
never needs a code change.
"""
from pipeline import configload

_spent_usd = 0.0


def set_baseline(prior_usd: float) -> None:
    """Seed the meter with what earlier runs already spent TODAY (from pipeline_run),
    so the cap is a per-DAY guarantee, not per-process — a retry run after a morning
    failure continues from the morning's spend instead of getting a fresh budget."""
    global _spent_usd
    _spent_usd = max(_spent_usd, float(prior_usd or 0))

DEFAULT_PRICES = {
    "claude-sonnet-5": {"in": 3.0, "out": 15.0},
    "claude-haiku-4-5-20251001": {"in": 1.0, "out": 5.0},
}
# unknown model id -> assume strong-tier pricing (over-estimate, never under)
FALLBACK_PRICE = {"in": 3.0, "out": 15.0}


def _prices() -> dict:
    return {**DEFAULT_PRICES, **(configload.settings().get("models_pricing") or {})}


def add(model: str, usage) -> None:
    """Record one API call's usage (anthropic Message.usage)."""
    global _spent_usd
    p = _prices().get(model, FALLBACK_PRICE)
    _spent_usd += (getattr(usage, "input_tokens", 0) / 1e6 * p["in"]
                   + getattr(usage, "output_tokens", 0) / 1e6 * p["out"])


def spent() -> float:
    return _spent_usd


def budget() -> float:
    """The stage's ceiling for today.

    On the API backend this is real money. On LLM_BACKEND=cli it is NOT: the meter
    records the NOTIONAL cost of subscription tokens so /qa keeps a comparable measure
    of work done, but nothing is billed. Comparing that notional figure to the API's
    $1.95 cap throttled the pipeline to ~20 articles/day, because every CLI invocation
    carries ~20k tokens of Claude Code system prompt that the API path does not pay
    (measured 1 Aug 2026: $0.10/article on cli vs $0.0149 on api). The cli ceiling is
    therefore a RUNAWAY GUARD sized to daily intake, not a spend limit — the real
    backstop on that backend is the rate limit, which stops the stage gracefully and
    resumes next run."""
    p = configload.settings().get("processing", {})
    from pipeline import llm
    if llm.backend() == "cli":
        return float(p.get("llm_budget_usd_per_day_cli", 20.0))
    return float(p.get("llm_budget_usd_per_day", p.get("llm_budget_usd_per_run", 2.0)))


def extraction_share(pending_adjudications: int, budget_usd: float,
                     per_item_usd: float = 0.015, floor: float = 0.15,
                     cap: float = 0.5) -> float:
    """Share of the daily budget extraction may spend, given how much adjudication
    actually has to do today. Pure + unit-tested.

    A FIXED 50/50 split (15 Jul) was starving extraction: measured adjudication needs
    only ~$0.012/item, so on a typical day it used ~$0.28 of its reserved ~$0.97 and
    the pipeline left ~35% of the budget unspent while the queue grew. Reserve what is
    needed, hand the rest to extraction.
    """
    if budget_usd <= 0:
        return 1.0 - cap
    reserve = (max(0, pending_adjudications) * per_item_usd) / budget_usd
    return 1.0 - min(cap, max(floor, reserve))


def over(share: float = 1.0) -> bool:
    """True once spend reaches `share` of the daily budget. Extraction passes a share
    < 1 so it can never consume the whole budget and starve adjudication (which gates
    publication) — the 11-14 Jul map freeze: extraction ate 100%, 0 got published."""
    return _spent_usd >= budget() * share
