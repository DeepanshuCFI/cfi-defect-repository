"""Relevance classifier (BUILD_SPEC §7.2) — cheap model, forced JSON via tool use.

Question: is this article about a specific road-traffic crash (or road-infrastructure
hazard/defect) in India? Drops obituaries-of-other-causes, crime, politics, generic
statistics pieces, and non-India stories before the expensive extraction step.
"""

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
    "road damage. Also OUT of scope even though they involve infrastructure: "
    "building/roof/ceiling/wall collapses, hospital/school/office premises hazards, "
    "elevators, wells, electrocutions — UNLESS the road itself is the hazard "
    "(road cave-in, open drain on a road, waterlogged carriageway). "
    "Judge only from the text given."
)


def classify(title: str | None, text: str) -> dict:
    from pipeline import llm
    content = f"TITLE: {title or '(none)'}\n\nTEXT:\n{text[:2500]}"
    return llm.structured(SYSTEM, TOOL, content,
                          model=llm.model_for("relevance"), max_tokens=200)
