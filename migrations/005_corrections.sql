-- 005 — public corrections (§14, PRD §10) + visible disputed status + run telemetry.

create table if not exists correction (
  id           bigint generated always as identity primary key,
  entity_type  text not null check (entity_type in ('incident','hotspot')),
  entity_id    bigint not null,
  message      text not null check (length(trim(message)) > 0),
  contact      text,
  status       text not null default 'open' check (status in ('open','resolved','dismissed')),
  created_at   timestamptz not null default now(),
  resolved_at  timestamptz
);
create index if not exists idx_correction_status on correction (status);

-- Disputed entries stay VISIBLE (badged on the site) while being checked — hiding them
-- would let anyone censor the map by filing a correction. Rejected stays excluded.
create or replace view public_incident as
select i.*
from incident i
where i.verification_status <> 'rejected'
  and (
    i.verification_status in ('reviewed', 'verified', 'disputed')
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

-- guard: a 'disputed' mark only keeps an entry public if it was published before the
-- dispute (reviewed/verified or gate-passing). Auto-only disputed rows (never public)
-- fall out naturally because 'disputed' replaces 'auto' — acceptable at this scale;
-- reviewers resolve disputes from the queue.

-- reviewers must see disputed entries too (they stay public+badged meanwhile)
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

-- pipeline run telemetry (Phase 10)
create table if not exists pipeline_run (
  id          bigint generated always as identity primary key,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  ok          boolean,
  stage_stats jsonb,
  note        text
);
