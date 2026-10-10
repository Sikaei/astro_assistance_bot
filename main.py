import asyncio
import html
import os
import re
from datetime import date, datetime, timedelta

from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"  # рядом с main.py, а не относительно текущей папки
load_dotenv(ENV_PATH)
if not os.getenv("BOT_TOKEN"):
    raise SystemExit(
        f"BOT_TOKEN не найден.\nИщу файл: {ENV_PATH} (существует: {ENV_PATH.exists()})\n"
        "Проверь, что файл называется именно .env (не .env.txt) "
        "и в нём есть строка BOT_TOKEN=... без пробелов и кавычек."
    )

import ai  # noqa: E402  (после load_dotenv, чтобы подхватить GEMINI_MODEL)
import database as db  # noqa: E402
import schedule_sync  # noqa: E402
import weather as weather_mod  # noqa: E402

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Список ID, которым можно пользоваться ботом (OWNER_IDS=111,222,333 или OWNER_ID=...)
raw_owner_ids = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID", "")
OWNER_IDS = [int(x.strip()) for x in raw_owner_ids.split(",") if x.strip().isdigit()]

CITY = os.getenv("CITY", "Nanjing")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))
DEFAULT_MORNING = os.getenv("MORNING_TIME", "07:00")
SYNC_HOUR = int(os.getenv("SYNC_HOUR", "20"))  # воскресное напоминание про фото расписания

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WD_CAP = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

LINE = "──────────────"

# Кнопки главного меню (постоянная клавиатура внизу)
BTN_MORNING = "Сводка"
BTN_TODAY = "Расписание"
BTN_TOMORROW = "Завтра"
BTN_WEEK = "Неделя"
BTN_TASKS = "Задачи"
BTN_ADD = "Добавить задачу"
MENU_TEXTS = {BTN_MORNING, BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_TASKS, BTN_ADD}

# Прокси нужен, если Telegram и Google недоступны напрямую (например, в Китае).
# Для Gemini прокси подхватывается через переменные окружения.
PROXY_URL = os.getenv("PROXY_URL")
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL

session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else AiohttpSession()
bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)

public = Router()  # /start доступен всем, чтобы узнать свой ID
owner = Router()  # всё остальное только тем, кто в списке OWNER_IDS

# Фильтры доступа проверяют вхождение ID в список OWNER_IDS
owner.message.filter(lambda m: bool(OWNER_IDS) and bool(m.from_user) and m.from_user.id in OWNER_IDS)
owner.callback_query.filter(lambda c: bool(OWNER_IDS) and bool(c.from_user) and c.from_user.id in OWNER_IDS)

# user_id -> тип задачи, текст которой бот ждёт следующим сообщением
awaiting = {}

TASK_KINDS = {
    "daily": ("daily", None, "Ежедневная задача добавлена"),
    "today": ("once", 0, "Задача на сегодня добавлена"),
    "tomorrow": ("once", 1, "Задача на завтра добавлена"),
}


def today() -> date:
    return datetime.now(TZ).date()


def esc(s) -> str:
    return html.escape(str(s))


def fmt_day(d: date) -> str:
    return f"{WD_CAP[d.weekday()]} {d.strftime('%d.%m')}"


_WD_SHORT = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
_WD_FULL = {"пон": 0, "вто": 1, "сре": 2, "чет": 3, "пят": 4, "суб": 5, "вос": 6}


def parse_day(arg):
    """Разбирает день из текста: сегодня/завтра/послезавтра/вчера, пн..вс (ближайший),
    дд.мм, дд.мм.гггг, гггг-мм-дд. Пустая строка = сегодня. Не распознано -> None."""
    a = (arg or "").strip().lower()
    if not a:
        return today()
    if a.startswith("пос"):
        return today() + timedelta(days=2)
    if a.startswith("зав"):
        return today() + timedelta(days=1)
    if a.startswith("вче"):
        return today() - timedelta(days=1)
    if a.startswith("сег"):
        return today()
    wd = _WD_SHORT.get(a) if len(a) == 2 else _WD_FULL.get(a[:3])
    if wd is not None:
        return today() + timedelta(days=(wd - today().weekday()) % 7)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
            return date.fromisoformat(a)
        mt = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", a)
        if mt:
            dd, mm, yy = int(mt.group(1)), int(mt.group(2)), mt.group(3)
            return date(int(yy) if yy else today().year, mm, dd)
    except ValueError:
        pass
    return None


