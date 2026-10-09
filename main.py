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
    Message,
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


def today() -> date:
    return datetime.now(TZ).date()


def esc(s) -> str:
    return html.escape(str(s))


async def send_to_owners(text: str, reply_markup=None):
    """Одинаковое сообщение всем из списка (используется для напоминания)."""
    for owner_id in OWNER_IDS:
        try:
            await bot.send_message(owner_id, text, reply_markup=reply_markup)
        except Exception as e:
            print(f"[send_to_owners] Не удалось отправить сообщение для ID {owner_id}: {e}")


# ---------- форматирование ----------
def lessons_text(lessons, with_date=False):
    if not lessons:
        return "Занятий нет 🎉"
    lines = []
    for l in lessons:
        t = l["start"] + (f"–{l['end']}" if l.get("end") else "")
        place = f" ({esc(l['place'])})" if l.get("place") else ""
        prefix = ""
        if with_date:
            d = date.fromisoformat(l["date"])
            prefix = f"<b>{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}</b> "
        time_fmt = f"<code>{t}</code>"
        title_fmt = f"<b>{esc(l['title'])}</b>"
        place_fmt = f"📍 <i>{esc(l['place'])}</i>" if l.get("place") else ""

        lines.append(f"🔹 {prefix}{time_fmt} — {title_fmt} {place_fmt}".strip())
    return "\n".join(lines)


def tasks_keyboard(tasks, day: date):
    rows = []
    for t in tasks:
        mark = "✅" if t["done"] else "⬜"
        label = t["text"][:48] + (" ⏰" if t["overdue"] else "")
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"t:{t['id']}:{day.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def delete_keyboard(tasks):
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 {'🔁' if t['type'] == 'daily' else '📌 ' + t['due_date'][5:]} {t['text'][:40]}",
                callback_data=f"d:{t['id']}",
            )
        ]
        for t in tasks
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_morning(chat_id: int, day: date, w):
    """Утреннее сообщение конкретного человека: его пары и его задачи. Погода общая (w)."""
    lessons = await db.get_lessons(chat_id, day.isoformat())
    tasks = await db.tasks_for_day(chat_id, day.isoformat())
    w_str = weather_mod.weather_text(w)
    advice = await ai.get_advice(w, lessons, tasks, w_str)

    text = (
        f"☀️ <b>Доброе утро!</b> {WEEKDAYS[day.weekday()].upper()}, {day.strftime('%d.%m.%Y')}\n\n"
        f"📚 <b>Расписание</b>\n{lessons_text(lessons)}\n\n"
        f"{w_str}\n\n"
        f"🤖 <b>Совет</b>\n{esc(advice)}\n\n"
        f"📝 <b>Дела на сегодня</b>" + ("" if tasks else "\nПока пусто. Добавь через /daily или /today")
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
                await bot.send_message(uid, f"⚠️ Не удалось собрать утреннее сообщение: {esc(e)}")
            except Exception:
                pass


async def remind_schedule():
    await send_to_owners(
        "📸 Воскресенье! Пришли фото или скриншот расписания на следующую неделю, и я его загружу."
    )


# ---------- команды ----------
@public.message(Command("start"))
async def cmd_start(m: Message):
    if not OWNER_IDS:
        await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>\nВпиши его в .env как OWNER_IDS=... и перезапусти бота.")
    elif m.from_user.id in OWNER_IDS:
        await m.answer(
            "Привет, Я Astro! Твой утренний помощник.\n\n"
            "/morning — утреннее сообщение прямо сейчас\n"
            "/schedule — расписание на сегодня\n"
            "/week — расписание на неделю\n"
            "/addlesson — добавить пару\n"
            "/daily текст — ежедневная задача\n"
            "/today текст — задача только на сегодня\n"
            "/tomorrow текст — задача на завтра\n"
            "/tasks — список задач и удаление\n"
            "📸 Пришли фото расписания — я загружу его на нужную неделю\n"
            "/settime 07:30 — время утреннего сообщения (общее для всех)"
        )
    else:
        await m.answer(
            f"Тебя нет в списке доступа. Твой Telegram ID: <code>{m.from_user.id}</code>\n"
            "Отправь его владельцу бота, чтобы он тебя добавил."
        )
@public.message(Command("nikitos"))
async def cmd_nikitos(m: Message):
    await m.answer("Никитосик Хуесосик")

@owner.message(Command("morning"))
async def cmd_morning(m: Message):
    day = today()
    w = await weather_mod.get_weather(CITY, day, TZ)
    text, kb = await build_morning(m.from_user.id, day, w)
    await m.answer(text, reply_markup=kb)


@owner.message(Command("schedule"))
async def cmd_schedule(m: Message):
    lessons = await db.get_lessons(m.from_user.id, today().isoformat())
    await m.answer(f"📚 <b>Сегодня</b>\n{lessons_text(lessons)}")


@owner.message(Command("week"))
async def cmd_week(m: Message):
    d = today()
    monday = d - timedelta(days=d.weekday())
    lessons = await db.get_lessons(m.from_user.id, monday.isoformat(), (monday + timedelta(days=6)).isoformat())
    await m.answer(f"📚 <b>Эта неделя</b>\n{lessons_text(lessons, with_date=True)}")


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
    await m.answer(f"✅ Добавлено: {WEEKDAYS[day.weekday()]} {day.strftime('%d.%m')} {start} {esc(title)}")


async def _add_task(m: Message, command: CommandObject, type_, due, ok_text):
    text = (command.args or "").strip()
    if not text:
        return await m.answer("Напиши текст после команды, например: <code>/daily читать 30 минут</code>")
    await db.add_task(m.from_user.id, text, type_, due.isoformat() if due else None)
    await m.answer(f"{ok_text}: {esc(text)}")


@owner.message(Command("daily"))
async def cmd_daily(m: Message, command: CommandObject):
    await _add_task(m, command, "daily", None, "🔁 Ежедневная задача добавлена")


@owner.message(Command("today"))
async def cmd_today(m: Message, command: CommandObject):
    await _add_task(m, command, "once", today(), "📌 Задача на сегодня добавлена")


@owner.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message, command: CommandObject):
    await _add_task(m, command, "once", today() + timedelta(days=1), "📌 Задача на завтра добавлена")


