-- 016: add 'query_defect' to the config_keyword.category vocabulary.
--
-- query_defect = the defect terms the collector uses as Google News SEARCH queries
-- ('<district> गड्ढे', '<district> जलभराव'), distinct from infra_defect (the long
-- pre-filter / priority vocabulary). 3.5-month funnel (16 Oct 2026): articles carrying
-- defect vocabulary were 13% of the collection but 68% of published pins, and 87% of
-- pins are casualty-free defect reports — while all 584 daily queries were crash queries.
--
-- Same-commit rule (migration 014): the CHECK gains the term in the SAME commit as the
-- code that writes it (scripts/load_configs.py reads config_keyword_pack.json into this
-- table on every CI run) — else the "Sync configs to DB" step aborts on the first term.
alter table config_keyword drop constraint config_keyword_category_check;
alter table config_keyword add constraint config_keyword_category_check
  check (category in ('crash','fatality','injury','crash_type','infra_defect','query_defect'));
