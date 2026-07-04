# BUILD PROMPT — National Road-Infrastructure Defect Repository (India)
**Paste this into Claude Code as the founding brief. It is written as an "organize-thoughts" spec: read top-to-bottom, then build in the order given in §11.**

---

## 0. One-line mandate
Build a **public, credible, national repository of road-infrastructure defects** in India, mined from **news evidence** (≈90% regional/vernacular, ≈10% national), geolocated as precisely as possible, deduplicated into incidents and hotspots, and ranked by a **priority score** so that repeat-crash locations surface for government action.

## 1. Why this exists (context you must respect while building)
- Government black-spot data covers mainly **National Highways**. The majority of crashes on **city, district, rural, and urban roads are invisible** in official datasets.
- News reports — especially **district-level and vernacular outlets** — routinely name the exact stretch, junction, curve, or landmark of a crash and often the contributing infrastructure defect (no signage, missing barrier, bad geometry, potholes, no lighting, etc.).
- **The core value of this product is credibility.** Every entry must be traceable to a source. When in doubt, label as "reported / unverified", never invent. This repository will be used in conversations with government — a single fabricated defect destroys trust. Optimize the whole system for *auditability over volume*.

## 2. Users & primary jobs
1. **Public / journalists** — browse a map, see defects near them, cite sources.
2. **CFI advocacy team (internal)** — pull a ranked list of priority hotspots by state/district to take to authorities; export evidence packs.
3. **Government / road-owning agencies** — receive a defensible, evidence-linked list of locations needing engineering fixes.
4. **Reviewers (internal ops)** — validate low-confidence extractions before they go public.

## 3. Scope decision (locked): National from day one — with a confidence gate
Build architecture for **all-India, all road types, multilingual** immediately.
**Risk mitigation (mandatory, non-negotiable):** only entries above a confidence threshold publish to the **public** view. Everything else lands in a **review queue**. This lets us go national without publishing garbage. (See §8, §10.)

## 4. Core data model (PostgreSQL + PostGIS)
Design these entities. Use PostGIS `geography(Point)` for coordinates and spatial clustering.

**`source_article`**
`id, url (unique), outlet_name, outlet_tier (national|regional|district|aggregator), language, state, district, published_at, fetched_at, raw_html, clean_text, translated_text, dedup_hash, processing_status`

**`incident`** — one real-world crash event (may be cited by many articles)
`id, crash_date, crash_time, location_text_raw, location_text_best, road_name, road_type (NH|SH|MDR|district|urban_arterial|urban_local|rural|unknown), admin_state, admin_district, admin_city, admin_ward, geom (Point), geocode_confidence (0-1), geocode_method, fatalities (int), injuries (int), vehicles_involved (text[]), victim_types (text[]: pedestrian|two_wheeler|cyclist|car|truck|bus|auto), narrative_summary, infra_implicated (bool), extraction_confidence (0-1), verification_status (auto|reviewed|verified|disputed|rejected), primary_source_id, cluster_id`

**`incident_defect`** — many-to-one to incident; tagged from the taxonomy in §5
`id, incident_id, defect_type, defect_confidence (0-1), evidence_snippet, evidence_source_id`

**`incident_source`** — join table linking an incident to all articles reporting it (dedup cluster)
`incident_id, source_article_id, match_confidence`

**`hotspot`** — spatial cluster of incidents at ~one location/stretch
`id, centroid_geom, road_name, admin_state, admin_district, admin_city, incident_count, fatality_count, injury_count, first_crash_date, last_crash_date, dominant_defects (text[]), priority_score (0-100), status (new|monitoring|escalated_to_govt|acknowledged|fixed|disputed), last_recomputed_at`

**`review_action`** — audit log
`id, entity_type, entity_id, reviewer, action (approve|edit|reject|merge|split), before_json, after_json, note, created_at`

> Every public claim must be reconstructable from `incident → incident_source → source_article.url`. Never store a defect without an `evidence_snippet` and source id.

## 5. Infrastructure-defect taxonomy (controlled vocabulary — extraction must map to these)
Do **not** let the LLM invent free-text defect types. Force it to choose from:
- `no_or_faded_road_markings`
- `missing_or_inadequate_signage`
- `no_street_lighting`
- `unmarked_or_illegal_speed_breaker`
- `potholes_or_broken_surface`
- `sharp_or_blind_curve_geometry`
- `missing_crash_barrier_or_guardrail`
- `unsafe_junction_or_intersection_design`
- `missing_pedestrian_crossing_or_FOB`
- `missing_footpath_or_encroached_footpath`
- `median_gap_or_illegal_U_turn`
- `no_service_road_direct_access`
- `waterlogging_or_poor_drainage`
- `unsafe_construction_or_work_zone`
- `missing_rumble_strips`
- `poor_visibility_vegetation_or_obstruction`
- `unprotected_railway_or_level_crossing`
- `steep_gradient_or_missing_runaway_provision`
- `known_blackspot_untreated`
- `other_infrastructure` (requires reviewer confirmation before publishing)
- `no_infrastructure_defect_identified` (crash driven by behaviour/vehicle only — store but exclude from defect map)

