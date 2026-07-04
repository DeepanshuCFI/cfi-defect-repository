# National Road-Infrastructure Defect Repository · Crashfree India

News-evidence-mined, geolocated, deduplicated, priority-ranked repository of road
infrastructure defects across India. Spec: [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) ·
Product context: [docs/PRD.md](docs/PRD.md) · Standing rules: [CLAUDE.md](CLAUDE.md).

## Setup (once)

```bash
python3 -m pip install -r requirements.txt
# Fill .env with real keys: ANTHROPIC_API_KEY, DATABASE_URL (Supabase), MAPBOX_TOKEN
# In the Supabase SQL editor, run once:  create extension postgis;
```

---

## Phase 1 — Database schema + config seed load ✅ (code complete)

**What it builds**
- `migrations/001_schema.sql` — core entities (spec §4): `source_article`, `incident`,
  `incident_defect` (evidence snippet NOT NULL — no defect without evidence),
  `incident_source`, `hotspot` (with `score_breakdown` jsonb + `escalation_candidate`),
  `review_action`. PostGIS `geography(Point)` + GiST indexes.
- `migrations/002_config_tables.sql` — config spine: `config_district`, `config_keyword`,
  `config_outlet`, `config_defect_taxonomy` (21 codes, FK-enforced on `incident_defect`),
  `config_setting` (priority weights, confidence gate, cluster epsilon — all runtime-editable).
- `config/config_defect_taxonomy.json` — §5 vocabulary + severity weights (edit here, reload).
- `config/config_settings.json` — §8/§9 thresholds & weights (must sum to 1.0; loader enforces).
- `scripts/migrate.py` — ordered, tracked migrations (`schema_migrations` table).
- `scripts/load_configs.py` — idempotent upsert of all config files into the DB.

**Run it** (needs `DATABASE_URL` in `.env`)
```bash
python3 -m scripts.migrate            # apply 001, 002
python3 -m scripts.load_configs       # upsert configs, prints row counts
```

**Verify**
```bash
python3 -m scripts.migrate --status   # both migrations listed as applied
python3 -m scripts.load_configs       # expect: config_district 722 · config_keyword 263
                                      #         config_outlet 94 · config_defect_taxonomy 21
                                      #         config_setting 11
```
Or in Supabase SQL editor: `select count(*) from config_district;` → **722**.

**Verified without a DB** (already run in-repo)
- All migration SQL parses via `pglast` (real Postgres grammar).
- `python3 -m scripts.load_configs --check` validates every config file
  (languages, tiers, taxonomy codes, weights summing to 1.0) — no DB required.

---

## Phase 2 — Ingestion (RSS + GDELT + fetch/dedup) ✅ (live-verified)

**What it builds**
- `pipeline/collectors/rss.py` — Google News RSS per district: English starter query +
  primary-language crash terms (query matrix per CLAUDE.md). Fetches feeds with a browser
  UA (Google blocks feedparser's default), resolves Google's redirector links to real
  publisher URLs via `googlenewsdecoder` (current `AU_yq…` id format).
- `pipeline/collectors/gdelt.py` — GDELT 2.0 DOC API wide net, per-language crash queries
  restricted to India; 429-aware with backoff (GDELT free tier throttles aggressively).
- `pipeline/fetch.py` — robots.txt respected (cached), per-domain rate limit, Chrome UA,
  trafilatura clean text **+ JSON-LD `articleBody` fallback** (rescues JS-rendered portals
  like Amar Ujala), 64-bit SimHash `dedup_hash`.
- `pipeline/store.py` — same interface over Postgres (`DATABASE_URL` set) or
  `data/source_article.jsonl` (no DB yet); URL dedup + 14-day near-duplicate check
  (Hamming ≤ 6).
- `pipeline/run.py` — CLI: `collect --district X | --state Y | --gdelt en,hi`,
  flags for window/limits/delay/no-fetch/jsonl.

**Run it**
```bash
python3 -m pipeline.run collect --district Supaul --days 3          # one district
python3 -m pipeline.run collect --state Bihar --days 7              # whole state
python3 -m pipeline.run collect --gdelt en,hi --timespan 1d         # GDELT net
```

**Live verification (2026-07-04, JSONL mode — no DB yet)**
- 3 districts, 3 languages: Supaul (hi), Coimbatore (ta), Pune (mr) → **18 articles
  stored, 4 languages** (hi 6 · en 6 · ta 3 · mr 3), tiers tagged (14 regional /
  3 national / 1 district), median clean-text 1,785 chars.
- Real regional crash reports landed, with locations in the text (e.g. Bhaskar: Supaul
  bike death near Baghi; Dinamani: Palladam crash; Sakal: Kolhapur divider crash) —
  including an infra story (NH-27 service road potholes, Bhimpur Chowk).
- Near-duplicate detection caught 3 repeat stories across overlapping queries.
- GDELT: collector correct per API; this network is currently 429-throttled by GDELT's
  free tier — degrades gracefully, retries next run. RSS is the primary flow.

---

## Phases 3–10 (per spec §11)

| Phase | What | Status |
|---|---|---|
| 2 | Ingestion: Google News RSS (district×keyword×language) + GDELT + outlets | ✅ code + live-verified |
| 3 | LLM relevance + forced-JSON extraction | pending |
| 4 | Geocoding (most-specific-first, confidence-scored) | pending |
| 5 | Article→incident dedup + PostGIS DBSCAN hotspots | pending |
| 6 | Priority engine + nightly recompute | pending |
| 7 | Confidence gate + review queue UI | pending |
| 8 | Public dashboard (React + Vite + Tailwind + MapLibre) | pending |
| 9 | Read-only API + corrections | pending |
| 10 | Observability / QA dashboard | pending |

First milestone (§E of the runbook): Phases 1–6 end-to-end on **one state** (Bihar proposed).
