import aiosqlite

DB_PATH = "bot.db"

# Одна база, но у каждой записи есть chat_id: у каждого человека свои пары, задачи и галочки.
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL DEFAULT 0,
    date TEXT NOT NULL,                  -- YYYY-MM-DD
    start TEXT NOT NULL,                 -- HH:MM
    end TEXT,
    title TEXT NOT NULL,
    place TEXT,
    source TEXT NOT NULL DEFAULT 'manual'   -- 'manual' | 'site' (с фото)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('daily', 'once')),
    due_date TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS task_log (
    task_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    done_at TEXT,
    PRIMARY KEY (task_id, date)
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # Миграция старой базы (без chat_id)
        for table in ("lessons", "tasks"):
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in await cur.fetchall()]
            if "chat_id" not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN chat_id INTEGER NOT NULL DEFAULT 0")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_lessons ON lessons(chat_id, date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks ON tasks(chat_id, active)")
        await db.commit()


async def assign_legacy(chat_id):
    """Старые данные (chat_id = 0, появились до разделения по людям) отдаём одному человеку."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE lessons SET chat_id=? WHERE chat_id=0", (chat_id,))
        await db.execute("UPDATE tasks SET chat_id=? WHERE chat_id=0", (chat_id,))
        await db.commit()


# ---------- settings (общие для всех) ----------
async def get_setting(key, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        await db.commit()


# ---------- lessons ----------
async def add_lesson(chat_id, date, start, end, title, place=None, source="manual"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO lessons(chat_id, date, start, end, title, place, source) VALUES (?,?,?,?,?,?,?)",
            (chat_id, date, start, end, title, place, source),
        )
        await db.commit()


async def replace_site_lessons(chat_id, first_day, last_day, lessons):
    """Заменяет пары, загруженные с фото, за период. Пары, добавленные вручную, не трогает."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM lessons WHERE chat_id=? AND source='site' AND date BETWEEN ? AND ?",
            (chat_id, first_day, last_day),
        )
        await db.executemany(
            "INSERT INTO lessons(chat_id, date, start, end, title, place, source) VALUES (?,?,?,?,?,?,'site')",
            [(chat_id, l["date"], l["start"], l.get("end"), l["title"], l.get("place")) for l in lessons],
        )
        await db.commit()


async def get_lessons(chat_id, first_day, last_day=None):
    last_day = last_day or first_day
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM lessons WHERE chat_id=? AND date BETWEEN ? AND ? ORDER BY date, start",
            (chat_id, first_day, last_day),
        )
        return [dict(r) for r in await cur.fetchall()]


# ---------- tasks ----------
async def add_task(chat_id, text, type_, due_date=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tasks(chat_id, text, type, due_date) VALUES (?,?,?,?)",
            (chat_id, text, type_, due_date),
        )
        await db.commit()
        return cur.lastrowid


async def delete_task(chat_id, task_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET active=0 WHERE id=? AND chat_id=?", (task_id, chat_id))
        await db.commit()


async def list_active_tasks(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tasks WHERE chat_id=? AND active=1 ORDER BY type, id", (chat_id,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def tasks_for_day(chat_id, day):
    """Задачи на день: ежедневные + разовые на эту дату + просроченные невыполненные разовые."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT t.id, t.text, t.type, t.due_date, COALESCE(l.done, 0) AS done, 0 AS overdue
            FROM tasks t
            LEFT JOIN task_log l ON l.task_id = t.id AND l.date = ?
            WHERE t.chat_id = ? AND t.active = 1 AND (t.type = 'daily' OR t.due_date = ?)
            UNION ALL
            SELECT t.id, t.text, t.type, t.due_date, 0 AS done, 1 AS overdue
            FROM tasks t
            WHERE t.chat_id = ? AND t.active = 1 AND t.type = 'once' AND t.due_date < ?
              AND NOT EXISTS (SELECT 1 FROM task_log l WHERE l.task_id = t.id AND l.done = 1)
            ORDER BY type, id
            """,
            (day, chat_id, day, chat_id, day),
        )
        return [dict(r) for r in await cur.fetchall()]


async def toggle_task(chat_id, task_id, day):
    """Ставит или снимает галочку. Работает только со своими задачами. Возвращает новое состояние или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM tasks WHERE id=? AND chat_id=?", (task_id, chat_id))
        if not await cur.fetchone():
            return None
        cur = await db.execute("SELECT done FROM task_log WHERE task_id=? AND date=?", (task_id, day))
        row = await cur.fetchone()
        new_state = 0 if (row and row[0]) else 1
        await db.execute(
            "INSERT OR REPLACE INTO task_log(task_id, date, done, done_at) "
            "VALUES (?, ?, ?, CASE WHEN ?=1 THEN datetime('now') ELSE NULL END)",
            (task_id, day, new_state, new_state),
        )
        await db.commit()
        return bool(new_state)
