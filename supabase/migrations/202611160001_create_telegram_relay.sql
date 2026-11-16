-- Run this once in Supabase Dashboard -> SQL Editor.
-- This schema deliberately stores no Telegram session and no post body.

begin;

create table if not exists public.tg_relay_state (
    relay_key text primary key,
    last_message_id bigint not null default 0 check (last_message_id >= 0),
    initialized_at timestamptz not null default now(),
    last_run_at timestamptz,
    last_success_at timestamptz,
    updated_at timestamptz not null default now()
);

create table if not exists public.tg_relay_delivery (
    relay_key text not null references public.tg_relay_state (relay_key) on delete cascade,
    telegram_message_id bigint not null check (telegram_message_id > 0),
    telegram_date timestamptz,
    source_url text,
    status text not null check (status in ('pending', 'sent', 'failed')),
    attempts integer not null default 0 check (attempts >= 0),
    last_error text,
    sent_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (relay_key, telegram_message_id)
);

create index if not exists tg_relay_delivery_status_idx
    on public.tg_relay_delivery (relay_key, status, updated_at desc);

create or replace function public.tg_relay_set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists tg_relay_state_set_updated_at on public.tg_relay_state;
create trigger tg_relay_state_set_updated_at
before update on public.tg_relay_state
for each row execute function public.tg_relay_set_updated_at();

drop trigger if exists tg_relay_delivery_set_updated_at on public.tg_relay_delivery;
create trigger tg_relay_delivery_set_updated_at
before update on public.tg_relay_delivery
for each row execute function public.tg_relay_set_updated_at();

alter table public.tg_relay_state enable row level security;
alter table public.tg_relay_delivery enable row level security;

-- No browser/client role receives any access. The GitHub workflow uses a
-- server-side secret key held only in GitHub Actions Secrets.
revoke all on public.tg_relay_state from anon, authenticated;
revoke all on public.tg_relay_delivery from anon, authenticated;
grant usage on schema public to service_role;
grant select, insert, update, delete on public.tg_relay_state to service_role;
grant select, insert, update, delete on public.tg_relay_delivery to service_role;

commit;
