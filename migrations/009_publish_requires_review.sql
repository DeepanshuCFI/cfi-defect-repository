-- 009: publication requires a review — human OR machine — never the numeric gate alone.
-- Root cause of the Alwar hospital-ceiling incident (#358) reaching the public map:
-- the raw confidence gate published status='auto' items that the skeptical
-- auto-reviewer had never seen (it only read the review_queue, i.e. gate FAILERS).
-- Now the reviewer adjudicates every incident (auto_review.py), and this view only
-- publishes reviewed/verified/auto_published (+ disputed stays up with its badge —
-- censorship-by-correction prevention, unchanged).
-- PREREQUISITE: run the transition sweep first (adjudicate all status='auto' rows),
-- otherwise legitimate gate-passers vanish from the map until their adjudication.

create or replace view public_incident as
select i.*
from incident i
where i.verification_status in ('reviewed', 'verified', 'disputed', 'auto_published');

-- CREATE OR REPLACE VIEW resets reloptions — re-apply invoker rights (see 007)
alter view public_incident set (security_invoker = true);