async def send_to_owners(text: str, reply_markup=None):
    """Одинаковое сообщение всем из списка (используется для напоминания)."""
    for owner_id in OWNER_IDS:
        try:
            await bot.send_message(owner_id, text, reply_markup=reply_markup)
        except Exception as e:
            print(f"[send_to_owners] Не удалось отправить сообщение для ID {owner_id}: {e}")


# ---------- клавиатуры ----------
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_TASKS)],
            [KeyboardButton(text=BTN_MORNING), KeyboardButton(text=BTN_ADD)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие",
    )


def day_nav_kb(day: date) -> InlineKeyboardMarkup:
    prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
    monday = day - timedelta(days=day.weekday())
    week_row = []
    for i in range(7):
        d = monday + timedelta(days=i)
        label = WD_CAP[i]
        if d == day:
            label = f"[{label}]"
        elif d == today():
            label = f"{label}·"
        week_row.append(InlineKeyboardButton(text=label, callback_data=f"sd:{d.isoformat()}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            week_row,
            [
                InlineKeyboardButton(text=f"‹ {fmt_day(prev_d)}", callback_data=f"sd:{prev_d.isoformat()}"),
                InlineKeyboardButton(text="Сегодня", callback_data=f"sd:{today().isoformat()}"),
                InlineKeyboardButton(text=f"{fmt_day(next_d)} ›", callback_data=f"sd:{next_d.isoformat()}"),
            ],
            [InlineKeyboardButton(text="Неделя", callback_data=f"wk:{(day - timedelta(days=day.weekday())).isoformat()}")],
        ]
    )


def week_nav_kb(monday: date) -> InlineKeyboardMarkup:
    prev_m, next_m = monday - timedelta(days=7), monday + timedelta(days=7)
    cur = today() - timedelta(days=today().weekday())
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="‹ Пред.", callback_data=f"wk:{prev_m.isoformat()}"),
                InlineKeyboardButton(text="Текущая", callback_data=f"wk:{cur.isoformat()}"),
                InlineKeyboardButton(text="След. ›", callback_data=f"wk:{next_m.isoformat()}"),
            ]
        ]
    )