@owner.message(Command("tasks"))
async def cmd_tasks(m: Message):
    tasks = await db.list_active_tasks(m.from_user.id)
    if not tasks:
        return await m.answer("Задач нет. Добавь через /daily или /today")
    await m.answer("Нажми на задачу, чтобы удалить:", reply_markup=delete_keyboard(tasks))


@owner.message(Command("settime"))
async def cmd_settime(m: Message, command: CommandObject):
    mt = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (command.args or "").strip())
    if not mt:
        return await m.answer("Формат: <code>/settime 07:30</code>")
    h, mi = int(mt.group(1)), int(mt.group(2))
    await db.set_setting("morning_time", f"{h:02d}:{mi:02d}")
    scheduler.reschedule_job("morning", trigger="cron", hour=h, minute=mi)
    await m.answer(f"⏰ Утреннее сообщение (для всех) теперь в {h:02d}:{mi:02d}")


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
    status = await m.answer("🔍 Читаю расписание с изображения…")
    if m.photo:
        file_id, mime = m.photo[-1].file_id, "image/jpeg"
    else:
        file_id, mime = m.document.file_id, m.document.mime_type
    monday, sunday = target_week(m.caption)
    try:
        buf = await bot.download(file_id)
        lessons = await schedule_sync.extract_lessons_from_image(buf.read(), mime, monday, sunday)
    except schedule_sync.SyncError as e:
        return await status.edit_text(f"⚠️ {esc(e)}")
    except Exception as e:
        print(f"[photo] error: {e}")
        return await status.edit_text(f"⚠️ Не получилось прочитать изображение: {esc(e)}")

    if not lessons:
        return await status.edit_text(
            "Не нашёл занятий на этой неделе. Попробуй сфотографировать ровнее и крупнее или пришли файлом."
        )
    pending[(m.from_user.id, status.message_id)] = (monday, sunday, lessons)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💾 Сохранить", callback_data=f"ls:{status.message_id}"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data=f"lc:{status.message_id}"),
            ]
        ]
    )
    await status.edit_text(
        f"📅 <b>Неделя {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}</b>, "
        f"найдено занятий: {len(lessons)}\n\n{lessons_text(lessons, with_date=True)}\n\nВсё верно?",
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
        f"✅ Расписание на {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')} сохранено "
        f"({len(lessons)} занятий).\n\n{lessons_text(lessons, with_date=True)}"
    )
    await c.answer()


@owner.callback_query(F.data.startswith("lc:"))
async def cb_cancel_lessons(c: CallbackQuery):
    pending.pop((c.from_user.id, int(c.data.split(":")[1])), None)
    await c.message.edit_text("Отменено. Можешь прислать другое фото.")
    await c.answer()


# ---------- кнопки ----------
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


@owner.callback_query(F.data.startswith("d:"))
async def cb_delete(c: CallbackQuery):
    await db.delete_task(c.from_user.id, int(c.data.split(":")[1]))
    tasks = await db.list_active_tasks(c.from_user.id)
    await c.message.edit_reply_markup(reply_markup=delete_keyboard(tasks))
    await c.answer("Удалено")


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
            BotCommand(command="morning", description="Утреннее сообщение сейчас"),
            BotCommand(command="schedule", description="Расписание на сегодня"),
            BotCommand(command="week", description="Расписание на неделю"),
            BotCommand(command="addlesson", description="Добавить пару"),
            BotCommand(command="daily", description="Ежедневная задача"),
            BotCommand(command="today", description="Задача на сегодня"),
            BotCommand(command="tomorrow", description="Задача на завтра"),
            BotCommand(command="tasks", description="Список задач"),
            BotCommand(command="settime", description="Время утреннего сообщения"),
        ]
    )
    print("Готов к бою")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
