-- 004 — fix: real (float4) 0.7 fails `>= 0.7` (numeric literal) by 1e-8.
-- Incidents at exactly the threshold were wrongly queued. Cast to numeric(4,2)
-- everywhere the gate compares confidences. (Found live: 3 of 10 pilot incidents.)

create or replace view public_incident as
select i.*
from incident i
where i.verification_status not in ('rejected', 'disputed')
  and (
    i.verification_status in ('reviewed', 'verified')
    or (
      i.extraction_confidence::numeric(4,2) >= coalesce((
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
         when i.extraction_confidence::numeric(4,2) < 0.7 then 'extraction_confidence_below_min'
         when i.geocode_confidence is null
              or i.geocode_confidence::numeric(4,2) < 0.6 then 'geocode_confidence_below_min'
         when not i.infra_implicated then 'infra_not_implicated'
         else 'other_infrastructure_needs_review'
       end as queue_reason
from incident i
where i.verification_status = 'auto'
  and i.id not in (select id from public_incident);
