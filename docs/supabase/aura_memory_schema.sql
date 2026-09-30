-- A.U.R.A persistent memory schema for backend-only Supabase access.
-- Do not expose the Supabase service-role key to the frontend.
-- If direct frontend access is added later, enable strict RLS policies first.
-- Keep all reads and writes scoped by owner_key.
-- Consider enabling RLS even for future safety, with service-role-only backend access.

create table if not exists aura_conversations (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  title text,
  source text not null default 'main_orb',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb
);

create table if not exists aura_messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references aura_conversations(id) on delete cascade,
  owner_key text not null,
  role text not null check (role in ('user', 'assistant', 'system', 'tool')),
  source text,
  input_source text check (input_source in ('voice', 'text', 'tool', 'diagnostics')),
  content text not null,
  raw_content text,
  normalized_content text,
  language text,
  provider_route text,
  frontend_kind text,
  intent_detected text,
  tool_name text,
  failure_class text,
  actions_returned int default 0,
  actions_executed text[],
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_aura_messages_conversation_created
on aura_messages(conversation_id, created_at desc);

create index if not exists idx_aura_messages_owner_created
on aura_messages(owner_key, created_at desc);

create table if not exists aura_memory_facts (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  fact_text text,
  fact_type text,
  category text not null,
  fact text not null,
  confidence numeric not null default 0.8,
  source_message_id uuid references aura_messages(id) on delete set null,
  importance int not null default 3 check (importance between 1 and 5),
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  expires_at timestamptz,
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists idx_aura_memory_facts_owner_category
on aura_memory_facts(owner_key, category, is_active);

create table if not exists aura_conversation_summaries (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references aura_conversations(id) on delete cascade,
  owner_key text not null,
  summary text not null,
  message_start_id uuid,
  message_end_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists idx_aura_summaries_owner_created
on aura_conversation_summaries(owner_key, created_at desc);

create table if not exists aura_interaction_events (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  conversation_id uuid references aura_conversations(id) on delete set null,
  event_type text not null,
  event_status text,
  tool_name text,
  intent_detected text,
  failure_class text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_aura_interaction_events_owner_created
on aura_interaction_events(owner_key, created_at desc);
