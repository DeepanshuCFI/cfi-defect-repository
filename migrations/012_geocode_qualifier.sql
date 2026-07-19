-- 012: separate geocode_qualifier column (audit finding, 19 Jul).
-- The 18 Jul fail-closed guards suffixed geocode_method ('landmark_district_unanchored',
-- '..._stateless_hit', '..._wide_area') — but geocode_method has a CHECK constraint
-- pinning it to a controlled vocabulary, so the next geocode run would have failed
-- 100% on constraint violation. CLAUDE.md rule 4 (controlled vocabulary) says keep the
-- method clean; the guard signal belongs in its own column.

alter table incident add column if not exists geocode_qualifier text
  check (geocode_qualifier in ('unanchored', 'stateless_hit', 'wide_area'));

comment on column incident.geocode_qualifier is
  'Which fail-closed geocode guard fired (confidence was capped below the publish bar).';
