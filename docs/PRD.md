# PRD — National Road-Infrastructure Defect Repository
**Crashfree India · v1.0 · Internal + government-facing**

---

## 1. Problem statement
India's official road-safety data (MoRTH black spots, iRAD/eDAR) is **highway-centric**. The bulk of crashes on **city, district, urban, and rural roads** — where infrastructure defects are pervasive and cheap to fix — are **not captured** in any public, queryable dataset. Yet these crashes *are* documented daily in **regional and vernacular news**, often with precise locations (junction, curve, landmark, stretch) and the contributing defect (no signage, missing barrier, bad geometry, no lighting, potholes, waterlogging).

**Gap:** there is no credible, national, source-linked repository that (a) mines this news evidence, (b) geolocates it, (c) detects repeat-crash patterns, and (d) ranks locations for intervention.

## 2. Vision & goal
A **public, credible, evidence-linked national map** of reported road-infrastructure defects, powering **data-driven advocacy** with road-owning agencies. Success = CFI can walk into any district/state authority and say *"these 10 locations caused X crashes in the last 6 months; here are the sources; here's the likely engineering fix — act."*

## 3. Guiding principles
1. **Credibility over coverage.** Every claim traces to a source. Confidence-gated publishing. This is the product's moat.
2. **Attribution, not accusation.** Report what media reported; don't assert proven causation or blame individuals.
3. **Pattern → priority.** Raw incidents matter less than *repeat* locations. The engine exists to rank.
4. **Regional-first.** ~90% regional/vernacular sourcing; national outlets only for corroboration.
5. **Actionable output.** Everything ends in an exportable evidence pack an official can act on.

## 4. Users & jobs-to-be-done
| User | Job | Key output |
|---|---|---|
| Public / press | See defects near me, cite them | Map, source links |
| CFI advocacy | Prioritise & escalate locations | Ranked hotspots, evidence packs |
| Govt / road agency | Receive defensible fix-list | Location + defect + sources + suggested fix |
| Internal reviewer | Validate low-confidence data | Review queue, audit log |

## 5. Scope
**In scope (national, day one):** all states, all road types (NH/SH/MDR/district/urban/rural), multilingual ingestion, incident + hotspot modelling, priority scoring, public dashboard, review workflow, read-only API.
**Out of scope (v1):** physical/field audits, crowd-sourced citizen reporting (phase 2), predictive modelling, integration with iRAD/eDAR (phase 3 — pursue data-sharing MoU), real-time alerting to agencies (phase 2).

> **Trade-off flagged honestly:** "National from day one" maximises reach but raises data-quality risk across ~20+ languages and messy Indian geocoding. Mitigation is the **confidence gate** (§8) — national ingestion, but only high-confidence entries publish; the rest queue for review. This preserves credibility without narrowing scope.

## 6. Data model (summary)
`source_article → incident → incident_defect`, with `incident_source` (dedup cluster) and `hotspot` (spatial cluster). Full field list in the build prompt (§4). Non-negotiable: **no defect without an evidence snippet + source URL.**

## 7. Pipeline (news → ranked hotspot)
1. **Ingest** — Google News RSS (district × keyword × language), GDELT, curated regional outlets, capped national. Clean text via trafilatura.
2. **Classify** — is it an Indian road crash? (cheap model).
3. **Extract** — forced-JSON structured extraction: location, casualties, road type, defects (controlled taxonomy), confidence, evidence snippets.
4. **Translate** — vernacular → English (original retained for evidence).
5. **Geocode** — most-specific-first; method + confidence recorded; snap to road.
6. **Deduplicate** — articles → single incident.
7. **Cluster** — incidents → hotspot (PostGIS DBSCAN, ~150–300 m).
8. **Score & route** — priority score; confidence gate to public vs review.

## 8. Confidence gate (credibility control)
Public only if `extraction_confidence ≥ 0.7`, `geocode_confidence ≥ 0.6`, `infra_implicated = true`. Else → review queue with approve/edit/reject (audit-logged). Reviewer approval overrides.

## 9. Priority engine
Per-hotspot 0–100 score from: fatalities-weighted casualties, crash frequency (6/12 mo), recency decay, vulnerable-user share, defect severity, evidence strength. Config-editable weights. **Headline rule:** ≥3 incidents in 6 months → auto-flag as escalation candidate. Output tiers: Critical / High / Medium / Watch, with a transparent score breakdown (agencies will demand the "why").

## 10. Dashboard requirements
Map-first with priority-coloured clustering; filters (geo hierarchy, road type, defect, date, severity, repeat-only); hotspot & incident detail with source links and score breakdown; national + state rankings; CSV/PDF evidence-pack export; correction workflow; trust UI with disclaimers; read-only public API.

## 11. Metrics of success
- **Coverage:** # districts with ≥1 verified hotspot; regional-source share ≥ 85%.
- **Quality:** extraction precision (sampled ≥ 90% on relevance; ≥ 80% on defect tagging); geocode-confidence distribution; dedup accuracy.
- **Impact (north star):** # hotspots escalated to authorities; # acknowledged; # marked *fixed*.
- **Trust:** correction rate < 5%; median sources per public entry ≥ 2.

## 12. Risks & mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| Vernacular extraction errors | Wrong defects/locations | Translate-then-extract; confidence gate; human review; per-language sampling QA |
| Geocoding imprecision | Wrong pins, lost trust | Most-specific-first; confidence score; centroid-only → review, labelled approximate |
| False causation (crash blamed on infra wrongly) | Credibility / defamation | "Reported" framing; evidence snippet required; `no_infrastructure_defect_identified` option |
| Duplicate/inflated counts | Skewed priority | Article→incident dedup before counting |
| LLM cost at national scale | Budget blowout | Keyword pre-filter; cheap-model relevance; cache; process-once |
| Copyright / privacy | Legal exposure | Store snippets + links only (not full text); no victim PII/images |
| Govt dismisses "media data" | Advocacy fails | Transparent scoring, multi-source corroboration, offer joint field verification |
| Source bias (better-covered districts look worse) | Misleading geography | Report incident *rates* & note coverage bias; treat absence ≠ safety |

## 13. Roadmap
- **Phase 1 (MVP, national ingestion + gated publish):** pipeline, hotspots, priority engine, public dashboard, review queue, API.
- **Phase 2:** citizen correction/reporting, agency alerting, per-agency dashboards, WhatsApp/press digests, suggested-fix library mapped to defect taxonomy.
- **Phase 3:** MoU-based integration with iRAD/eDAR/MoRTH; before-after tracking of fixed sites; impact reporting (crashes averted).

## 14. Government engagement model (how the data creates change)
1. Rank hotspots per district → 2. generate evidence pack (locations, sources, defect, suggested Safe-System fix) → 3. submit to road-owning agency + District Road Safety Committee → 4. track status (escalated → acknowledged → fixed) inside the tool → 5. publish before-after wins to build pressure and trust. Anchor recommendations in the **Safe System / Vision Zero** framing agencies already recognise.

---
*Prepared for Crashfree India. Companion to the Claude Code build prompt. Build for credibility first; let the pattern engine drive the advocacy.*
