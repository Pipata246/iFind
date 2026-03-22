--Создание таблицы пользователи и внесение пользователе при отправке старт
create table if not exists public.bot_users (
  telegram_id bigint primary key,
  username text,
  first_name text,
  last_name text,
  language_code text,
  is_bot boolean,
  started_at timestamptz,
  last_seen_at timestamptz
);

alter table public.bot_users enable row level security;

create policy "anon can upsert bot_users"
on public.bot_users
for all
to anon
using (true)
with check (true);
----------------------------------
--таблица bot_settings + RLS для anon
create table if not exists public.bot_settings (
  telegram_id bigint primary key,
  platform text not null default 'avito',

  keyword text,
  model text,
  city text,

  price_min integer,
  price_max integer,

  memory text[] default '{}',
  ram text[] default '{}',
  sim text[] default '{}',
  colors text[] default '{}',
  condition text[] default '{}',

  seller_type text default 'all',
  rating_4_plus boolean default false,
  precision integer default 7,

  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

alter table public.bot_settings enable row level security;

-- Разрешаем anon полностью (чтобы точно заработало с anon key)
-- ВАЖНО: это будет означать, что любой с этим anon key может писать данные.
create policy "anon can upsert bot_settings"
on public.bot_settings
for all
to anon
using (true)
with check (true);
----------------------------------

-- Одна строка на пользователя: настройки именно ручного запуска + флаг «только сегодня»
create table if not exists public.bot_manual_settings (
  telegram_id bigint primary key,
  platform text not null default 'avito',

  keyword text,
  model text,
  city text,

  price_min integer,
  price_max integer,

  memory text[] default '{}',
  ram text[] default '{}',
  sim text[] default '{}',
  colors text[] default '{}',
  condition text[] default '{}',

  seller_type text default 'all',
  rating_4_plus boolean default false,
  precision integer default 7,

  today_only boolean default false,

  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

alter table public.bot_manual_settings enable row level security;

create policy "anon can upsert bot_manual_settings"
on public.bot_manual_settings
for all
to anon
using (true)
with check (true);

----------------------------------

--таблица exel файлов пользователей
create table if not exists public.bot_excel_files (
  id bigserial primary key,
  telegram_id bigint not null,
  filename text not null,
  content_base64 text not null,
  created_at timestamptz default now()
);

create index if not exists bot_excel_files_telegram_id_idx
on public.bot_excel_files (telegram_id);

alter table public.bot_excel_files enable row level security;

-- Так как вы используете anon key без auth, иначе сделать "только себе"
-- невозможно. Поэтому разрешаем доступ всем (как и в ваших предыдущих политиках).
create policy "anon excel read/write"
on public.bot_excel_files
for all
to anon
using (true)
with check (true);

----------------------------------

-- Миграция: если раньше добавляли wb_today_only — удалить (не используется):
-- alter table public.bot_settings drop column if exists wb_today_only;
