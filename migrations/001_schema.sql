-- 001_schema.sql — core entities (BUILD_SPEC §4)
-- Idempotent: safe to re-run. Requires: create extension postgis (once, superuser/Supabase SQL editor).

create extension if not exists postgis;

-- ---------------------------------------------------------------- source_article
create table if not exists source_article (
  id                 bigint generated always as identity primary key,
  url                text not null unique,
  outlet_name        text,
  outlet_tier        text check (outlet_tier in ('national','regional','district','aggregator')),
  language           text,                       -- ISO-ish code: hi, ta, te, en …
  state              text,                       -- ingestion hint (district page / query origin)
  district           text,
  published_at       timestamptz,
  fetched_at         timestamptz not null default now(),
  raw_html           text,
  clean_text         text,
  translated_text    text,
  dedup_hash         text,                       -- simhash hex of clean_text
  processing_status  text not null default 'new'
    check (processing_status in
      ('new','fetched','irrelevant','relevant','extracted','failed','near_duplicate')),
  created_at         timestamptz not null default now()
);
create index if not exists idx_source_article_status on source_article (processing_status);
create index if not exists idx_source_article_dedup  on source_article (dedup_hash);
create index if not exists idx_source_article_pub    on source_article (published_at);

-- ---------------------------------------------------------------- incident
create table if not exists incident (
  id                    bigint generated always as identity primary key,
  crash_date            date,
  crash_time            time,
  location_text_raw     text,
  location_text_best    text,
  road_name             text,
  road_type             text not null default 'unknown'
    check (road_type in ('NH','SH','MDR','district','urban_arterial','urban_local','rural','unknown')),
  admin_state           text,
  admin_district        text,
  admin_city            text,
  admin_ward            text,
  geom                  geography(Point, 4326),
  geocode_confidence    real check (geocode_confidence between 0 and 1),
  geocode_method        text
    check (geocode_method in
      ('coords_in_text','landmark_district','road_city','city_centroid','district_centroid','manual')),
  fatalities            integer not null default 0 check (fatalities >= 0),
  injuries              integer not null default 0 check (injuries >= 0),
  vehicles_involved     text[] not null default '{}',
  victim_types          text[] not null default '{}',   -- pedestrian|two_wheeler|cyclist|car|truck|bus|auto
  narrative_summary     text,
  infra_implicated      boolean not null default false,
  extraction_confidence real check (extraction_confidence between 0 and 1),
  verification_status   text not null default 'auto'
    check (verification_status in ('auto','reviewed','verified','disputed','rejected')),
  primary_source_id     bigint references source_article(id),
  cluster_id            bigint,                          -- FK to hotspot added in 003 (created later)
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
create index if not exists idx_incident_geom     on incident using gist (geom);
create index if not exists idx_incident_date     on incident (crash_date);
create index if not exists idx_incident_admin    on incident (admin_state, admin_district);
create index if not exists idx_incident_cluster  on incident (cluster_id);

-- ---------------------------------------------------------------- incident_defect
create table if not exists incident_defect (
  id                 bigint generated always as identity primary key,
  incident_id        bigint not null references incident(id) on delete cascade,
  defect_type        text not null,                       -- FK to config_defect_taxonomy in 002
  defect_confidence  real check (defect_confidence between 0 and 1),
  -- Non-negotiable (spec §4): never a defect without its evidence
  evidence_snippet   text not null check (length(trim(evidence_snippet)) > 0),
  evidence_source_id bigint not null references source_article(id),
  created_at         timestamptz not null default now()
);
create index if not exists idx_incident_defect_incident on incident_defect (incident_id);
create index if not exists idx_incident_defect_type     on incident_defect (defect_type);

-- ---------------------------------------------------------------- incident_source
create table if not exists incident_source (
  incident_id        bigint not null references incident(id) on delete cascade,
  source_article_id  bigint not null references source_article(id) on delete cascade,
  match_confidence   real check (match_confidence between 0 and 1),
  created_at         timestamptz not null default now(),
  primary key (incident_id, source_article_id)
);

-- ---------------------------------------------------------------- hotspot
create table if not exists hotspot (
  id                 bigint generated always as identity primary key,
  centroid_geom      geography(Point, 4326),
  road_name          text,
  admin_state        text,
  admin_district     text,
  admin_city         text,
  incident_count     integer not null default 0,
  fatality_count     integer not null default 0,
  injury_count       integer not null default 0,
  first_crash_date   date,
  last_crash_date    date,
  dominant_defects   text[] not null default '{}',
  priority_score     real check (priority_score between 0 and 100),
  score_breakdown    jsonb,                                -- transparency: never a black box (§9)
  escalation_candidate boolean not null default false,     -- ≥3 incidents in 6 months
  status             text not null default 'new'
    check (status in ('new','monitoring','escalated_to_govt','acknowledged','fixed','disputed')),
  last_recomputed_at timestamptz,
  created_at         timestamptz not null default now()
);
create index if not exists idx_hotspot_geom  on hotspot using gist (centroid_geom);
create index if not exists idx_hotspot_admin on hotspot (admin_state, admin_district);
create index if not exists idx_hotspot_score on hotspot (priority_score desc);

-- incident.cluster_id -> hotspot (added after both tables exist)
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'fk_incident_cluster') then
    alter table incident
      add constraint fk_incident_cluster
      foreign key (cluster_id) references hotspot(id) on delete set null;
  end if;
end $$;

-- ---------------------------------------------------------------- review_action (audit log)
create table if not exists review_action (
  id           bigint generated always as identity primary key,
  entity_type  text not null check (entity_type in ('source_article','incident','incident_defect','hotspot')),
  entity_id    bigint not null,
  reviewer     text not null,
  action       text not null check (action in ('approve','edit','reject','merge','split')),
  before_json  jsonb,
  after_json   jsonb,
  note         text,
  created_at   timestamptz not null default now()
);
create index if not exists idx_review_action_entity on review_action (entity_type, entity_id);
