create table if not exists public.aura_keepalive (
  id integer primary key default 1 check (id = 1),
  touched_at timestamptz not null default now(),
  source text not null default 'github_actions',
  note text
);

insert into public.aura_keepalive (id, touched_at, source, note)
values (1, now(), 'github_actions', 'A.U.R.A Supabase keepalive row')
on conflict (id)
do update set
  touched_at = excluded.touched_at,
  source = excluded.source,
  note = excluded.note;

comment on table public.aura_keepalive is
'A.U.R.A lightweight Supabase keepalive table. Updated by GitHub Actions to prevent inactivity.';

-- This table contains no private user data.
-- Do not store conversations, API keys, clipboard content, screenshots, audio,
-- camera frames, or identity descriptors in this table.
