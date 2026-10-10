import asyncio
import os

from google import genai
from google.genai import types

MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash"
# Если модель недоступна (404) или отвечает пусто, бот сам пробует запасные
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-flash-latest"]
# Ошибки, при которых стоит повторить или попробовать другую модель
RETRYABLE = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "404", "NOT_FOUND")
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def _config(json_mode, thinking, big):
    # Токены "размышления" Gemini 3 входят в max_output_tokens. Если не ограничить размышление,
    # оно может съесть весь лимит, и ответ придёт пустым или обрезанным (особенно с картинкой).
    kwargs = {"max_output_tokens": 16384 if big else 8192}
    if json_mode:
        kwargs["response_mime_type"] = "application/json"
    if thinking:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
    return types.GenerateContentConfig(**kwargs)


def _finish_reason(resp):
    try:
        return resp.candidates[0].finish_reason
    except Exception:
        return "unknown"


async def generate(prompt, json_mode=False, image=None, mime="image/jpeg"):
    contents = [types.Part.from_bytes(data=image, mime_type=mime), prompt] if image else prompt
    timeout = 90 if image else 20
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    last_error = None

    for attempt in range(3):  # 3 круга по всем моделям, между кругами пауза
        if attempt:
            await asyncio.sleep(4 * attempt)

        for model in models:
            thinking = True  # низкий уровень размышления; если модель его не поддерживает, отключим ниже
            while True:
                try:
                    print(f"[ai] запрос к {model}, круг {attempt + 1}")
                    resp = await asyncio.wait_for(
                        _get_client().aio.models.generate_content(
                            model=model, contents=contents, config=_config(json_mode, thinking, bool(image))
                        ),
                        timeout,
                    )
                    text = resp.text or ""
                    if text.strip():
                        return text
                    reason = _finish_reason(resp)
                    print(f"[ai] {model}: пустой ответ (finish_reason={reason})")
                    last_error = RuntimeError(f"пустой ответ от {model} ({reason})")
                    break  # следующая модель
                except asyncio.TimeoutError:
                    print(f"[ai] {model} превысил тайм-аут ({timeout}с), переключаемся...")
                    last_error = RuntimeError(f"тайм-аут {model}")
                    break  # следующая модель
                except Exception as e:
                    msg = str(e)
                    if thinking and "thinking" in msg.lower():
                        thinking = False
                        continue
                    if any(k in msg for k in RETRYABLE):
                        print(f"[ai] {model}: {msg[:200]}")
                        last_error = e
                        break  # следующая модель
                    raise

    print(f"[ai] все попытки неудачны: {last_error}")
    raise RuntimeError(f"Gemini не смог обработать запрос ({str(last_error)[:150]}). Попробуй ещё раз чуть позже.")


def _fallback_advice(w):
    """Простое правило, если Gemini недоступен."""
    if not w:
        return "Погоду узнать не удалось, посмотри в окно перед выходом."
    tips = []
    t = w["tmax"]
    if t < 5:
        tips.append("тёплая куртка, шапка, перчатки")
    elif t < 15:
        tips.append("куртка или худи с курткой")
    elif t < 24:
        tips.append("лёгкая кофта или рубашка")
    else:
        tips.append("лёгкая одежда, футболка")
    if w["pop_max"] >= 40:
        tips.append("возьми зонт")
    if w["wind_max"] >= 8:
        tips.append("будет ветрено")
    return "Совет: " + ", ".join(tips) + "."


async def get_advice(weather, lessons, tasks, weather_str):
    lessons_str = "\n".join(
        f"- {l['start']}-{l.get('end') or '?'} {l['title']} ({l.get('place') or 'место не указано'})"
        for l in lessons
    ) or "занятий нет"
    tasks_str = "\n".join(f"- {t['text']}" for t in tasks) or "задач нет"

    prompt = f"""Ты личный утренний помощник. Ответь по-русски, дружелюбно и коротко (не более 6 строк), без markdown и без звёздочек.
Дай: 1) что надеть с учётом погоды и маршрута в течение дня (куртка, зонт, обувь и т.д.);
2) Напоминай про ежедневные задачи

Погода ({weather['city'] if weather else 'неизвестно'}):
{weather_str}

Расписание:
{lessons_str}

Дела на сегодня:
{tasks_str}
"""
    try:
        text = (await generate(prompt)).strip()
        return text or _fallback_advice(weather)
    except Exception as e:
        print(f"[ai] error: {e}")
        return _fallback_advice(weather)