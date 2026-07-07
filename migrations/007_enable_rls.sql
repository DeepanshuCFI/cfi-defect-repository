-- 007: enable RLS everywhere (Supabase advisor: rls_disabled_in_public).
-- No component uses the PostgREST Data API — pipeline/review/export all connect as the
-- table-owning postgres role, which bypasses plain (non-FORCE) RLS — so deny-by-default
-- costs nothing and closes the anon-key read/write surface.
-- NOTE: do NOT add FORCE; the owner role must keep bypassing RLS or the pipeline breaks.
-- NOTE: any future CREATE TABLE needs its own ENABLE ROW LEVEL SECURITY line.

do $$
declare r record;
begin
  -- ownership filter skips PostGIS's spatial_ref_sys (extension-owned SRID catalog,
  -- unalterable by us and harmless if world-readable)
  for r in select tablename from pg_tables
           where schemaname = 'public' and tableowner = current_user loop
    execute format('alter table public.%I enable row level security', r.tablename);
  end loop;
end $$;

-- Views default to definer (owner) rights, which would let the API read through them
-- even with table RLS on. Invoker rights close that; the postgres owner still reads fine.
-- NOTE: re-apply after any future CREATE OR REPLACE VIEW of these.
alter view public_incident set (security_invoker = true);
alter view public_incident_defect set (security_invoker = true);
alter view public_hotspot set (security_invoker = true);
alter view review_queue set (security_invoker = true);
