-- 010: restore the defect-existence condition for machine-published incidents.
-- Migration 009 replaced the gate-based public_incident view with a status allow-list
-- and silently DROPPED the old view's requirement that a public incident carry at
-- least one real taxonomy defect — 13 auto_published rows with no real defect leaked
-- through (guard-audit 2026-07-18). Human approvals (reviewed/verified) stay exempt:
-- a human saw the record. auto_review.py now also refuses to grant auto_published
-- without a real defect (belt); this view condition is the suspenders.

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
