-- ============================================================================
-- BU Monthly Performance & Ranking System — Supabase schema + RLS
-- ============================================================================
-- Run this once in the Supabase SQL Editor (Dashboard -> SQL Editor -> New
-- query). Auth is handled by Supabase Auth itself (auth.users); this file
-- only creates the app's own tables, links them to auth.users via
-- `profiles`, and locks them down with Row Level Security so that:
--   - a BU user can only see/insert rows for their own business unit
--   - an admin can see/edit everything
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. business_units
-- ----------------------------------------------------------------------------
create table if not exists business_units (
    id uuid primary key default gen_random_uuid(),
    bu_name text not null unique,
    bu_code text not null unique,
    created_at timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 2. profiles — one row per auth.users row. Created either by an admin
--    (Role Management panel, status='approved' immediately) or by public
--    self-registration (status='pending' until an admin approves it --
--    Supabase Auth itself doesn't know about "pending", so this status
--    column is what the app checks after a successful login).
-- ----------------------------------------------------------------------------
create table if not exists profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    full_name text,
    role text not null default 'bu_user' check (role in ('admin', 'bu_user')),
    bu_id uuid references business_units(id),
    status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
    created_at timestamptz not null default now()
);

-- Safe to re-run against an already-created table (adds the column only if
-- missing) -- lets you apply this update to a database from before `status`
-- existed without dropping anything.
alter table profiles add column if not exists status text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected'));

-- Existing admin rows created before `status` existed must not be locked
-- out by the new default of 'pending'.
update profiles set status = 'approved' where role = 'admin' and status <> 'approved';

-- ----------------------------------------------------------------------------
-- 3. monthly_reports — a BU uploads a report FILE (any format, unparsed);
--    there's no metric/score data entry. Rank is arrival order within the
--    month (1st BU to submit = rank 1), admin-editable afterward.
-- ----------------------------------------------------------------------------
create table if not exists monthly_reports (
    id uuid primary key default gen_random_uuid(),
    bu_id uuid not null references business_units(id),
    month_year text not null,                          -- 'YYYY-MM'
    file_name text,
    file_url text,
    file_path text,                                     -- storage object path, for cleanup/reference
    rank int,
    submitted_at timestamptz not null default now(),
    status text not null default 'Submitted' check (status in ('Submitted', 'Late', 'Pending')),
    submitted_by uuid references profiles(id) on delete set null,
    unique (bu_id, month_year)                          -- one report per BU per month
);

-- Migration for a database created before "on delete set null" was added
-- above: without it, deleting a user who ever submitted a report fails
-- outright (Supabase Auth cascades auth.users -> profiles, which then hits
-- this FK with no ON DELETE behavior and rejects the whole delete). This
-- preserves the report/ranking data -- only the "who submitted it"
-- attribution is cleared, never the file or rank. Safe to re-run.
alter table monthly_reports drop constraint if exists monthly_reports_submitted_by_fkey;
alter table monthly_reports add constraint monthly_reports_submitted_by_fkey
    foreign key (submitted_by) references profiles(id) on delete set null;

-- Migration for a database created before this file-based version: drop
-- the old metric/score columns, add the file columns. Safe to re-run.
alter table monthly_reports drop column if exists metric_1;
alter table monthly_reports drop column if exists metric_2;
alter table monthly_reports drop column if exists total_score;
alter table monthly_reports add column if not exists file_name text;
alter table monthly_reports add column if not exists file_url text;
alter table monthly_reports add column if not exists file_path text;

-- ============================================================================
-- Row Level Security
-- ============================================================================
alter table business_units enable row level security;
alter table profiles enable row level security;
alter table monthly_reports enable row level security;

-- A profiles-select-from-inside-a-profiles-policy check would recurse
-- infinitely under RLS, so role checks go through this SECURITY DEFINER
-- function instead: it runs with the function owner's privileges, bypassing
-- RLS for its own internal lookup, and simply returns true/false.
create or replace function is_admin()
returns boolean
language sql
security definer
set search_path = public
stable
as $$
    select exists (
        select 1 from profiles where id = auth.uid() and role = 'admin'
    );
$$;

-- ----------------------------------------------------------------------------
-- profiles policies
-- ----------------------------------------------------------------------------
-- Postgres has no "CREATE POLICY IF NOT EXISTS", so every policy below is
-- preceded by a DROP POLICY IF EXISTS -- without this, re-running this file
-- against a database that already has these policies fails with "policy
-- already exists", and because Supabase's SQL Editor runs a pasted script
-- as one transaction, that failure silently rolls back EVERYTHING in the
-- same run, including any schema changes earlier in the file.
drop policy if exists "profiles_select_self_or_admin" on profiles;
create policy "profiles_select_self_or_admin" on profiles
    for select
    using (auth.uid() = id or is_admin());

drop policy if exists "profiles_insert_admin" on profiles;
create policy "profiles_insert_admin" on profiles
    for insert
    with check (is_admin());

drop policy if exists "profiles_update_admin" on profiles;
create policy "profiles_update_admin" on profiles
    for update
    using (is_admin());

-- ----------------------------------------------------------------------------
-- business_units policies — every signed-in user can read (needed to
-- populate BU dropdowns); only admin can add/edit business units.
-- ----------------------------------------------------------------------------
drop policy if exists "bu_select_authenticated" on business_units;
create policy "bu_select_authenticated" on business_units
    for select
    using (auth.role() = 'authenticated');

drop policy if exists "bu_write_admin" on business_units;
create policy "bu_write_admin" on business_units
    for all
    using (is_admin())
    with check (is_admin());

-- ----------------------------------------------------------------------------
-- monthly_reports policies
-- ----------------------------------------------------------------------------
drop policy if exists "reports_select_own_or_admin" on monthly_reports;
create policy "reports_select_own_or_admin" on monthly_reports
    for select
    using (
        is_admin()
        or bu_id = (select bu_id from profiles where id = auth.uid())
    );

drop policy if exists "reports_insert_own_bu" on monthly_reports;
create policy "reports_insert_own_bu" on monthly_reports
    for insert
    with check (
        bu_id = (select bu_id from profiles where id = auth.uid())
    );

-- Only admin can edit a report after it's been submitted (score
-- corrections, marking incomplete, etc.) -- BU users cannot self-edit.
drop policy if exists "reports_update_admin" on monthly_reports;
create policy "reports_update_admin" on monthly_reports
    for update
    using (is_admin());

drop policy if exists "reports_delete_admin" on monthly_reports;
create policy "reports_delete_admin" on monthly_reports
    for delete
    using (is_admin());

-- ============================================================================
-- Seed the first admin (run AFTER creating the auth user)
-- ============================================================================
-- 1. Create the admin's login in Supabase Dashboard -> Authentication ->
--    Users -> Add User (set "Auto Confirm User" so no email step is needed),
--    or via the app once one admin profile exists (chicken-and-egg for the
--    very first admin, so the dashboard is the simplest path).
-- 2. Copy that user's UUID and run:
--
--   insert into profiles (id, full_name, role, bu_id, status)
--   values ('<uuid-from-auth.users>', 'Admin', 'admin', null, 'approved');
