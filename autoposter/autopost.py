"""
Автопостер канала про курортную недвижимость.

При каждом запуске:
  1. Берёт свежий «угол подачи» из ротации тем и список недавних заголовков.
  2. Генерирует новый пост через Claude (claude-opus-4-8, структурированный вывод).
  3. Публикует его в Telegram-канал через Bot API.
  4. Записывает заголовок в history.json, чтобы посты не повторялись.

Запуск разово:   python autopost.py
Цикл по таймеру: python autopost.py --loop 6h   (постит каждые 6 часов)

Нужны переменные окружения (см. .env.example):
  ANTHROPIC_API_KEY  — ключ Claude API
  BOT_TOKEN          — токен бота из @BotFather (бот должен быть админом канала)
  CHANNEL_ID         — @username канала или числовой id (-100...)
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from pydantic import BaseModel, Field

# ---------- Конфигурация ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]            # @username или -100…
MODEL = os.environ.get("MODEL", "claude-opus-4-8")

HISTORY_PATH = Path(__file__).with_name("history.json")
HISTORY_KEEP = 40                                # сколько заголовков помнить для антиповтора

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autopost")

# Бренд/контекст канала — подставляется в системный промпт.
CHANNEL_BRIEF = (
    "Канал про курортную недвижимость Краснодарского края: Сочи, Большой Сочи "
    "(Лазаревское, Дагомыс, Адлер, Имеретинка), Анапа, Геленджик, Кубань. "
    "Аудитория — частные инвесторы и покупатели жилья у моря: квартиры, апартаменты, "
    "дома, новостройки, доходная недвижимость под аренду."
)

# Ротация «углов подачи», чтобы посты не были однообразными.
ANGLES = [
    "Инвестиционный разбор: доходность аренды конкретного формата жилья у моря",
    "Гайд по району: чем живёт локация, кому подходит, плюсы и минусы для покупателя",
    "Разбор ошибки покупателя курортной недвижимости и как её избежать",
    "Сравнение двух форматов (например, апартаменты vs квартира, новостройка vs вторичка)",
    "Сезонность рынка: что происходит с ценами и спросом в это время года",
    "Юридический момент сделки простым языком (что проверить перед покупкой)",
    "Чек-лист: как выбрать объект под сдачу в аренду посуточно",
    "Тренд рынка курортной недвижимости и что он значит для покупателя",
    "Мини-кейс: как считается окупаемость объекта у моря на цифрах",
    "Ответ на частый вопрос подписчиков о покупке жилья на юге",
]


# ---------- Структура поста ----------
class Post(BaseModel):
    title: str = Field(description="Цепляющий заголовок поста, до 80 символов, без хэштегов и эмодзи в начале")
    body: str = Field(description="Тело поста: 600–900 символов, обычный текст с переносами строк, 1–3 уместных эмодзи, без markdown и html")
    hashtags: list[str] = Field(description="3–5 релевантных хэштегов на русском без пробелов, каждый начинается с #")


SYSTEM_PROMPT = (
    "Ты — опытный SMM-редактор и эксперт по курортной недвижимости. "
    "Пишешь живые, полезные посты для Telegram-канала, без воды и кликбейта. "
    f"{CHANNEL_BRIEF}\n\n"
    "Правила:\n"
    "- Конкретика и польза: цифры, примеры, практические выводы.\n"
    "- Тон — экспертный, дружелюбный, на «вы».\n"
    "- Никакой markdown-разметки (* _ #) внутри тела — только обычный текст и переносы строк.\n"
    "- Не выдумывай несуществующие ЖК, точные цены и проценты как факты; используй реалистичные ориентиры "
    "с оговоркой («ориентировочно», «в среднем»), если приводишь числа.\n"
    "- Без обещаний гарантированной доходности и юридических гарантий.\n"
    "- В конце тела допустим мягкий призыв к действию (подписаться/написать), но не навязчивый."
)


def generate_post(client: anthropic.Anthropic, angle: str, recent_titles: list[str]) -> Post:
    avoid = "\n".join(f"- {t}" for t in recent_titles) or "(пока пусто)"
    user = (
        f"Сгенерируй один пост для канала. Угол подачи на сегодня: «{angle}».\n\n"
        f"Темы/заголовки последних постов — НЕ повторяй их по смыслу и формулировкам:\n{avoid}\n\n"
        "Выдай результат строго по схеме (title, body, hashtags)."
    )
    response = client.messages.parse(
        model=MODEL,
        max_tokens=2000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_format=Post,
    )
    return response.parsed_output


def render(post: Post) -> str:
    """Собирает Telegram-сообщение в безопасном HTML."""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in post.hashtags)
    parts = [f"<b>{esc(post.title.strip())}</b>", "", esc(post.body.strip())]
    if tags:
        parts += ["", esc(tags)]
    text = "\n".join(parts)
    return text[:4000]  # запас под лимит Telegram (4096)


def publish(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    log.info("Опубликовано в %s (message_id=%s)", CHANNEL_ID, data["result"]["message_id"])


# ---------- История ----------
def load_history() -> list[dict]:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_PATH.write_text(
        json.dumps(history[-HISTORY_KEEP:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_once() -> None:
    client = anthropic.Anthropic()  # ключ из ANTHROPIC_API_KEY
    history = load_history()
    recent_titles = [h["title"] for h in history[-HISTORY_KEEP:]]

    # Угол подачи, который дольше всего не использовался.
    used = [h.get("angle") for h in history]
    angle = min(ANGLES, key=lambda a: _last_index(used, a))
    if used and used[-1] == angle:        # подстраховка от повтора подряд
        angle = random.choice([a for a in ANGLES if a != angle])

    log.info("Генерирую пост. Угол: %s", angle)
    post = generate_post(client, angle, recent_titles)
    text = render(post)
    publish(text)

    history.append({
        "title": post.title.strip(),
        "angle": angle,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    save_history(history)


def _last_index(seq: list, value) -> int:
    """Индекс последнего вхождения value; -1 если не встречалось (значит — самый «старый»)."""
    for i in range(len(seq) - 1, -1, -1):
        if seq[i] == value:
            return i
    return -1


def parse_interval(s: str) -> int:
    """'6h' -> 21600, '30m' -> 1800, '90' -> 90 (секунды)."""
    m = re.fullmatch(r"(\d+)([smhd]?)", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("Интервал в формате 30m / 6h / 1d / 3600")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def main() -> None:
    ap = argparse.ArgumentParser(description="Автопостер канала про курортную недвижимость")
    ap.add_argument("--loop", type=parse_interval, metavar="ИНТЕРВАЛ",
                    help="Постить циклически каждые N (например 6h). Без флага — один пост и выход.")
    args = ap.parse_args()

    if args.loop:
        log.info("Режим цикла: каждые %d сек.", args.loop)
        while True:
            try:
                run_once()
            except Exception:
                log.exception("Ошибка в итерации, продолжаю после паузы")
            time.sleep(args.loop)
    else:
        run_once()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
