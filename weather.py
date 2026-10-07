import os
from datetime import datetime

import aiohttp

API = "https://api.openweathermap.org/data/2.5"


def fmt_t(t):
    return f"{t:+.0f}°"


async def _get(session, endpoint, city):
    params = {
        "q": city,
        "appid": os.getenv("OPENWEATHER_API_KEY"),
        "units": "metric",
        "lang": "ru",
    }
    async with session.get(f"{API}/{endpoint}", params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json()


async def get_weather(city, day, tz):
    """Возвращает словарь с погодой на день или None, если API недоступен."""
    try:
        async with aiohttp.ClientSession() as session:
            current = await _get(session, "weather", city)
            forecast = await _get(session, "forecast", city)
    except Exception as e:  # сеть, неверный ключ и т.д.
        print(f"[weather] error: {e}")
        return None

    slots = []
    for item in forecast.get("list", []):
        dt = datetime.fromtimestamp(item["dt"], tz)
        if dt.date() == day:
            slots.append(
                {
                    "hour": dt.hour,
                    "temp": item["main"]["temp"],
                    "feels": item["main"]["feels_like"],
                    "desc": item["weather"][0]["description"],
                    "pop": item.get("pop", 0) * 100,
                    "wind": item["wind"]["speed"],
                }
            )
    if not slots:  # прогноз на сегодня мог уже закончиться
        slots = [
            {
                "hour": datetime.now(tz).hour,
                "temp": current["main"]["temp"],
                "feels": current["main"]["feels_like"],
                "desc": current["weather"][0]["description"],
                "pop": 0,
                "wind": current["wind"]["speed"],
            }
        ]

    return {
        "city": city,
        "now_temp": current["main"]["temp"],
        "now_feels": current["main"]["feels_like"],
        "now_desc": current["weather"][0]["description"],
        "tmin": min(s["temp"] for s in slots),
        "tmax": max(s["temp"] for s in slots),
        "pop_max": max(s["pop"] for s in slots),
        "wind_max": max(s["wind"] for s in slots),
        "slots": slots,
    }


def weather_text(w):
    if not w:
        return "🌤 Погода сейчас недоступна."
    return (
        f"🌡 Сейчас {fmt_t(w['now_temp'])} (ощущается {fmt_t(w['now_feels'])}), {w['now_desc']}\n"
        f"📈 За день: от {fmt_t(w['tmin'])} до {fmt_t(w['tmax'])}, "
        f"осадки до {w['pop_max']:.0f}%, ветер до {w['wind_max']:.0f} м/с"
    )
