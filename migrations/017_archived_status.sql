-- 017: add 'archived' to the incident.verification_status vocabulary.
--
-- 16 Oct 2026: 4,452 incidents sat in needs_human against 66 human actions in 3.5
-- months; 2,747 of them could never publish because their location sits below the 0.6
-- bar. 'archived' = terminal, never public (public_incident and review_queue list their
-- statuses explicitly, so it is excluded from both without a view change). The Mapbox
-- rescue pass may still lift an archived row back to 'auto' when its geocode crosses
-- the bar. Written by auto_review (unresolved verdict below the location bar) and by
-- the daily hygiene (needs_human untouched for 30 days). Same-commit rule (014/016).
alter table incident drop constraint incident_verification_status_check;
alter table incident add constraint incident_verification_status_check
  check (verification_status in
    ('auto','reviewed','verified','disputed','rejected','machine_ok',
     'auto_published','needs_human','archived'));
