"""Relevance classifier (BUILD_SPEC §7.2) — cheap model, forced JSON via tool use.

Question: is this article about a specific road-traffic crash (or road-infrastructure
hazard/defect) in India? Drops obituaries-of-other-causes, crime, politics, generic
statistics pieces, and non-India stories before the expensive extraction step.
"""
import anthropic

from pipeline import configload
from pipeline.settings import ANTHROPIC_API_KEY

_client: anthropic.Anthropic | None = None

TOOL = {
    "name": "record_classification",
    "description": "Record whether the article is in scope.",
    "input_schema": {
        "type": "object",
        "properties": {
            "in_scope": {
                "type": "boolean",
                "description": ("true only if the article reports a specific road-traffic "
                                "crash in India, OR a specific road-infrastructure hazard/"
                                "defect in India (potholes, missing signage/lighting/barrier, "
                                "dangerous stretch, residents demanding road safety fixes)."),
            },
            "kind": {
                "type": "string",
                "enum": ["crash", "infra_defect", "both", "out_of_scope"],
            },
            "reason": {"type": "string", "description": "≤15 words"},
        },
        "required": ["in_scope", "kind", "reason"],
    },
}

SYSTEM = (
    "You screen Indian news articles for a road-safety evidence registry. "
    "In scope: a specific road crash in India, or a specific road-infrastructure "
    "defect/hazard in India. Out of scope: crashes outside India, crime/violence, "
    "suicides, rail/air/boat accidents (unless at a road level crossing), pure "
    "statistics/policy pieces with no specific location, weather stories without "
    "road damage. Judge only from the text given."
)


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def classify(title: str | None, text: str) -> dict:
    model = configload.settings()["models"]["relevance"]
    content = f"TITLE: {title or '(none)'}\n\nTEXT:\n{text[:2500]}"
    msg = client().messages.create(
        model=model, max_tokens=200, system=SYSTEM,
        tools=[TOOL], tool_choice={"type": "tool", "name": "record_classification"},
        messages=[{"role": "user", "content": content}])
    from pipeline import llmcost
    llmcost.add(model, msg.usage)
    for block in msg.content:
        if block.type == "tool_use":
            return dict(block.input)
    return {"in_scope": False, "kind": "out_of_scope", "reason": "no tool output"}
