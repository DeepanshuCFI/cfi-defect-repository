-- 003_public_views.sql — the §8 confidence gate, materialised as views.
-- Public surfaces (dashboard Phase 8, API Phase 9) read ONLY from these.
-- Rule: gate passes on stored confidences OR reviewer said so; rejected never shows.

create or replace view public_incident as
select i.*
from incident i
where i.verification_status not in ('rejected', 'disputed')
  and (
    i.verification_status in ('reviewed', 'verified')       -- reviewer override
    or (
      i.extraction_confidence >= coalesce((
        select (value->>'extraction_confidence_min')::real
        from config_setting where key = 'confidence_gate'), 0.7)
      and i.geocode_confidence >= coalesce((
        select (value->>'geocode_confidence_min')::real
        from config_setting where key = 'confidence_gate'), 0.6)
      and i.infra_implicated
      -- other_infrastructure alone never auto-publishes (needs review)
      and exists (
        select 1 from incident_defect d
        where d.incident_id = i.id
          and d.defect_type not in ('other_infrastructure',
                                    'no_infrastructure_defect_identified'))
    )
  );

-- Defects joined to their evidence, public incidents only
create or replace view public_incident_defect as
select d.*
from incident_defect d
join public_incident i on i.id = d.incident_id
where d.defect_type <> 'no_infrastructure_defect_identified';

-- A hotspot is public when it has at least one public member incident.
-- public_* casualty/incident counts are recomputed over PUBLIC members only, so the
-- public site never cites numbers it cannot source (full counts stay internal).
create or replace view public_hotspot as
select h.*,
       p.n_public_incidents, p.public_fatalities, p.public_injuries
from hotspot h
join lateral (
  select count(*) n_public_incidents,
         coalesce(sum(pi.fatalities),0) public_fatalities,
         coalesce(sum(pi.injuries),0) public_injuries
  from public_incident pi where pi.cluster_id = h.id
) p on p.n_public_incidents > 0;

-- Review queue: everything NOT public and not yet decided, with the failure reason
create or replace view review_queue as
select i.*,
       case
         when i.extraction_confidence < 0.7 then 'extraction_confidence_below_min'
         when i.geocode_confidence is null or i.geocode_confidence < 0.6
              then 'geocode_confidence_below_min'
         when not i.infra_implicated then 'infra_not_implicated'
         else 'other_infrastructure_needs_review'
       end as queue_reason
from incident i
where i.verification_status = 'auto'
  and i.id not in (select id from public_incident);
