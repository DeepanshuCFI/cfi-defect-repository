-- 014: add 'expired' to the processing_status vocabulary.
--
-- 'expired' = a 'new' row still holding a Google News redirector after the 30-day TTL.
-- Past that age the feed id has rotted and repeated resolver attempts only burn quota
-- (Google throttles per-IP volume), so the row is closed out as terminal. The row and
-- its URL are KEPT so store.seen_url still dedups the item if a feed re-serves it.
--
-- Same-commit rule (learned twice: migration 012's cleanup, and the 013 near-miss):
-- a CHECK-constrained vocabulary must gain its migration in the SAME commit as the
-- code that writes the new term — pipeline/store.py expire_stale_unresolved here.
alter table source_article drop constraint source_article_processing_status_check;
alter table source_article add constraint source_article_processing_status_check
  check (processing_status in
    ('new','fetched','irrelevant','relevant','extracted','failed',
     'near_duplicate','expired'));
