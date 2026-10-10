import json
import os
import re
from datetime import date, timedelta
from html.parser import HTMLParser



import ai
import database as db


class SyncError(Exception):
    pass


class _TextExtractor(HTMLParser):
    BLOCK = {"tr", "br", "p", "div", "li", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()





def next_week_bounds(today):
    monday = today + timedelta(days=7 - today.weekday())  # в воскресенье это завтра
    return monday, monday + timedelta(days=6)


def _week_hint(monday):
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    return "\n".join(f"{names[i]} = {(monday + timedelta(days=i)).isoformat()}" for i in range(7))


_JSON_FORMAT = """Верни ТОЛЬКО JSON-массив объектов вида:
{"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM или null", "title": "название", "place": "аудитория/место или null"}
Если занятий нет, верни []. Не выдумывай занятия, которых не видно."""


DEFAULT_BELL = (
    "1 пара 08:00-08:45, 2 пара 08:55-09:40, 3 пара 10:10-10:55, 4 пара 11:05-11:50, "
    "5 пара 13:45-14:30, 6 пара 14:40-15:25, 7 пара 15:55-16:40, 8 пара 16:50-17:35, "
    "9 пара 18:45-19:30, 10 пара 19:40-20:25, 11 пара 20:35-21:20"
)  # расписание звонков по скриншоту из личного кабинета; можно переопределить через BELL_SCHEDULE


def _week_number(monday):
    """Номер учебной недели, если в .env задан SEMESTER_START (понедельник 1-й недели, YYYY-MM-DD)."""
    try:
        start = date.fromisoformat(os.getenv("SEMESTER_START", ""))
    except ValueError:
        return None
    return (monday - start).days // 7 + 1


def _chinese_notes(monday):
    bell = os.getenv("BELL_SCHEDULE", DEFAULT_BELL)
    wk = _week_number(monday)
    week_line = (
        f"Нужная неделя — учебная неделя №{wk}. Включай только предметы, у которых эта неделя входит в указанные "
        f"недели (например 1-16周, а также 单周 = нечётные, 双周 = чётные недели)."
        if wk
        else "Если у предметов указаны учебные недели (например 1-16周), а номер нужной недели неизвестен, "
        "включай все предметы."
    )
    return f"""Проанализируй изображение расписания (текст может быть на китайском) и переведи на русский язык.

    ### 1. Форматирование полей
    • title: Перевод названия на русский + (оригинал на китайском). Исключай служебные метки вроде (留学生).
      Пример: "Высшая математика (高等数学)"
    • place: Перевод названия здания + оригинальные номера корпусов/аудиторий.
      Пример: "Здание Ридинг (雷丁楼), ауд. S206" или "корпус A, ауд. 301"

    ### 2. Дни недели и время
    • Сопоставление дней: 周一/星期一=пн, 周二=вт, 周三=ср, 周四=чт, 周五=пт, 周六=сб, 周日/星期日=вс.
    • Если шапка дней отсутствует, колонки идут слева направо (пн...вс). Пустые колонки пропускай.
    • Номера пар (например, 第1-2节) конвертируй в точное время по сетке звонков:
    {bell}
    • Если занятие идет несколько пар подряд: start = начало первой пары, end = конец последней.

    ### 3. Особенности сетки и слитного текста
    • Цветные блоки занимают строки пар, напротив которых расположены.
    • В слитных строках вида "综合汉语(1)(留学生)雷丁楼S206" обязательно разделяй название и аудиторию:
      - title: Комплексный китайский (1) (综合汉语)
      - place: Здание Ридинг (雷丁楼), ауд. S206

    {week_line}"""


def _parse_lessons(raw, monday, sunday):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise SyncError("Gemini вернул ответ, который не получилось разобрать.")
    if not isinstance(data, list):
        raise SyncError("Gemini вернул неожиданный формат.")

    clean = []
    for l in data:
        try:
            d = date.fromisoformat(str(l["date"]))
            start = str(l["start"]).strip()
            if not (monday <= d <= sunday) or not re.fullmatch(r"\d{1,2}:\d{2}", start):
                continue
            end = l.get("end")
            end = str(end).strip() if end and re.fullmatch(r"\d{1,2}:\d{2}", str(end).strip()) else None
            clean.append(
                {
                    "date": d.isoformat(),
                    "start": start.zfill(5),
                    "end": end.zfill(5) if end else None,
                    "title": str(l["title"]).strip(),
                    "place": (str(l["place"]).strip() if l.get("place") else None),
                }
            )
        except (KeyError, ValueError, TypeError):
            continue
    return clean


async def extract_lessons(text, monday, sunday):
    prompt = f"""Ниже текст страницы сайта с расписанием.
Найди все занятия на период с {monday.isoformat()} по {sunday.isoformat()}.
Если на странице дни недели без дат, считай, что это именно эта неделя:
{_week_hint(monday)}
{_chinese_notes(monday)}
{_JSON_FORMAT}

ТЕКСТ СТРАНИЦЫ:
{text}
"""
    return _parse_lessons(await ai.generate(prompt, json_mode=True), monday, sunday)


async def extract_lessons_from_image(image, mime, monday, sunday):
    """Читает расписание с фото или скриншота."""
    prompt = f"""На изображении расписание занятий (таблица, скриншот или фото).
Выпиши все занятия на неделю с {monday.isoformat()} по {sunday.isoformat()}.
Если на изображении указаны только дни недели, сопоставь их с датами:
{_week_hint(monday)}
Если время указано в часах, бери его точно как на изображении.
{_chinese_notes(monday)}
{_JSON_FORMAT}
"""
    raw = await ai.generate(prompt, json_mode=True, image=image, mime=mime)
    return _parse_lessons(raw, monday, sunday)





async def sync_next_week(today):
    """Скачивает расписание, сохраняет следующую неделю. Возвращает (monday, sunday, lessons)."""
    url = os.getenv("SCHEDULE_URL")
    if not url:
        raise SyncError("В .env не указан SCHEDULE_URL.")
    monday, sunday = next_week_bounds(today)
    text = await fetch_page_text(url)
    lessons = await extract_lessons(text, monday, sunday)
    await db.replace_site_lessons(monday.isoformat(), sunday.isoformat(), lessons)
    return monday, sunday, lessons