def tasks_keyboard(tasks, day: date):
    rows = []
    for t in tasks:
        mark = "✓" if t["done"] else "○"
        label = t["text"][:40] + (" · просрочено" if t["overdue"] else "")
        rows.append([InlineKeyboardButton(text=f"{mark}  {label}", callback_data=f"t:{t['id']}:{day.isoformat()}")])
    rows.append(
        [
            InlineKeyboardButton(text="+ Добавить", callback_data="add"),
            InlineKeyboardButton(text="Управление", callback_data="mg"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_keyboard(tasks):
    rows = []
    for t in tasks:
        if t["type"] == "daily":
            tag = "Ежедн."
        else:
            tag = f"{t['due_date'][8:10]}.{t['due_date'][5:7]}"
        rows.append([InlineKeyboardButton(text=f"✕  {tag} · {t['text'][:38]}", callback_data=f"d:{t['id']}")])
    rows.append([InlineKeyboardButton(text="Готово", callback_data="x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="На сегодня", callback_data="at:today"),
                InlineKeyboardButton(text="На завтра", callback_data="at:tomorrow"),
            ],
            [InlineKeyboardButton(text="Ежедневная", callback_data="at:daily")],
            [InlineKeyboardButton(text="Отмена", callback_data="ac")],
        ]
    )


# ---------- форматирование ----------
def lessons_text(lessons, with_date=False):
    if not lessons:
        return "<i>Занятий нет</i>"
    out = []
    current = None
    for l in lessons:
        if with_date and l["date"] != current:
            current = l["date"]
            if out:
                out.append("")
            out.append(f"<b>{fmt_day(date.fromisoformat(current))}</b>")
        t = l["start"] + (f"–{l['end']}" if l.get("end") else "")
        out.append(f"<code>{t}</code>  {esc(l['title'])}")
        if l.get("place"):
            out.append(f"└ <i>{esc(l['place'])}</i>")
    return "\n".join(out)


async def day_screen(uid: int, day: date):
    lessons = await db.get_lessons(uid, day.isoformat())
    mark = " · сегодня" if day == today() else ""
    return f"<b>Расписание</b> · {fmt_day(day)}{mark}\n{LINE}\n{lessons_text(lessons)}", day_nav_kb(day)


async def week_screen(uid: int, monday: date):
    sunday = monday + timedelta(days=6)
    lessons = await db.get_lessons(uid, monday.isoformat(), sunday.isoformat())
    head = f"<b>Неделя</b> · {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}"
    return f"{head}\n{LINE}\n{lessons_text(lessons, with_date=True)}", week_nav_kb(monday)


async def tasks_screen(uid: int, day: date):
    tasks = await db.tasks_for_day(uid, day.isoformat())
    text = f"<b>Задачи</b> · {fmt_day(day)}\n{LINE}\n" + ("Нажми на задачу, чтобы отметить." if tasks else "<i>Пока пусто</i>")
    return text, tasks_keyboard(tasks, day)


async def build_morning(chat_id: int, day: date, w):
    """Утреннее сообщение конкретного человека: его пары и его задачи. Погода общая (w)."""
    lessons = await db.get_lessons(chat_id, day.isoformat())
    tasks = await db.tasks_for_day(chat_id, day.isoformat())
    w_str = weather_mod.weather_text(w)
    advice = await ai.get_advice(w, lessons, tasks, w_str)

    text = (
        f"<b>Доброе утро</b>\n{WD_CAP[day.weekday()]}, {day.strftime('%d.%m.%Y')}\n\n"
        f"{LINE}\n<b>РАСПИСАНИЕ</b>\n{lessons_text(lessons)}\n\n"
        f"{LINE}\n{w_str}\n\n"
        f"{LINE}\n<b>СОВЕТ</b>\n{esc(advice)}\n\n"
        f"{LINE}\n<b>ЗАДАЧИ</b>" + ("" if tasks else "\n<i>Пока пусто</i>")
    )
    return text, tasks_keyboard(tasks, day)


async def send_morning():
    day = today()
    w = await weather_mod.get_weather(CITY, day, TZ)  # погоду берём один раз на всех
    for uid in OWNER_IDS:
        try:
            text, kb = await build_morning(uid, day, w)
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            print(f"[morning] {uid}: {e}")
            try:
                await bot.send_message(uid, f"Не удалось собрать утреннее сообщение: {esc(e)}")
            except Exception:
                pass


async def remind_schedule():
    await send_to_owners(
        "<b>Воскресенье</b>\nПришли фото или скриншот расписания на следующую неделю, и я его загружу."
    )


# ---------- команды ----------
@public.message(Command("start"))
async def cmd_start(m: Message):
    if not OWNER_IDS:
        await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>\nВпиши его в .env как OWNER_IDS=... и перезапусти бота.")
    elif m.from_user.id in OWNER_IDS:
        awaiting.pop(m.from_user.id, None)
        await m.answer(
            "<b>Astro</b> · утренний помощник\n"
            f"{LINE}\n"
            "Управление через меню внизу.\n\n"
            "<b>Команды</b>\n"
            "/morning — сводка на сегодня\n"
            "/schedule — расписание на сегодня\n"
            "/day <i>завтра | пт | 15.10</i> — расписание на любой день\n"
            "/week — расписание на неделю\n"
            "/addlesson — добавить пару\n"
            "/daily · /today · /tomorrow <i>текст</i> — добавить задачу\n"
            "/tasks — задачи на сегодня\n"
            "/settime <i>07:30</i> — время сводки (общее для всех)\n\n"
            "Фото расписания можно отправить в любой момент — я загружу его на нужную неделю.",
            reply_markup=main_menu(),
        )
    else:
        await m.answer(
            f"Тебя нет в списке доступа. Твой Telegram ID: <code>{m.from_user.id}</code>\n"
            "Отправь его владельцу бота, чтобы он тебя добавил."
        )


@owner.message(Command("morning"))
async def cmd_morning(m: Message):
    day = today()
    w = await weather_mod.get_weather(CITY, day, TZ)
    text, kb = await build_morning(m.from_user.id, day, w)
    await m.answer(text, reply_markup=kb)


async def _show_day(m: Message, day: date):
    text, kb = await day_screen(m.from_user.id, day)
    await m.answer(text, reply_markup=kb)


@owner.message(Command("schedule", "day"))
async def cmd_schedule(m: Message, command: CommandObject):
    day = parse_day(command.args)
    if day is None:
        return await m.answer(
            "Не понял день. Примеры:\n"
            "<code>/day завтра</code>\n"
            "<code>/day пт</code>\n"
            "<code>/day 15.10</code>\n"
            "<code>/day 2026-10-15</code>"
        )
    await _show_day(m, day)


@owner.message(Command("week"))
async def cmd_week(m: Message):
    d = today()
    text, kb = await week_screen(m.from_user.id, d - timedelta(days=d.weekday()))
    await m.answer(text, reply_markup=kb)


@owner.message(Command("addlesson"))
async def cmd_addlesson(m: Message, command: CommandObject):
    usage = "Формат: <code>/addlesson пн 09:00-10:30 Математика | ауд. 305</code>\nили с датой: <code>/addlesson 2026-10-12 09:00 Математика</code>"
    mt = re.match(r"^(\S+)\s+(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?\s+(.+)$", command.args or "")
    if not mt:
        return await m.answer(usage)
    day_s, start, end, rest = mt.groups()
    title, _, place = (p.strip() for p in rest.partition("|"))
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_s):
            day = date.fromisoformat(day_s)
        else:
            wd = WEEKDAYS.index(day_s.lower()[:2])
            day = today() + timedelta(days=(wd - today().weekday()) % 7)  # ближайший такой день
    except ValueError:
        return await m.answer(usage)
    await db.add_lesson(m.from_user.id, day.isoformat(), start.zfill(5), end.zfill(5) if end else None, title, place or None)
    await m.answer(f"Добавлено: <b>{fmt_day(day)}</b> · <code>{start}</code> · {esc(title)}")


async def _add_task(m: Message, command: CommandObject, type_, due, ok_text):
    text = (command.args or "").strip()
    if not text:
        return await m.answer("Напиши текст после команды, например: <code>/daily читать 30 минут</code>")
    await db.add_task(m.from_user.id, text, type_, due.isoformat() if due else None)
    await m.answer(f"{ok_text}: {esc(text)}")


@owner.message(Command("daily"))
async def cmd_daily(m: Message, command: CommandObject):
    await _add_task(m, command, "daily", None, "Ежедневная задача добавлена")


@owner.message(Command("today"))
async def cmd_today(m: Message, command: CommandObject):
    await _add_task(m, command, "once", today(), "Задача на сегодня добавлена")


@owner.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message, command: CommandObject):
    await _add_task(m, command, "once", today() + timedelta(days=1), "Задача на завтра добавлена")


@owner.message(Command("tasks"))
async def cmd_tasks(m: Message):
    text, kb = await tasks_screen(m.from_user.id, today())
    await m.answer(text, reply_markup=kb)


@owner.message(Command("settime"))
async def cmd_settime(m: Message, command: CommandObject):
    mt = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (command.args or "").strip())
    if not mt:
        return await m.answer("Формат: <code>/settime 07:30</code>")
    h, mi = int(mt.group(1)), int(mt.group(2))
    await db.set_setting("morning_time", f"{h:02d}:{mi:02d}")
    scheduler.reschedule_job("morning", trigger="cron", hour=h, minute=mi)
    await m.answer(f"Утренняя сводка (для всех) теперь в <b>{h:02d}:{mi:02d}</b>")


