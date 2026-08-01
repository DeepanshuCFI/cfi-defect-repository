-- 013: let geocode_qualifier hold the district guard's signals.
--
-- Migration 012 was written the day the guard signal was moved OUT of geocode_method,
-- which has its own CHECK constraint, after that mistake would have failed 100% of
-- geocodes on the next run (audit finding, 19 Jul). The district-anchoring fix
-- (5db87ee, 1 Aug) then added a FOURTH qualifier, 'districtless_hit', without widening
-- this constraint — the identical mistake one column over.
--
-- It has not bitten yet only by timing: 5db87ee was committed at 06:56 UTC on 1 Aug and
-- that day's scheduled run had finished at 06:39, so the district guard has never run in
-- production. cmd_geocode writes the qualifier with no try/except and commits after the
-- loop, so the first provider response that omits a district would abort the whole
-- geocode batch, and would do so again every run.
--
-- 'no_district_valid_location' is the repair script's signal for a record where no
-- district-valid location exists at all: its confidence is zeroed so it stops clustering
-- and stops counting toward the >=3-in-6-months escalation rule.
--
-- Lesson worth keeping: a controlled vocabulary in a CHECK constraint needs a migration
-- in the SAME commit as the code that adds a term. Twice now the code shipped alone.

alter table incident drop constraint if exists incident_geocode_qualifier_check;

alter table incident add constraint incident_geocode_qualifier_check
  check (geocode_qualifier in ('unanchored', 'stateless_hit', 'wide_area',
                               'districtless_hit', 'no_district_valid_location'));

comment on column incident.geocode_qualifier is
  'Which fail-closed geocode guard fired (confidence was capped below the publish bar, '
  'or zeroed entirely when no district-valid location exists).';
