-- 011: named-site watch — standing surveillance of CFI's audited junctions.
-- Any news article or geocoded incident touching a watched site surfaces as a
-- watch_hit: live evidence for authority follow-ups (a fresh crash at a site marked
-- "Fully Implemented" is grounds to reopen the complaint).

create table if not exists watch_site (
  id               bigint generated always as identity primary key,
  name             text not null unique,
  name_variants    text[] not null default '{}',   -- incl. Hindi/vernacular spellings
  city             text,
  district         text,
  state            text,
  geom             geography(Point, 4326),          -- null = name-match only
  authority        text,                            -- who the complaint is filed with
  authority_status text,                            -- sheet status at seed time
  enabled          boolean not null default true,
  created_at       timestamptz not null default now()
);

create table if not exists watch_hit (
  id          bigint generated always as identity primary key,
  site_id     bigint not null references watch_site(id),
  article_id  bigint references source_article(id),
  incident_id bigint references incident(id),
  match_kind  text not null check (match_kind in ('name', 'geo')),
  note        text,
  notified    boolean not null default false,
  created_at  timestamptz not null default now(),
  unique nulls not distinct (site_id, article_id, incident_id, match_kind)
);

-- 007 lesson: every new table ships with RLS on
alter table watch_site enable row level security;
alter table watch_hit enable row level security;
