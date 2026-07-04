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

## Phases 2–10 (per spec §11)

| Phase | What | Status |
|---|---|---|
| 2 | Ingestion: Google News RSS (district×keyword×language) + GDELT + outlets | pending |
| 3 | LLM relevance + forced-JSON extraction | pending |
| 4 | Geocoding (most-specific-first, confidence-scored) | pending |
| 5 | Article→incident dedup + PostGIS DBSCAN hotspots | pending |
| 6 | Priority engine + nightly recompute | pending |
| 7 | Confidence gate + review queue UI | pending |
| 8 | Public dashboard (React + Vite + Tailwind + MapLibre) | pending |
| 9 | Read-only API + corrections | pending |
| 10 | Observability / QA dashboard | pending |

First milestone (§E of the runbook): Phases 1–6 end-to-end on **one state** (Bihar proposed).
