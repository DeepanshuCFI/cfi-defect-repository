-- 002_config_tables.sql — config spine loaded from config/ (BUILD_SPEC §6, CLAUDE.md)
-- Idempotent. Config lives in the DB so the pipeline queries it; files in config/ stay the
-- editable source of truth and scripts/load_configs.py upserts them.

-- ---------------------------------------------------------------- districts (722)
create table if not exists config_district (
  id                bigint generated always as identity primary key,
  district          text not null,
  state             text not null,
  primary_language  text not null,          -- hi|en|te|gu|mr|ta|as|kn|or|bn|ml|ur|pa
  query_name        text not null,          -- name form used inside search queries
  starter_query_en  text not null,
  enabled           boolean not null default true,
  unique (district, state)
);
create index if not exists idx_config_district_state on config_district (state);
create index if not exists idx_config_district_lang  on config_district (primary_language);

-- ---------------------------------------------------------------- keyword pack (13 languages)
create table if not exists config_keyword (
  id        bigint generated always as identity primary key,
  language  text not null,                  -- hi, ta, …
  category  text not null
    check (category in ('crash','fatality','injury','crash_type','infra_defect')),
  term      text not null,
  enabled   boolean not null default true,
  unique (language, category, term)
);
create index if not exists idx_config_keyword_lang on config_keyword (language, category);

-- ---------------------------------------------------------------- outlets (94 seeds)
create table if not exists config_outlet (
  id             bigint generated always as identity primary key,
  name           text not null unique,
  language       text,
  region_state   text,
  tier           text check (tier in ('national','regional','district','aggregator')),
  website        text,
  domain_verify  text not null default 'pending'
    check (domain_verify in ('pending','verified','unreachable','blocked')),
  coverage_notes text,
  enabled        boolean not null default true
);

-- ---------------------------------------------------------------- defect taxonomy (§5 — controlled vocabulary)
create table if not exists config_defect_taxonomy (
  code             text primary key,
  label            text not null,
  severity_weight  real not null default 3 check (severity_weight between 1 and 5),
  requires_review  boolean not null default false,   -- other_infrastructure gate (§8)
  maps_to_defects  boolean not null default true,    -- false: no_infrastructure_defect_identified
  enabled          boolean not null default true
);

-- taxonomy FK now that the target exists
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'fk_incident_defect_taxonomy') then
    alter table incident_defect
      add constraint fk_incident_defect_taxonomy
      foreign key (defect_type) references config_defect_taxonomy(code);
  end if;
end $$;

-- ---------------------------------------------------------------- settings (priority weights, gates, cluster epsilon…)
create table if not exists config_setting (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);
