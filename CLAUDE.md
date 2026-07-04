# CLAUDE.md — Project Memory (auto-loaded by Claude Code)
**Project:** National Road-Infrastructure Defect Repository (India) · Crashfree India

> Read `docs/BUILD_SPEC.md` (the full spec) before writing code. Read `docs/PRD.md` for product context. This file is the short version you must always obey.

## Mission
Build a public, credible, national repository of road-infrastructure defects mined from news evidence (~90% regional/vernacular, ~10% national), geolocated, deduplicated into incidents and hotspots, and ranked by a priority score so repeat-crash locations surface for government action.

## Non-negotiable principles
1. **Credibility > coverage.** Every public claim must trace to a source URL. When unsure, mark `unverified` and route to review — never fabricate a defect, location, or number.
2. **Attribution, not accusation.** Frame defects as "reported in [outlet], [date]". No blaming named individuals/officials. No proven-causation claims.
3. **Confidence gate.** Only entries passing the gate publish publicly; everything else goes to the review queue (see spec §8).
4. **Controlled vocabulary.** Defects must map to the taxonomy in the spec (§5) / `config/` — never free-text.
5. **Privacy & copyright.** No victim PII or images. Store source URL + short evidence snippet only, never full article text on the public site.

## Recommended stack (change only with a stated reason)
- **DB:** PostgreSQL + **PostGIS** (use Supabase for Postgres+PostGIS+auth+auto REST API).
- **Pipeline:** Python (FastAPI, feedparser, trafilatura, simhash, shapely, scikit-learn DBSCAN).
- **LLM extraction:** Anthropic Claude API with JSON-schema-forced structured output. Cheap model for relevance, stronger model for extraction.
- **Geocoding:** Mapbox or Google Geocoding; Nominatim fallback.
- **Frontend:** React + Vite + Tailwind + Mapbox GL / Leaflet.
- **Jobs:** cron/worker for collectors + nightly hotspot/priority recompute.

## Config files (already provided — do not regenerate)
- `config/config_districts_master.csv` — 722 districts × state × primary_language × starter query. Ingestion geographic spine.
- `config/config_keyword_pack.json` — 13-language crash/fatality/injury/crash_type/infra_defect terms.
- `config/config_regional_outlets.csv` — ~90 seed regional outlets (verify domains on first run; column `domain_verify=pending`).

## Ingestion rule
Query matrix = districts × keyword terms (English + district's primary_language). Primary sources: Google News RSS + GDELT; secondary: curated outlets. Cap national outlets at ~10% of volume. Respect robots.txt and rate limits. Cache raw HTML; dedup on URL + near-duplicate text hash.

## Pipeline order (per article → hotspot)
lang-detect → translate (keep original) → relevance classify → structured extract (JSON, taxonomy, evidence snippets, confidence) → geocode (most-specific-first + confidence) → dedup articles→incident → cluster incidents→hotspot (PostGIS DBSCAN ~150–300m) → priority score → route (public vs review).

## Priority engine (config-editable weights)
Per hotspot 0–100 from: fatalities-weighted casualties (w1=.30), crash frequency 6/12mo (w2=.25), recency decay (w3=.15), vulnerable-user share (w4=.15), defect severity (w5=.10), evidence strength (w6=.05). **Headline rule:** ≥3 incidents in 6 months → auto-flag `escalation_candidate`, top of district view. Always expose the score breakdown.

## Confidence gate (publish only if)
`extraction_confidence ≥ 0.7` AND `geocode_confidence ≥ 0.6` AND `infra_implicated = true`. Else → review queue. Reviewer approval overrides.

## Working style for this repo
- Build in thin vertical slices (spec §11). After each phase, print a short status + how to verify it.
- Never hard-code secrets. Use `.env` (`ANTHROPIC_API_KEY`, `DATABASE_URL`/`SUPABASE_URL`+`SUPABASE_KEY`, `MAPBOX_TOKEN`).
- Sample-check outputs (eyeball ≥50 extractions) before scaling ingestion.
- Keep the defect taxonomy and priority weights in `config/`, not in code.
- Write tests for: dedup logic, clustering, priority scoring, confidence gate.
