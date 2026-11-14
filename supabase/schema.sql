-- skywalk: онлайн и общие рекорды игроков.
-- Запускать один раз в Supabase → SQL Editor. Повторный запуск безопасен (всё через IF NOT EXISTS / OR REPLACE).
--
-- Как устроено:
--   * программа знает только публичный ключ (anon / publishable) и может ТОЛЬКО вызывать две функции:
--     heartbeat(...) — «я запущена» и submit_record(...) — «новый рекорд». Читать или менять таблицы напрямую нельзя.
--   * функции сами проверяют данные: формат Steam ID, пределы дистанций, чтобы стрейфы и sync сходились
--     с дистанцией, частоту отправки, бан. Подозрительный рекорд сохраняется со статусом 'pending'
--     и ждёт ручной проверки в админке.
--   * видеть и менять всё может только админ: он входит в админку по email и паролю (Supabase Auth),
--     а его id записан в таблицу admins.

-- ---------- таблицы ----------
create table if not exists public.players (
    steam_id   text primary key check (steam_id ~ '^7656119[0-9]{10}$'),
    nick       text not null check (char_length(nick) between 1 and 64),
    version    text check (char_length(version) <= 32),
    first_seen timestamptz not null default now(),
    last_seen  timestamptz not null default now(),
    banned     boolean not null default false
);

create table if not exists public.records (
    id         bigint generated always as identity primary key,
    steam_id   text not null references public.players (steam_id) on delete cascade,
    abbr       text not null,
    mode       text not null default 'CKZ',
    dist       numeric(7, 3) not null,
    pre        numeric(6, 2),
    sync       numeric(5, 2),
    strafes    int,
    block      int,
    map        text check (char_length(map) <= 64),
    jumped_at  timestamptz,
    details    jsonb,
    status     text not null default 'pending' check (status in ('approved', 'pending', 'rejected')),
    reason     text,
    created_at timestamptz not null default now()
);
create index if not exists records_player_abbr on public.records (steam_id, abbr);
create index if not exists records_status on public.records (status, created_at desc);

create table if not exists public.admins (
    user_id uuid primary key references auth.users (id) on delete cascade
);

-- ---------- доступ: напрямую — только админ ----------
alter table public.players enable row level security;
alter table public.records enable row level security;
alter table public.admins  enable row level security;

create or replace function public.is_admin() returns boolean
    language sql stable security definer set search_path = public
as $$ select exists (select 1 from public.admins where user_id = auth.uid()) $$;

drop policy if exists admin_all on public.players;
create policy admin_all on public.players for all to authenticated using (public.is_admin()) with check (public.is_admin());
drop policy if exists admin_all on public.records;
create policy admin_all on public.records for all to authenticated using (public.is_admin()) with check (public.is_admin());
drop policy if exists admin_self on public.admins;
create policy admin_self on public.admins for select to authenticated using (user_id = auth.uid());

-- ---------- пределы дистанций ----------
-- review — с этой дистанции рекорд всегда проверяется вручную (порог тира wrecker из CS2KZ),
-- max — больше этого не бывает: такой рекорд отклоняется сразу.
create or replace function public.dist_limits(p_mode text, p_abbr text, out review numeric, out max numeric)
    language sql immutable
as $$
    select l.review, l.max from (values
        ('CKZ', 'LJ', 284, 300), ('CKZ', 'BH', 295, 330), ('CKZ', 'MBH', 295, 340), ('CKZ', 'WJ', 295, 330),
        ('CKZ', 'LAJ', 200, 230), ('CKZ', 'LAH', 278, 300), ('CKZ', 'JB', 295, 320),
        ('CKZ', 'LBH', 295, 330), ('CKZ', 'LWJ', 295, 330),
        ('VNL', 'LJ', 248, 265), ('VNL', 'BH', 242, 265), ('VNL', 'MBH', 248, 275), ('VNL', 'WJ', 246, 265),
        ('VNL', 'LAJ', 108, 130), ('VNL', 'LAH', 263, 280), ('VNL', 'JB', 272, 290),
        ('VNL', 'LBH', 242, 265), ('VNL', 'LWJ', 246, 265)
    ) as l (mode, abbr, review, max)
    where l.mode = p_mode and l.abbr = p_abbr
$$;

-- ---------- «я запущена» (раз в минуту) ----------
create or replace function public.heartbeat(p_steam_id text, p_nick text, p_version text)
    returns text language plpgsql security definer set search_path = public
as $$
begin
    if p_steam_id !~ '^7656119[0-9]{10}$' or coalesce(char_length(p_nick), 0) not between 1 and 64 then
        return 'invalid';
    end if;
    insert into public.players (steam_id, nick, version)
    values (p_steam_id, p_nick, left(p_version, 32))
    on conflict (steam_id) do update
        set nick = excluded.nick, version = excluded.version, last_seen = now()
        where players.last_seen < now() - interval '20 seconds';     -- чаще раза в 20 с — не пишем
    return 'ok';
