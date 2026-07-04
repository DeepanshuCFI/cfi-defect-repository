"""Structured extraction (BUILD_SPEC §7.3) — strong model, forced JSON, taxonomy-locked.

Honesty enforcement beyond the prompt:
- defect_type outside the taxonomy → dropped (schema enum makes this near-impossible)
- evidence_snippet must be a verbatim substring of the article text (whitespace-
  normalised); non-verbatim snippets are DROPPED and logged, never stored.
- If all defects drop, infra_implicated is recomputed accordingly.

Note (stated deviation, Phase 0): translation is folded into this call — English fields
out, original-language evidence snippets kept. No separate translate pass.
"""
import json
import re

import anthropic

from pipeline import configload
from pipeline.settings import ANTHROPIC_API_KEY, CONFIG_DIR

_client: anthropic.Anthropic | None = None


def taxonomy_codes() -> list[str]:
    tax = json.load(open(CONFIG_DIR / "config_defect_taxonomy.json"))["taxonomy"]
    return [t["code"] for t in tax]


def build_tool() -> dict:
    codes = taxonomy_codes()
    return {
        "name": "record_incident",
        "description": "Record the structured road-crash/defect incident from the article.",
        "input_schema": {
            "type": "object",
            "properties": {
                "crash_date": {"type": ["string", "null"],
                               "description": "YYYY-MM-DD if stated/derivable, else null. Never guess."},
                "crash_time": {"type": ["string", "null"], "description": "HH:MM 24h if stated, else null"},
                "location_text_best": {
                    "type": "string",
                    "description": ("Most specific location phrase for geocoding, English/transliterated: "
                                    "landmark/junction/village + road + district + state, e.g. "
                                    "'near Baghi village, NH-27, Supaul district, Bihar'")},
                "road_name": {"type": ["string", "null"]},
                "road_type": {"type": "string",
                              "enum": ["NH", "SH", "MDR", "district", "urban_arterial",
                                       "urban_local", "rural", "unknown"]},
                "admin_state": {"type": ["string", "null"]},
                "admin_district": {"type": ["string", "null"]},
                "admin_city": {"type": ["string", "null"]},
                "admin_ward": {"type": ["string", "null"]},
                "fatalities": {"type": "integer", "minimum": 0,
                               "description": "Only if explicitly stated; else 0"},
                "injuries": {"type": "integer", "minimum": 0},
                "victim_types": {"type": "array", "items": {
                    "type": "string",
                    "enum": ["pedestrian", "two_wheeler", "cyclist", "car", "truck", "bus", "auto", "other"]}},
                "vehicles_involved": {"type": "array", "items": {"type": "string"}},
                "narrative_summary": {"type": "string",
                                      "description": "≤60 words, English, factual, no embellishment"},
                "infra_implicated": {
                    "type": "boolean",
                    "description": ("true ONLY if the article's own words attribute the crash/hazard "
                                    "(at least partly) to road infrastructure")},
                "defects": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "defect_type": {"type": "string", "enum": codes},
                        "evidence_snippet": {
                            "type": "string",
                            "description": ("VERBATIM quote from the article (original language, "
                                            "≤200 chars) that supports this defect. Copy exactly — "
                                            "it is validated as a substring.")},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["defect_type", "evidence_snippet", "confidence"]}},
                "extraction_confidence": {
                    "type": "number", "minimum": 0, "maximum": 1,
                    "description": "Overall: article clarity × completeness of what you extracted"},
            },
            "required": ["location_text_best", "road_type", "fatalities", "injuries",
                         "narrative_summary", "infra_implicated", "defects",
                         "extraction_confidence"],
        },
    }


SYSTEM = (
    "You extract structured road-crash/infrastructure-defect records from Indian news "
    "articles (any Indian language) for a public evidence registry used with government. "
    "Absolute rules:\n"
    "1. NEVER invent or infer beyond the article's words. Unstated → null/0/empty.\n"
    "2. Tag a defect ONLY if the article's own words support it, with a VERBATIM quote "
    "in the article's original language as evidence_snippet.\n"
    "3. If the cause is purely driver/vehicle behaviour, use defect_type "
    "'no_infrastructure_defect_identified' with the sentence stating the cause, and set "
    "infra_implicated=false.\n"
    "4. Casualty numbers only when explicitly stated.\n"
    "5. location_text_best: as specific as the article allows, nothing more.\n"
    "6. Multiple distinct crashes in one article: extract the MAIN one only.\n"
    "7. Always fill admin_state and admin_district when they are derivable from the "
    "article or location (English spellings); leave null only if genuinely unclear."
)

_ws = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _ws.sub(" ", s).strip().lower()


def validate_snippets(result: dict, article_text: str) -> tuple[dict, list[str]]:
    """Consistency pass:
    - drop defects whose snippet is not a verbatim (whitespace-normalised) substring
    - dedupe repeated defect_types (keep highest confidence)
    - no_infrastructure_defect_identified cannot co-exist with real defects
    """
    dropped = []
    hay = _norm(article_text)
    best: dict[str, dict] = {}
    for d in result.get("defects", []):
        if _norm(d.get("evidence_snippet", "")) not in hay:
            dropped.append(d.get("defect_type", "?"))
            continue
        code = d["defect_type"]
        if code not in best or d["confidence"] > best[code]["confidence"]:
            best[code] = d
    real = [c for c in best if c != "no_infrastructure_defect_identified"]
    if real:
        best.pop("no_infrastructure_defect_identified", None)
    result["defects"] = list(best.values())
    if not real:
        result["infra_implicated"] = False
    return result, dropped


def extract(title: str | None, text: str,
            published_at: str | None = None,
            light: bool = False) -> tuple[dict, list[str]]:
    """light=True uses the cheap model — for pure-behaviour crashes that only feed
    location crash-frequency counts; infra-implicated coverage gets the strong model."""
    models = configload.settings()["models"]
    model = models.get("extraction_light", models["extraction"]) if light \
        else models["extraction"]
    pub = f"ARTICLE PUBLISHED: {published_at}\n" if published_at else ""
    content = (f"{pub}TITLE: {title or '(none)'}\n\nARTICLE:\n{text[:7000]}\n\n"
               "Resolve relative dates (e.g. 'on Friday', 'yesterday', weekday names "
               "with no year) against the published date above. If the article "
               "clearly reports an OLD crash (an anniversary/retrospective), keep the "
               "old date. If no date is derivable, use null.")
    msg = _get_client().messages.create(
        model=model, max_tokens=2000, system=SYSTEM,
        tools=[build_tool()], tool_choice={"type": "tool", "name": "record_incident"},
        messages=[{"role": "user", "content": content}])
    for block in msg.content:
        if block.type == "tool_use":
            return validate_snippets(dict(block.input), text)
    raise RuntimeError("extraction returned no tool output")


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client
