import aiosqlite

DB_PATH = "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,              -- YYYY-MM-DD
    start TEXT NOT NULL,             -- HH:MM
    end TEXT,
    title TEXT NOT NULL,
    place TEXT,
    source TEXT NOT NULL DEFAULT 'manual'   -- 'manual' | 'site'
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('daily', 'once')),
    due_date TEXT,                   -- только для 'once'
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
        await db.commit()


# ---------- settings ----------
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
async def add_lesson(date, start, end, title, place=None, source="manual"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO lessons(date, start, end, title, place, source) VALUES (?,?,?,?,?,?)",
            (date, start, end, title, place, source),
        )
        await db.commit()


async def replace_site_lessons(first_day, last_day, lessons):
    """Удаляет пары, загруженные с сайта, за период и записывает новые.
    Пары, добавленные вручную, не трогает."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM lessons WHERE source='site' AND date BETWEEN ? AND ?",
            (first_day, last_day),
        )
        await db.executemany(
            "INSERT INTO lessons(date, start, end, title, place, source) VALUES (?,?,?,?,?,'site')",
            [(l["date"], l["start"], l.get("end"), l["title"], l.get("place")) for l in lessons],
        )
        await db.commit()


async def get_lessons(first_day, last_day=None):
    last_day = last_day or first_day
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM lessons WHERE date BETWEEN ? AND ? ORDER BY date, start",
            (first_day, last_day),
        )
        return [dict(r) for r in await cur.fetchall()]


# ---------- tasks ----------
async def add_task(text, type_, due_date=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tasks(text, type, due_date) VALUES (?,?,?)", (text, type_, due_date)
        )
        await db.commit()
        return cur.lastrowid


async def delete_task(task_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))
        await db.commit()


async def list_active_tasks():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY type, id")
        return [dict(r) for r in await cur.fetchall()]


async def tasks_for_day(day):
    """Задачи на день: ежедневные + разовые на эту дату + просроченные невыполненные разовые."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT t.id, t.text, t.type, t.due_date, COALESCE(l.done, 0) AS done, 0 AS overdue
            FROM tasks t
            LEFT JOIN task_log l ON l.task_id = t.id AND l.date = ?
            WHERE t.active = 1 AND (t.type = 'daily' OR t.due_date = ?)
            UNION ALL
            SELECT t.id, t.text, t.type, t.due_date, 0 AS done, 1 AS overdue
            FROM tasks t
            WHERE t.active = 1 AND t.type = 'once' AND t.due_date < ?
              AND NOT EXISTS (SELECT 1 FROM task_log l WHERE l.task_id = t.id AND l.done = 1)
            ORDER BY type, id
            """,
            (day, day, day),
        )
        return [dict(r) for r in await cur.fetchall()]


async def toggle_task(task_id, day):
    """Ставит или снимает галочку. Возвращает новое состояние."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT done FROM task_log WHERE task_id=? AND date=?", (task_id, day)
        )
        row = await cur.fetchone()
        new_state = 0 if (row and row[0]) else 1
        await db.execute(
            "INSERT OR REPLACE INTO task_log(task_id, date, done, done_at) "
            "VALUES (?, ?, ?, CASE WHEN ?=1 THEN datetime('now') ELSE NULL END)",
            (task_id, day, new_state, new_state),
        )
        await db.commit()
        return bool(new_state)
