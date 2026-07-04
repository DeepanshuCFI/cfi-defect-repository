"""Confidence gate (BUILD_SPEC §8) — the publish/review router. Pure logic here;
Phase 7 builds the review-queue UI around it."""


def publishable(extraction_confidence: float | None, geocode_confidence: float | None,
                infra_implicated: bool, defect_types: list[str],
                gate_cfg: dict, reviewed_approved: bool = False) -> tuple[bool, str]:
    """-> (publish?, reason). Reviewer approval overrides the gate."""
    if reviewed_approved:
        return True, "reviewer_approved"
    if (extraction_confidence or 0) < gate_cfg["extraction_confidence_min"]:
        return False, "extraction_confidence_below_min"
    if (geocode_confidence or 0) < gate_cfg["geocode_confidence_min"]:
        return False, "geocode_confidence_below_min"
    if gate_cfg.get("require_infra_implicated") and not infra_implicated:
        return False, "infra_not_implicated"
    real = [d for d in defect_types if d != "no_infrastructure_defect_identified"]
    if real and all(d == "other_infrastructure" for d in real):
        return False, "other_infrastructure_needs_review"
    return True, "gate_passed"