# ---------- кнопки главного меню ----------
@owner.message(F.text == BTN_MORNING)
async def btn_morning(m: Message):
    awaiting.pop(m.from_user.id, None)
    await cmd_morning(m)


@owner.message(F.text == BTN_TODAY)
async def btn_today(m: Message):
    awaiting.pop(m.from_user.id, None)
    await _show_day(m, today())


@owner.message(F.text == BTN_TOMORROW)
async def btn_tomorrow(m: Message):
    awaiting.pop(m.from_user.id, None)
    await _show_day(m, today() + timedelta(days=1))


@owner.message(F.text == BTN_WEEK)
async def btn_week(m: Message):
    awaiting.pop(m.from_user.id, None)
    await cmd_week(m)


@owner.message(F.text == BTN_TASKS)
async def btn_tasks(m: Message):
    awaiting.pop(m.from_user.id, None)
    await cmd_tasks(m)


@owner.message(F.text == BTN_ADD)
async def btn_add(m: Message):
    awaiting.pop(m.from_user.id, None)
    await m.answer("<b>Новая задача</b>\nВыбери тип:", reply_markup=add_kind_keyboard())


# Текст задачи после выбора типа
@owner.message(F.text, lambda m: m.from_user.id in awaiting and not m.text.startswith("/") and m.text not in MENU_TEXTS)
async def on_task_text(m: Message):
    type_, offset, ok_text = TASK_KINDS[awaiting.pop(m.from_user.id)]
    due = (today() + timedelta(days=offset)).isoformat() if offset is not None else None
    text = m.text.strip()
    await db.add_task(m.from_user.id, text, type_, due)
    await m.answer(f"{ok_text}\n└ {esc(text)}")


