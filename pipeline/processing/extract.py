"""Structured extraction (BUILD_SPEC §7.3) — strong model, forced JSON, taxonomy-locked.

Honesty enforcement beyond the prompt:
- defect_type outside the taxonomy → dropped (schema enum makes this near-impossible)
- evidence_snippet must be a verbatim substring of the article text (whitespace-
  normalised); non-verbatim snippets are DROPPED and logged, never stored.
- If all defects drop, infra_implicated is recomputed accordingly.

Note (stated deviation, Phase 0): translation is folded into this call — English fields
out, original-language evidence snippets kept. No separate translate pass.

ONE CALL PER ARTICLE (read_article, Nov 2026): the relevance verdict is folded into
this call too. On the subscription backend every call carries the same fixed overhead
whatever it asks, so a cheap screening call ahead of extraction saved nothing and
halved what a day's allowance could read. The two-call path (relevance.classify, then
extract) stays for the API backend, where a small model screens at a fraction of the
extraction price.
"""
import json
import re

from pipeline.settings import CONFIG_DIR


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
    "4. Casualty numbers only when explicitly stated, and only the victims of the ONE "
    "crash this record describes. Totals over a period, an area or many crashes "
    "('560 deaths in 10 months', 'over 50 deaths at this spot') are NOT casualties: set "
    "fatalities=0 and injuries=0 and keep the figure in narrative_summary.\n"
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


def _content(title: str | None, text: str, published_at: str | None) -> str:
    pub = f"ARTICLE PUBLISHED: {published_at}\n" if published_at else ""
    return (f"{pub}TITLE: {title or '(none)'}\n\nARTICLE:\n{text[:7000]}\n\n"
            "Resolve relative dates (e.g. 'on Friday', 'yesterday', weekday names "
            "with no year) against the published date above. If the article "
            "clearly reports an OLD crash (an anniversary/retrospective), keep the "
            "old date. If no date is derivable, use null.")


def extract(title: str | None, text: str,
            published_at: str | None = None,
            light: bool = False) -> tuple[dict, list[str]]:
    """light=True uses the cheap model — for pure-behaviour crashes that only feed
    location crash-frequency counts; infra-implicated coverage gets the strong model."""
    from pipeline import llm
    model = llm.model_for("extraction_light" if light else "extraction")
    result = llm.structured(SYSTEM, build_tool(), _content(title, text, published_at),
                            model=model, max_tokens=2000)
    return validate_snippets(result, text)


# ------------------------------------------------------------- one call per article
SCOPE_FIELDS = ("in_scope", "kind", "scope_reason")
IN_SCOPE_KINDS = ("crash", "infra_defect", "both")


def build_single_tool() -> dict:
    """The extraction schema with the scope verdict in front.

    Only the verdict is `required`: an out-of-scope article has no incident to
    describe, and forcing the model to fill one in would invent a record. What an
    in-scope answer must carry is checked in split_scope instead."""
    from pipeline.processing import relevance as rel
    tool = build_tool()
    schema = tool["input_schema"]
    scope = {
        "in_scope": {"type": "boolean",
                     "description": rel.TOOL["input_schema"]["properties"]["in_scope"]["description"]},
        "kind": {"type": "string", "enum": list(rel.KINDS),
                 "description": ("crash = a crash with no road defect reported; infra_defect = "
                                 "a road defect/hazard with no crash; both = a crash the article "
                                 "ties to a road defect; out_of_scope otherwise.")},
        "scope_reason": {"type": "string", "description": "≤15 words"},
    }
    return {
        "name": "record_article",
        "description": ("Record whether the article is in scope and, only if it is, the "
                        "structured road-crash/defect incident it reports."),
        "input_schema": {
            "type": "object",
            "properties": {**scope, **schema["properties"]},
            "required": list(SCOPE_FIELDS),
        },
    }


def single_system() -> str:
    from pipeline.processing import relevance as rel
    return (
        SYSTEM + "\n\n"
        "FIRST decide whether the article is in scope. " + rel.SCOPE_RULES +
        "Judge only from the text given.\n"
        "If it is OUT of scope: return in_scope=false, kind='out_of_scope' and "
        "scope_reason, and NOTHING else — do not describe an incident.\n"
        "If it is IN scope: return in_scope=true, kind, scope_reason and the full "
        "incident record under the rules above."
    )


def split_scope(result: dict) -> tuple[dict, dict | None]:
    """Separate the scope verdict from the incident. Pure.

    Returns ({in_scope, kind, reason}, incident-or-None). An article is in scope only
    when the model says so on BOTH fields: in_scope=true with kind='out_of_scope' is a
    contradiction, and a contradicted record is not one to put in an evidence registry.
    A missing or unknown kind is not a contradiction — the incident's own
    infra_implicated decides it. An in-scope answer missing the fields an incident is
    made of raises ValueError: the caller treats that as a failed read, never as a
    record with invented blanks."""
    result = dict(result or {})
    said_in = result.pop("in_scope", None) is True
    kind = result.pop("kind", None)
    reason = str(result.pop("scope_reason", "") or "")
    if not said_in or kind == "out_of_scope":
        return {"in_scope": False, "kind": "out_of_scope", "reason": reason}, None
    missing = [f for f in build_tool()["input_schema"]["required"] if f not in result]
    if missing:
        raise ValueError(f"in scope but the incident is missing {missing}")
    if kind not in IN_SCOPE_KINDS:
        kind = "both" if result.get("infra_implicated") is True else "crash"
    return {"in_scope": True, "kind": kind, "reason": reason}, result


def read_article(title: str | None, text: str,
                 published_at: str | None = None) -> tuple[dict, dict | None, list[str]]:
    """Scope verdict and extraction in ONE model call.

    Returns (scope, incident, dropped_snippets); incident is None when out of scope.
    Always the full extraction model: with a single call there is no earlier verdict
    to tier on."""
    from pipeline import llm
    result = llm.structured(single_system(), build_single_tool(),
                            _content(title, text, published_at),
                            model=llm.model_for("extraction"), max_tokens=2000)
    try:
        scope, inc = split_scope(result)
    except ValueError as exc:
        raise llm.BackendError(str(exc)) from exc
    if inc is None:
        return scope, None, []
    inc, dropped = validate_snippets(inc, text)
    return scope, inc, dropped


def single_call_enabled(proc_cfg: dict, backend: str) -> bool:
    """processing.single_call_extraction: "cli" (default) = one call per article on the
    subscription backend only; true = on every backend; false = two calls everywhere.
    Pure."""
    v = proc_cfg.get("single_call_extraction", "cli")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == backend
