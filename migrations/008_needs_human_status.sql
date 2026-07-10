-- 008: 'needs_human' verification status — adjudicate-once for the auto-reviewer.
-- Before this, borderline items kept status 'auto' after a needs_human verdict, so the
-- auto-reviewer re-adjudicated the SAME items with the strong model every daily run —
-- pure recurring spend. Now a needs_human verdict flips status to 'needs_human': the
-- auto-reviewer's selector (verification_status='auto') never picks them up again, while
-- the review_queue view below still shows them to human reviewers.

alter table incident drop constraint if exists incident_verification_status_check;
alter table incident add constraint incident_verification_status_check
  check (verification_status in
    ('auto','reviewed','verified','disputed','rejected','machine_ok','auto_published',
     'needs_human'));

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
   or (i.verification_status in ('auto', 'needs_human')
       and i.id not in (select id from public_incident));

-- CREATE OR REPLACE VIEW resets reloptions — re-apply invoker rights (see 007)
alter view review_queue set (security_invoker = true);