# ---------- навигация по расписанию ----------
async def _safe_edit(c: CallbackQuery, text: str, kb):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # сообщение не изменилось


@owner.callback_query(F.data.startswith("sd:"))
async def cb_day(c: CallbackQuery):
    day = date.fromisoformat(c.data.split(":", 1)[1])
    text, kb = await day_screen(c.from_user.id, day)
    await _safe_edit(c, text, kb)
    await c.answer()


@owner.callback_query(F.data.startswith("wk:"))
async def cb_week(c: CallbackQuery):
    monday = date.fromisoformat(c.data.split(":", 1)[1])
    text, kb = await week_screen(c.from_user.id, monday)
    await _safe_edit(c, text, kb)
    await c.answer()


# ---------- расписание с фото ----------
pending = {}  # (chat_id, id сообщения-статуса) -> (monday, sunday, lessons)


def target_week(caption):
    """Какую неделю загружаем. В пятницу-воскресенье по умолчанию следующую.
    Подпись к фото 'эта' или 'следующая' это переопределяет."""
    d = today()
    c = (caption or "").lower()
    if "след" in c:
        nxt = True
    elif "эта" in c or "тек" in c:
        nxt = False
    else:
        nxt = d.weekday() >= 5
    monday = d - timedelta(days=d.weekday()) + timedelta(days=7 if nxt else 0)
    return monday, monday + timedelta(days=6)


@owner.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_schedule_photo(m: Message):
    status = await m.answer("Читаю расписание с изображения…")
    if m.photo:
        file_id, mime = m.photo[-1].file_id, "image/jpeg"
    else:
        file_id, mime = m.document.file_id, m.document.mime_type
    monday, sunday = target_week(m.caption)
    try:
        buf = await bot.download(file_id)
        lessons = await schedule_sync.extract_lessons_from_image(buf.read(), mime, monday, sunday)
    except schedule_sync.SyncError as e:
        return await status.edit_text(f"{esc(e)}")
    except Exception as e:
        print(f"[photo] error: {e}")
        return await status.edit_text(f"Не получилось прочитать изображение: {esc(e)}")

    if not lessons:
        return await status.edit_text(
            "Не нашёл занятий на этой неделе. Попробуй сфотографировать ровнее и крупнее или пришли файлом."
        )
    pending[(m.from_user.id, status.message_id)] = (monday, sunday, lessons)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✓  Сохранить", callback_data=f"ls:{status.message_id}"),
                InlineKeyboardButton(text="✕  Отмена", callback_data=f"lc:{status.message_id}"),
            ]
        ]
    )
    await status.edit_text(
        f"<b>Неделя</b> · {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}\n"
        f"Найдено занятий: {len(lessons)}\n{LINE}\n{lessons_text(lessons, with_date=True)}\n\n"
        "Всё верно?",
        reply_markup=kb,
    )


@owner.callback_query(F.data.startswith("ls:"))
async def cb_save_lessons(c: CallbackQuery):
    data = pending.pop((c.from_user.id, int(c.data.split(":")[1])), None)
    if not data:
        await c.answer("Устарело, пришли фото ещё раз", show_alert=True)
        return
    monday, sunday, lessons = data
    await db.replace_site_lessons(c.from_user.id, monday.isoformat(), sunday.isoformat(), lessons)
    await c.message.edit_text(
        f"<b>Сохранено</b> · {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}\n"
        f"Занятий: {len(lessons)}\n{LINE}\n{lessons_text(lessons, with_date=True)}"
    )
    await c.answer()


