"""База данных в облаке (PostgreSQL). Адрес берётся из переменной окружения DATABASE_URL.

Функции те же, что были с SQLite, так что main.py менять не нужно.
Записи подписаны chat_id: у каждого человека свои пары, задачи и галочки.
"""
import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL DEFAULT 0,
    date TEXT NOT NULL,                  -- YYYY-MM-DD
    start TEXT NOT NULL,                 -- HH:MM
    "end" TEXT,
    title TEXT NOT NULL,
    place TEXT,
    source TEXT NOT NULL DEFAULT 'manual'   -- 'manual' | 'site' (с фото)
);
CREATE TABLE IF NOT EXISTS tasks (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('daily', 'once')),
    due_date TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS task_log (
    task_id BIGINT NOT NULL,
    date TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    done_at TIMESTAMPTZ,
    PRIMARY KEY (task_id, date)
);
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS chat_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS chat_id BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_lessons ON lessons(chat_id, date);
CREATE INDEX IF NOT EXISTS idx_tasks ON tasks(chat_id, active);
"""

_pool = None


def _dsn():
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL не задан. Добавь в .env (или в переменные окружения хостинга) ссылку на облачную базу, "
            "вида postgresql://пользователь:пароль@хост/имя_базы?sslmode=require"
        )
    # Оставляем только sslmode: другие параметры из панелей Neon/Supabase (channel_binding, pgbouncer и т.п.)
    # asyncpg не понимает и воспринимает как настройки сервера.
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k == "sslmode"]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


async def _get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            _dsn(),
            min_size=1,
            max_size=5,
            statement_cache_size=0,  # нужно для пулеров соединений (pgbouncer) у облачных провайдеров
            max_inactive_connection_lifetime=60,  # облако рвёт долго простаивающие соединения
            command_timeout=30,
        )
    return _pool


async def _run(fn):
    """Выполняет fn(connection). При обрыве соединения (облако «засыпает») повторяет до 3 раз."""
    last = None
    for attempt in range(3):
        try:
            pool = await _get_pool()
            async with pool.acquire() as con:
                return await fn(con)
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, ConnectionError, OSError, TimeoutError) as e:
            last = e
            print(f"[db] проблема с соединением ({e}), повтор {attempt + 1}/3")
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last


async def init_db():
    async def f(con):
        await con.execute(SCHEMA)

    await _run(f)


async def assign_legacy(chat_id):
    """Записи без владельца (chat_id = 0) отдаём одному человеку."""
    async def f(con):
        await con.execute("UPDATE lessons SET chat_id=$1 WHERE chat_id=0", chat_id)
        await con.execute("UPDATE tasks SET chat_id=$1 WHERE chat_id=0", chat_id)

    await _run(f)


# ---------- settings (общие для всех) ----------
async def get_setting(key, default=None):
    async def f(con):
        row = await con.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else default

    return await _run(f)


async def set_setting(key, value):
    async def f(con):
        await con.execute(
            "INSERT INTO settings(key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value,
        )

    await _run(f)


# ---------- lessons ----------
async def add_lesson(chat_id, date, start, end, title, place=None, source="manual"):
    async def f(con):
        await con.execute(
            'INSERT INTO lessons(chat_id, date, start, "end", title, place, source) VALUES ($1,$2,$3,$4,$5,$6,$7)',
            chat_id, date, start, end, title, place, source,
        )

    await _run(f)


async def replace_site_lessons(chat_id, first_day, last_day, lessons):
    """Заменяет пары, загруженные с фото, за период. Пары, добавленные вручную, не трогает."""
    async def f(con):
        async with con.transaction():
            await con.execute(
                "DELETE FROM lessons WHERE chat_id=$1 AND source='site' AND date BETWEEN $2 AND $3",
                chat_id, first_day, last_day,
            )
            await con.executemany(
                'INSERT INTO lessons(chat_id, date, start, "end", title, place, source) '
                "VALUES ($1,$2,$3,$4,$5,$6,'site')",
                [(chat_id, l["date"], l["start"], l.get("end"), l["title"], l.get("place")) for l in lessons],
            )

    await _run(f)


async def get_lessons(chat_id, first_day, last_day=None):
    last_day = last_day or first_day

    async def f(con):
        rows = await con.fetch(
            "SELECT * FROM lessons WHERE chat_id=$1 AND date BETWEEN $2 AND $3 ORDER BY date, start",
            chat_id, first_day, last_day,
        )
        return [dict(r) for r in rows]

    return await _run(f)


# ---------- tasks ----------
async def add_task(chat_id, text, type_, due_date=None):
    async def f(con):
        return await con.fetchval(
            "INSERT INTO tasks(chat_id, text, type, due_date) VALUES ($1,$2,$3,$4) RETURNING id",
            chat_id, text, type_, due_date,
        )

    return await _run(f)


async def delete_task(chat_id, task_id):
    async def f(con):
        await con.execute("UPDATE tasks SET active=0 WHERE id=$1 AND chat_id=$2", task_id, chat_id)

    await _run(f)


async def list_active_tasks(chat_id):
    async def f(con):
        rows = await con.fetch(
            "SELECT * FROM tasks WHERE chat_id=$1 AND active=1 ORDER BY type, id", chat_id
        )
        return [dict(r) for r in rows]

    return await _run(f)


async def tasks_for_day(chat_id, day):
    """Задачи на день: ежедневные + разовые на эту дату + просроченные невыполненные разовые."""
    async def f(con):
        rows = await con.fetch(
            """
            SELECT t.id, t.text, t.type, t.due_date, COALESCE(l.done, 0) AS done, 0 AS overdue
            FROM tasks t
            LEFT JOIN task_log l ON l.task_id = t.id AND l.date = $1
            WHERE t.chat_id = $2 AND t.active = 1 AND (t.type = 'daily' OR t.due_date = $1)
            UNION ALL
            SELECT t.id, t.text, t.type, t.due_date, 0 AS done, 1 AS overdue
            FROM tasks t
            WHERE t.chat_id = $2 AND t.active = 1 AND t.type = 'once' AND t.due_date < $1
              AND NOT EXISTS (SELECT 1 FROM task_log l WHERE l.task_id = t.id AND l.done = 1)
            ORDER BY type, id
            """,
            day, chat_id,
        )
        return [dict(r) for r in rows]

    return await _run(f)


async def toggle_task(chat_id, task_id, day):
    """Ставит или снимает галочку. Работает только со своими задачами. Возвращает новое состояние или None."""
    async def f(con):
        async with con.transaction():
            if not await con.fetchval("SELECT 1 FROM tasks WHERE id=$1 AND chat_id=$2", task_id, chat_id):
                return None
            row = await con.fetchrow("SELECT done FROM task_log WHERE task_id=$1 AND date=$2", task_id, day)
            new_state = 0 if (row and row["done"]) else 1
            done_at = datetime.now(timezone.utc) if new_state else None
            await con.execute(
                "INSERT INTO task_log(task_id, date, done, done_at) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (task_id, date) DO UPDATE SET done = EXCLUDED.done, done_at = EXCLUDED.done_at",
                task_id, day, new_state, done_at,
            )
            return bool(new_state)

    return await _run(f)
