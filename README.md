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

## Phase 3 — Relevance + structured extraction ✅ (live-verified)

**What it builds**
- `pipeline/processing/relevance.py` — Haiku 4.5 tool-forced classifier: specific Indian
  road crash OR road-infra defect/hazard → in scope; crime/suicide/weather/politics → out.
- `pipeline/processing/extract.py` — Sonnet 5 tool-forced extraction to the full §7.3
  schema. **Taxonomy locked by JSON-schema enum** (free-text defects impossible).
  Post-validation: evidence snippets must be **verbatim substrings** of the article
  (non-verbatim → dropped + logged), defect types deduped,
  `no_infrastructure_defect_identified` cannot co-exist with real defects,
  `infra_implicated` recomputed if defects drop. Translation folded into the call
  (stated Phase-0 deviation) — English fields out, original-language snippets kept.
- `pipeline/run.py process --limit N` — fetched → relevant/irrelevant → extracted/failed.
- `scripts/eyeball_extractions.py [--n 50] [--infra-only]` — side-by-side article vs
  extraction QA view (the CLAUDE.md ≥50 sample-check before scaling).

**Live verification (2026-07-04, 15 articles: hi/ta/mr/en)**
- 10 incidents extracted · 5 correctly rejected (suicide, sexual-assault, flood, crime,
  a signal-installation story) · 0 non-verbatim snippets.
- Multilingual: Hindi NH-27 service-road story → 3 defects (potholes, waterlogging,
  work zone) each with verbatim Hindi evidence, conf 0.85, infra=true. Tamil The-Hindu
  road-widening plea → 5 defect claims incl. blind curve + untreated blackspot.
- Honesty checks pass: casualties only when stated; old crash honestly dated 2023 (not
  faked recent); pure-behaviour crashes set `no_infrastructure_defect_identified` +
  infra=false — the confidence gate will route conf<0.7 items to review as designed.

---

## Phase 4 — Geocoding ✅ (live-verified)

**What it builds**
- `pipeline/processing/geocode.py` — most-specific-first ladder (§7.4): coords-in-text
  0.95 → full location string 0.80 → **leading landmark segment + district/state 0.80**
  → trimmed/road+city 0.70 → city centroid 0.50 → district centroid 0.40. Every result
  sanity-checked against the India bbox and the expected state (homonym guard).
  Nominatim (free, 1 req/s etiquette, contact UA) + persistent file cache;
  Mapbox drops in behind the same interface when the token lands.
- `pipeline/run.py geocode [--limit N]` — fills `geom`/`geocode_confidence`/
  `geocode_method` (PostGIS `ST_GeogFromText` in DB mode).

**Live verification (10 incidents)**
- 10/10 resolved. Distribution: landmark 0.8 ×2 · road 0.7 ×5 · city 0.5 ×1 ·
  district-centroid 0.4 ×2.
- Spot-checks: Garware bridge → JM Road, Pune (exact); Narkatiaganj (exact town);
  **Nashik Phata → the actual junction in Pimpri-Chinchwad** (the landmark-first variant
  fixed a 50 km road-level miss found during testing).
- Honesty holds: unresolvable spots land at 0.4 centroid — below the 0.6 publish gate,
  exactly as designed.

---

## Phases 5+6 — Dedup, hotspot clustering, priority engine ✅ (live-verified on Supabase)

**Database is live.** Migrations applied to Supabase (Postgres 17.6 + PostGIS);
722 districts + all configs loaded; pilot data imported (18 articles · 10 incidents ·
17 defects after consistency cleanup).

**What it builds**
- `pipeline/processing/dedup.py` — same-crash merge (§7.5): date ±1d AND ≤1 km AND
  compatible casualties; undated/ungeocoded never auto-merge; merges keep the higher-
  confidence incident, move sources/defects, audit-log to `review_action`.
- `pipeline/processing/cluster.py` — `ST_ClusterDBSCAN` hotspots (§7.6), eps from config
  (mean 231 m). **Only geocode_confidence ≥ 0.5 clusters** — district centroids would
  manufacture phantom hotspots. Stable rebuilds: hotspot keeps id/status if its new
  centroid lands within 300 m.
- `pipeline/processing/score.py` — §9 engine: 6 weighted components, saturating
  normalisations, full raw+component+weights breakdown stored as jsonb; tiers
  Critical/High/Medium/Watch; **≥3 incidents in 6 months → escalation_candidate**.
- `pipeline/processing/gate.py` — §8 publish/review router (Phase 7 UI wraps this).
- `pipeline/run.py recompute` — the nightly job: dedup → cluster → score → top-15 print.
- `tests/test_core_logic.py` — 17 tests: dedup rules, score bounds/monotonicity/decay,
  repeat-crash-outranks-single, tiers, all gate branches. `python3 -m pytest tests/ -q`

**Live verification**
- `recompute` on real data: 0 false merges (all 10 incidents genuinely distinct),
  8 hotspots (2 centroid-level incidents correctly excluded), scored 9.2–37.1 —
  Trichy SH30 (4 deaths, blind curve + untreated blackspot) correctly ranks #1;
  no false escalation flags on single-incident hotspots.
- Nightly cron: `python3 -m pipeline.run recompute` (schedule after ingestion).

---

## Phase 7 — Confidence gate + review queue ✅ (live-verified)

**What it builds**
- `migrations/003_public_views.sql` + `004_…` — the §8 gate materialised as SQL views:
  `public_incident` (gate passes on stored confidences OR reviewer override; rejected/
  disputed never show), `public_incident_defect`, `public_hotspot` (public members only —
  the public site never cites a number it can't source), `review_queue` (with per-row
  failure reason). Phases 8/9 read ONLY these views.
- `review/app.py` — FastAPI internal UI (`python3 -m uvicorn review.app:app --port 8600`):
  queue cards with evidence snippets + source links, **approve / reject / edit / merge /
  split**, every action audit-logged to `review_action`. Approve overrides the gate.

**Live verification**
- Routing before review: 3 auto-public · 6 queued (each with the correct reason) —
  found & fixed a real bug: `real`-typed 0.7 fails `>= 0.7` by float precision, which
  wrongly queued 3 threshold incidents (`004` migration).
- Workflow: approved the NH-27 Supaul infra story → became public + hotspot went public
  + `review_action` row written; rejected a behaviour-only crash → left queue, never
  publishes. 17/17 tests still pass.

---

## Phases 8–10 (per spec §11)

| Phase | What | Status |
|---|---|---|
| 2 | Ingestion: Google News RSS (district×keyword×language) + GDELT + outlets | ✅ code + live-verified |
| 3 | LLM relevance + forced-JSON extraction | ✅ code + live-verified |
| 4 | Geocoding (most-specific-first, confidence-scored) | ✅ code + live-verified |
| 5 | Article→incident dedup + PostGIS DBSCAN hotspots | ✅ code + live-verified |
| 6 | Priority engine + nightly recompute | ✅ code + live-verified |
| 7 | Confidence gate + review queue UI | ✅ code + live-verified |
| 8 | Public dashboard (React + Vite + Tailwind + MapLibre) | pending |
| 9 | Read-only API + corrections | pending |
| 10 | Observability / QA dashboard | pending |

First milestone (§E of the runbook): Phases 1–6 end-to-end on **one state** (Bihar proposed).