end $$;

-- ---------- новый рекорд ----------
create or replace function public.submit_record(
    p_steam_id text, p_nick text, p_version text,
    p_abbr text, p_mode text, p_dist numeric, p_pre numeric, p_sync numeric, p_strafes int, p_block int,
    p_map text, p_jumped_at timestamptz, p_details jsonb)
    returns text language plpgsql security definer set search_path = public
as $$
declare
    lim record;
    best numeric;
    v_status text := 'approved';
    why text[] := '{}';
    rows_n int;
    rows_sync numeric;
begin
    if p_steam_id !~ '^7656119[0-9]{10}$' or coalesce(char_length(p_nick), 0) not between 1 and 64 then
        return 'invalid';
    end if;
    perform public.heartbeat(p_steam_id, p_nick, p_version);
    if (select banned from public.players where steam_id = p_steam_id) then
        return 'banned';
    end if;
    -- не чаще 10 рекордов в минуту от одного игрока
    if (select count(*) from public.records
        where steam_id = p_steam_id and created_at > now() - interval '1 minute') >= 10 then
        return 'too_often';
    end if;
    select * into lim from public.dist_limits(coalesce(p_mode, 'CKZ'), p_abbr);
    if lim.max is null then
        return 'invalid';
    end if;
    -- явная неправда — сразу отказ, в базу не пишем
    if p_dist is null or p_dist <= 0 or p_dist > lim.max
       or p_sync is not null and (p_sync < 0 or p_sync > 100)
       or p_pre is not null and (p_pre < 0 or p_pre > 400)
       or p_strafes is not null and (p_strafes < 1 or p_strafes > 40)
       or p_details is not null and pg_column_size(p_details) > 30000 then
        return 'invalid';
    end if;
    -- храним только улучшения
    select max(dist) into best from public.records
        where records.steam_id = p_steam_id and records.abbr = p_abbr and records.status <> 'rejected';
    if best is not null and p_dist <= best then
        return 'not_better';
    end if;

    -- ---- что делает рекорд подозрительным ----
    if p_dist >= lim.review then
        why := array_append(why, 'дистанция уровня wrecker');
    end if;
    if best is not null and p_dist - best > 10 then
        why := array_append(why, format('прирост +%s к прошлому', round(p_dist - best, 2)));
    end if;
    if best is null and p_dist >= lim.review - 5 then
        why := array_append(why, 'первый рекорд игрока сразу почти топовый');
    end if;
    rows_n := case when jsonb_typeof(p_details -> 'strafe_rows') = 'array'
                   then jsonb_array_length(p_details -> 'strafe_rows') else 0 end;
    if rows_n = 0 then
        why := array_append(why, 'нет таблицы стрейфов');
    else
        select avg((r ->> 'sync')::numeric) into rows_sync
            from jsonb_array_elements(p_details -> 'strafe_rows') r
            where jsonb_typeof(r -> 'sync') = 'number';
        if p_strafes is not null and abs(rows_n - p_strafes) > 1 then
            why := array_append(why, format('стрейфов %s, а строк в таблице %s', p_strafes, rows_n));
        end if;
        if p_sync is not null and rows_sync is not null and abs(rows_sync - p_sync) > 15 then
            why := array_append(why, format('sync %s, а по стрейфам %s', p_sync, round(rows_sync, 1)));
        end if;
    end if;
    if p_pre is not null and p_abbr in ('LJ', 'LAH') and p_pre > 290 then
        why := array_append(why, format('pre %s — слишком высокий', p_pre));
    end if;
    if array_length(why, 1) > 0 then
        v_status := 'pending';
    end if;

    insert into public.records (steam_id, abbr, mode, dist, pre, sync, strafes, block, map, jumped_at, details,
                                status, reason)
    values (p_steam_id, p_abbr, coalesce(p_mode, 'CKZ'), p_dist, p_pre, p_sync, p_strafes, p_block,
            left(p_map, 64), p_jumped_at, p_details, v_status, array_to_string(why, '; '));
    return v_status;
end $$;

-- программа (роль anon) может только вызвать эти две функции
revoke all on function public.heartbeat(text, text, text) from public;
revoke all on function public.submit_record(text, text, text, text, text, numeric, numeric, numeric, int, int,
                                            text, timestamptz, jsonb) from public;
grant execute on function public.heartbeat(text, text, text) to anon, authenticated;
grant execute on function public.submit_record(text, text, text, text, text, numeric, numeric, numeric, int, int,
                                               text, timestamptz, jsonb) to anon, authenticated;
revoke all on function public.dist_limits(text, text) from public;
grant execute on function public.is_admin() to authenticated;
