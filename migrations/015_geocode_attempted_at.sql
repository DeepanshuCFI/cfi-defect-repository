-- 015: geocode_attempted_at — spend the daily geocode budget on rows never tried first.
--
-- cmd_geocode took `where geom is null limit 1000` with no order. Once a hard tail of
-- unlocatable rows exists (~3,000 after the 13–14 Oct 2026 backlog passes), each run
-- re-tries mostly that tail (~50 located per 1,000) while fresh incidents get ~1-in-3
-- odds per run. The marker is stamped on every attempt, hit or miss; the pending and
-- rescue queries order by it: never-attempted first (newest first), then the
-- least-recently attempted — the tail is still revisited, just after the new work.
alter table incident add column if not exists geocode_attempted_at timestamptz;

-- the pending query's exact order, restricted to the rows it scans
create index if not exists idx_incident_geocode_pending
  on incident (geocode_attempted_at nulls first, id desc) where geom is null;

-- backfill: every row currently without geom has been tried at least once (daily CI
-- runs plus the Mac passes of 13–14 Oct), so it joins the back of the queue, not the front
update incident set geocode_attempted_at = now() where geom is null;
