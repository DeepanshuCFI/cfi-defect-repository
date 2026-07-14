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
    p = configload.settings().get("processing", {})
    return float(p.get("llm_budget_usd_per_day", p.get("llm_budget_usd_per_run", 2.0)))


def over(share: float = 1.0) -> bool:
    """True once spend reaches `share` of the daily budget. Extraction passes a share
    < 1 so it can never consume the whole budget and starve adjudication (which gates
    publication) — the 11-14 Jul map freeze: extraction ate 100%, 0 got published."""
    return _spent_usd >= budget() * share
