-- Run once in Supabase: Dashboard > SQL Editor > New query > paste > Run.
-- Tables for the Skinstinct Telegram bot. Row Level Security is on with no policies,
-- so only the service_role key (used by the bot on Vercel) can read or write.

create table if not exists voice_skill (
    id bigint generated always as identity primary key,
    content text not null,
    sha256 text unique not null,
    loaded_at timestamptz not null default now()
);

create table if not exists notes (
    id bigint generated always as identity primary key,
    chat_id bigint,
    telegram_message_id bigint,
    text text not null,
    score int check (score between 0 and 10),
    score_reason text,
    created_at timestamptz not null default now()
);

create table if not exists drafts (
    id bigint generated always as identity primary key,
    note_id bigint references notes(id),
    note_text text not null,
    draft text not null,
    news_json jsonb,
    news_used boolean not null default false,
    voice_skill_id bigint references voice_skill(id),
    status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
    telegram_message_id bigint,
    created_at timestamptz not null default now(),
    decided_at timestamptz
);

create table if not exists settings (
    key text primary key,
    value text
);

-- Telegram re-sends a webhook update if the bot is slow to answer; this stops a note being drafted twice.
create table if not exists processed_updates (
    update_id bigint primary key,
    received_at timestamptz not null default now()
);

create index if not exists drafts_status_idx on drafts (status, id desc);
create index if not exists drafts_message_idx on drafts (telegram_message_id);

alter table voice_skill enable row level security;
alter table notes enable row level security;
alter table drafts enable row level security;
alter table settings enable row level security;
alter table processed_updates enable row level security;