## 6. Ingestion layer (the "get the news" engine)
Build modular collectors; run on a schedule (cron / GitHub Actions / a worker). Sources, in priority order:
1. **Google News RSS queries** — programmatic per **district × keyword × language**. Build a config table of ~700 districts and generate queries like `"<district> road accident"`, plus vernacular keywords: Hindi `सड़क दुर्घटना / हादसा`, Marathi `अपघात`, Tamil `விபத்து`, Telugu `ప్రమాదం`, Bengali `দুর্ঘটনা`, Kannada `ಅಪಘಾತ`, Gujarati `અકસ્માત`. (Store the keyword pack in config so it's editable.)
2. **GDELT 2.0** (free, global news index with geolocation) — filter to India + road-accident themes as a wide net.
3. **Curated regional-outlet list** — maintain a seed table of 200–400 regional/district publications; fetch their local/crime/accident sections.
4. **National outlets (cap at ~10% of volume)** — TOI, HT, The Hindu, Indian Express, etc. for corroboration.
- Article extraction: use `trafilatura` (preferred) or `newspaper3k` to get clean text.
- Respect `robots.txt` and rate limits; cache raw HTML; dedup on URL + near-duplicate text hash (SimHash/MinHash).
- **Do not** build brittle per-site scrapers first — start with RSS + GDELT for breadth, add custom scrapers only for high-value outlets.

## 7. Processing pipeline (per article → structured incident)
Chain of steps; each writes state so failures are resumable:
1. **Language detect** → if not English, **translate** to English (store both). Keep original for evidence snippets.
2. **Relevance classifier (LLM):** Is this a road-traffic crash in India? (yes/no). Drop non-crashes.
3. **Structured extraction (LLM, forced JSON schema):** pull `crash_date, location_text_best, road_name, road_type, admin_* , fatalities, injuries, victim_types, vehicles, narrative_summary, infra_implicated (bool), defects[] (from §5 taxonomy, each with evidence_snippet + confidence), extraction_confidence`.
   - Prompt rule: *only tag a defect if the article's own words support it; attach the exact snippet as evidence; if the cause is purely driver/vehicle behaviour, set `no_infrastructure_defect_identified`.*
4. **Geocoding:** resolve `location_text_best` → lat/long + `geocode_confidence`.
   - Try in order: exact coordinates in text → named landmark/junction + district (Google/Mapbox Geocoding or Nominatim) → road + city → city/district centroid (low confidence). Store which method was used.
   - Snap to nearest road where possible. Never publish a defect on a city-centroid-only geocode to the public map — send to review.
5. **Deduplicate → incident:** cluster articles describing the **same crash** (same date ±1 day, spatial proximity, similar casualties/narrative). One incident, many sources.
6. **Cluster → hotspot:** group incidents within ~150–300 m on the same road (DBSCAN on PostGIS, tune epsilon per road_type). Recompute hotspot stats.
7. **Score** (see §9) and **route**: high confidence → public; else → review queue.

## 8. Confidence gating (protects credibility)
Publish to public view **only if**: `extraction_confidence ≥ 0.7` AND `geocode_confidence ≥ 0.6` AND `infra_implicated = true` AND defect ≠ `other_infrastructure` unless reviewed. Everything else → **review queue** with a one-click approve/edit/reject UI writing to `review_action`. Reviewer approval overrides the gate.

## 9. Pattern & priority engine (the "what to fix first" brain)
This is the strategic payoff. Compute per **hotspot**, recomputed nightly:

```
priority_score (0–100) = normalize(
      w1 * fatalities_weighted        // fatalities ×5, grievous injuries ×2, minor ×1
    + w2 * crash_frequency            // incidents in trailing 6 & 12 months
    + w3 * recency                    // exponential decay, recent crashes weigh more
    + w4 * vulnerable_user_share      // pedestrian/cyclist/2-wheeler share of casualties
    + w5 * defect_severity            // taxonomy-weighted (e.g., missing barrier on curve = high)
    + w6 * evidence_strength          // #independent sources, geocode confidence
)
```
- Default weights (make them **config-editable**, not hard-coded): w1=0.30, w2=0.25, w3=0.15, w4=0.15, w5=0.10, w6=0.05.
- **Repeat-crash rule (headline feature):** flag any hotspot with **≥3 incidents in 6 months** as `escalated_candidate` and surface at top of the district view — this is the pattern the founder cares about most.
- Produce tiers: **Critical / High / Medium / Watch**. Expose the score breakdown (never a black box — govt will ask "why this road").

## 10. Public dashboard (frontend)
- **Map-first** (Leaflet or Mapbox GL): clustered pins coloured by priority tier; heat layer toggle.
- **Filters:** state → district → city; road_type; defect_type; date range; casualty severity; "repeat hotspots only".
- **Hotspot detail:** map, crash timeline, casualty totals, dominant defects with evidence snippets, **all source links**, priority-score breakdown, status badge, "Report a correction" button.
- **Incident detail:** narrative, date, location, defects+snippets, every source article link.
- **Rankings page:** national + per-state "Top priority locations" leaderboard; CSV/PDF **evidence-pack export** for govt submissions.
- **Trust UI:** every entry shows `verification_status`, source count, and last-updated. Prominent disclaimer: *"Defects are as reported in news media; locations are indicative and pending physical audit."*
- **Public API (read-only)** for researchers/RTI/press.

## 11. Build order (do it in this sequence — ship thin vertical slices)
1. **DB + PostGIS schema** (§4) + seed config: districts table, keyword packs (§6), outlet list.
2. **Ingestion MVP:** Google News RSS + GDELT collectors → `source_article`. Verify you're pulling real regional articles.
3. **Processing MVP:** relevance classifier + structured extraction (§7.1–7.3) with forced JSON schema → `incident` + `incident_defect`. Manually eyeball 50 outputs for accuracy.
4. **Geocoding** (§7.4) + confidence scoring.
5. **Dedup + hotspot clustering** (§7.5–7.6).
6. **Priority engine** (§9) + nightly recompute job.
7. **Confidence gate + review queue UI** (§8).
8. **Public dashboard** (§10): map → filters → detail → rankings → export.
9. **Read-only API + correction workflow.**
10. **Observability:** ingestion volume, extraction accuracy sampling, geocode-confidence distribution, dedup rate. Add an internal QA dashboard.

## 12. Tech stack (recommended defaults — swap only with reason)
- **DB/Backend:** PostgreSQL + **PostGIS**; consider **Supabase** (Postgres + PostGIS + auth + auto REST API) to cut build time.
- **Pipeline:** Python (FastAPI for API; `feedparser`, `trafilatura`, `simhash`, `shapely`, `scikit-learn` DBSCAN).
- **LLM extraction:** Claude API with **tool/JSON-schema-forced structured output**; batch for cost.
- **Geocoding:** Mapbox or Google Geocoding for accuracy; Nominatim as free fallback.
- **Frontend:** React + Vite + Tailwind + **Mapbox GL / Leaflet**.
- **Jobs/scheduling:** cron worker or GitHub Actions for collectors + nightly recompute.
- **Hosting:** Railway/Render/Fly for services; Vercel/Netlify for frontend.

## 13. Cost & scale controls (build these in from day 1)
- LLM is the main cost driver. **Pre-filter with cheap rules/keywords** before calling the model; use a **small/cheap model for relevance**, a stronger model only for extraction on relevant articles.
- Cache translations and geocodes; never re-process an unchanged article (dedup_hash).
- Rate-limit collectors; store raw so you can re-run extraction without re-fetching.

## 14. Legal, ethical & credibility guardrails (mandatory)
- **Attribution, not accusation:** frame every defect as *"reported in [outlet], [date]"*, link the source; avoid asserting proven causation or blaming named individuals/officials.
- **Victim privacy:** do not publish victim names, graphic detail, or images; store only what's needed for location + severity.
- **Copyright:** store headline, source URL, and a short evidence snippet — **not** full article text on the public site.
- **Corrections:** public "Report a correction" → review queue; visible `disputed` status.
- **No false precision:** show geocode confidence; label centroid-level locations as approximate.
- **Auditability:** every public number must trace to sources via the data model.

## 15. Definition of done (MVP)
- Collectors pull ≥ regional-dominant article flow across multiple states/languages daily.
- Pipeline produces geolocated, source-linked, defect-tagged incidents with confidence scores.
- Hotspots cluster correctly; repeat-crash (≥3/6mo) locations auto-flag.
- Public map + filters + rankings + evidence export live; only confidence-gated entries public; review queue functioning.
- Every public entry is click-through traceable to its news sources.

---
*Owner: Crashfree India. Build for credibility first, coverage second. When accuracy and volume conflict, choose accuracy.*
