-- 018: geocode_precision + geocode_match — what the name evidence behind a pin supports.
--
-- 14 Nov 2026 audit: 26 of 40 sampled public pins sat more than 10 km from the place the
-- article names. The provider, asked for a village road it does not know, answers with a
-- street called "Road" in the district town; state and district match, so the pin
-- published at 0.8. From 16 Nov a candidate must share a distinctive name with the
-- stated location (pipeline/processing/placematch.py), and WHAT matched is recorded:
--
--   spot      coordinates in the text, or an address on the named road
--   road      a street whose own name matches the road in the article
--   locality  the named village or neighbourhood (a few km across)
--   town      the named town and its surrounding area (tens of km across)
--   highway   only a numbered highway matched — recorded WITHOUT a pin
--   null      not assessed yet (rows geocoded before this migration), or nothing matched
--
-- geocode_match holds the provider's name for what matched ("Bilgram", "Sahpur Road"),
-- so a reader and a reviewer can see what the pin was hung on.
--
-- The vocabulary is CHECK-constrained and lives in placematch.PRECISIONS; a test holds
-- the two together. A term added in code without widening this constraint aborts the
-- whole geocode batch on the next run (shipped twice: 18 Jul on geocode_method, 1 Aug on
-- geocode_qualifier).
--
-- APPLY BEFORE the code that writes these columns reaches the daily run, and not while
-- a run is active: ADD COLUMN takes a brief ACCESS EXCLUSIVE lock on incident.
alter table incident add column if not exists geocode_precision text;
alter table incident add column if not exists geocode_match text;

alter table incident drop constraint if exists incident_geocode_precision_check;
alter table incident add constraint incident_geocode_precision_check
  check (geocode_precision in ('spot', 'road', 'locality', 'town', 'highway'));

comment on column incident.geocode_precision is
  'What the name evidence supports: spot | road | locality | town | highway (no pin). '
  'Null = not assessed or nothing matched. See migration 018.';
comment on column incident.geocode_match is
  'Provider feature name the pin was accepted on (e.g. "Bilgram", "Sahpur Road").';

-- public_incident is `select i.*`, which Postgres expands when the view is CREATED:
-- columns added to incident since migration 010 (geocode_qualifier, geocode_attempted_at
-- and the two above) are invisible through it until it is re-created. The WHERE clause
-- is 010's, unchanged. New columns land at the end, so dependent views are unaffected.
create or replace view public_incident as
select i.*
from incident i
where i.verification_status in ('reviewed', 'verified', 'disputed')
   or (i.verification_status = 'auto_published'
       and exists (select 1 from incident_defect d
                   where d.incident_id = i.id
                     and d.defect_type not in ('other_infrastructure',
                                               'no_infrastructure_defect_identified')));

-- CREATE OR REPLACE VIEW resets reloptions — re-apply invoker rights (see 007)
alter view public_incident set (security_invoker = true);
