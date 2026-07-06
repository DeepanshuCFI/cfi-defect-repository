-- 006 — auto-review statuses.
-- machine_ok:      2nd-pass AI confirmed a crash-only record (infra=false). Stays in the
--                  DB feeding crash-frequency/escalation counts; leaves the human queue;
--                  never publishable by override.
-- auto_published:  2nd-pass AI confirmed infra evidence + passed all gates. Public, and
--                  labelled as machine-reviewed on the site (never passed off as human).

alter table incident drop constraint if exists incident_verification_status_check;
alter table incident add constraint incident_verification_status_check
  check (verification_status in
    ('auto','reviewed','verified','disputed','rejected','machine_ok','auto_published'));

create or replace view public_incident as
select i.*
from incident i
where i.verification_status <> 'rejected'
  and (
    i.verification_status in ('reviewed', 'verified', 'disputed', 'auto_published')
    or (
      i.verification_status <> 'machine_ok'   -- crash-only clears never publish via gate
      and i.extraction_confidence::numeric(4,2) >= coalesce((
        select (value->>'extraction_confidence_min')::numeric
        from config_setting where key = 'confidence_gate'), 0.7)
      and i.geocode_confidence::numeric(4,2) >= coalesce((
        select (value->>'geocode_confidence_min')::numeric
        from config_setting where key = 'confidence_gate'), 0.6)
      and i.infra_implicated
      and exists (
        select 1 from incident_defect d
        where d.incident_id = i.id
          and d.defect_type not in ('other_infrastructure',
                                    'no_infrastructure_defect_identified'))
    )
  );

create or replace view review_queue as
select i.*,
       case
         when i.verification_status = 'disputed' then 'disputed_by_correction'
         when i.extraction_confidence::numeric(4,2) < 0.7 then 'extraction_confidence_below_min'
         when i.geocode_confidence is null
              or i.geocode_confidence::numeric(4,2) < 0.6 then 'geocode_confidence_below_min'
         when not i.infra_implicated then 'infra_not_implicated'
         else 'other_infrastructure_needs_review'
       end as queue_reason
from incident i
where i.verification_status = 'disputed'
   or (i.verification_status = 'auto'
       and i.id not in (select id from public_incident));