@owner.callback_query(F.data.startswith("lc:"))
async def cb_cancel_lessons(c: CallbackQuery):
    pending.pop((c.from_user.id, int(c.data.split(":")[1])), None)
    await c.message.edit_text("Отменено. Можешь прислать другое фото.")
    await c.answer()


# ---------- задачи (кнопки) ----------
@owner.callback_query(F.data.startswith("t:"))
async def cb_toggle(c: CallbackQuery):
    _, task_id, day_s = c.data.split(":")
    if await db.toggle_task(c.from_user.id, int(task_id), day_s) is None:
        return await c.answer("Задача не найдена", show_alert=True)
    tasks = await db.tasks_for_day(c.from_user.id, day_s)
    try:
        await c.message.edit_reply_markup(reply_markup=tasks_keyboard(tasks, date.fromisoformat(day_s)))
    except Exception:
        pass  # сообщение не изменилось
    await c.answer()


@owner.callback_query(F.data == "add")
async def cb_add(c: CallbackQuery):
    await c.message.answer("<b>Новая задача</b>\nВыбери тип:", reply_markup=add_kind_keyboard())
    await c.answer()


@owner.callback_query(F.data.startswith("at:"))
async def cb_add_kind(c: CallbackQuery):
    kind = c.data.split(":", 1)[1]
    if kind not in TASK_KINDS:
        return await c.answer()
    awaiting[c.from_user.id] = kind
    label = {"today": "на сегодня", "tomorrow": "на завтра", "daily": "ежедневная"}[kind]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="ac")]])
    await c.message.edit_text(f"<b>Новая задача</b> · {label}\nОтправь текст одним сообщением.", reply_markup=kb)
    await c.answer()


@owner.callback_query(F.data == "ac")
async def cb_add_cancel(c: CallbackQuery):
    awaiting.pop(c.from_user.id, None)
    await c.message.edit_text("Отменено.")
    await c.answer()


@owner.callback_query(F.data == "mg")
async def cb_manage(c: CallbackQuery):
    tasks = await db.list_active_tasks(c.from_user.id)
    if not tasks:
        return await c.answer("Задач нет", show_alert=True)
    await c.message.answer("<b>Управление задачами</b>\nНажми на задачу, чтобы удалить.", reply_markup=delete_keyboard(tasks))
    await c.answer()


@owner.callback_query(F.data.startswith("d:"))
async def cb_delete(c: CallbackQuery):
    await db.delete_task(c.from_user.id, int(c.data.split(":")[1]))
    tasks = await db.list_active_tasks(c.from_user.id)
    if tasks:
        await c.message.edit_reply_markup(reply_markup=delete_keyboard(tasks))
    else:
        await c.message.edit_text("Задач больше нет.")
    await c.answer("Удалено")


@owner.callback_query(F.data == "x")
async def cb_close(c: CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer()


# ---------- запуск ----------
async def main():
    await db.init_db()
    if OWNER_IDS:  # старые данные (до разделения по людям) достаются первому из списка
        await db.assign_legacy(OWNER_IDS[0])

    morning = await db.get_setting("morning_time", DEFAULT_MORNING)
    h, mi = map(int, morning.split(":"))
    scheduler.add_job(send_morning, "cron", hour=h, minute=mi, id="morning")
    scheduler.add_job(remind_schedule, "cron", day_of_week="sun", hour=SYNC_HOUR, minute=0, id="remind")
    scheduler.start()

    dp.include_router(public)
    dp.include_router(owner)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="morning", description="Сводка на сегодня"),
            BotCommand(command="schedule", description="Расписание на сегодня"),
            BotCommand(command="day", description="Расписание на день: завтра, пт, 15.10"),
            BotCommand(command="week", description="Расписание на неделю"),
            BotCommand(command="addlesson", description="Добавить пару"),
            BotCommand(command="daily", description="Ежедневная задача"),
            BotCommand(command="today", description="Задача на сегодня"),
            BotCommand(command="tomorrow", description="Задача на завтра"),
            BotCommand(command="tasks", description="Задачи на сегодня"),
            BotCommand(command="settime", description="Время утренней сводки"),
        ]
    )
    print("Готов к бою")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())