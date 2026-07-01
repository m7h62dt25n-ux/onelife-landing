"""
Бот-редактор канала про курортную недвижимость.

Сценарий:
  1. Каждый день в 11:00 бот присылает тебе варианты тем (кнопками).
  2. Ты выбираешь тему кнопкой.
  3. Бот пишет пост через Claude и показывает его тебе (в личке с ботом).
  4. Ты даёшь правки текстом — бот переписывает (сколько угодно раз).
  5. Жмёшь «✅ Опубликовать» — пост уходит в канал.

Команды:
  /start  — показать твой chat_id и помощь
  /themes — прислать темы прямо сейчас (не дожидаясь 11:00)

Нужны переменные окружения (см. .env.example):
  ANTHROPIC_API_KEY  — ключ Claude API
  BOT_TOKEN          — токен бота из @BotFather
  CHANNEL_ID         — @username канала или числовой id (-100…)
  EDITOR_CHAT_ID     — твой chat_id (кому слать темы в 11:00 и кто может публиковать)
  POST_HOUR          — час ежедневной рассылки тем (по умолчанию 11)
  TZ                 — таймзона (по умолчанию Europe/Moscow)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel, Field


def _load_dotenv() -> None:
    """Грузит .env из папки скрипта в os.environ (не перетирая уже заданные).
    Нужно, чтобы бот работал под launchd, где bash `source .env` упирается в TCC."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# ---------- Конфигурация ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
EDITOR_CHAT_ID = int(os.environ["EDITOR_CHAT_ID"]) if os.environ.get("EDITOR_CHAT_ID") else None
MODEL = os.environ.get("MODEL", "claude-opus-4-8")
POST_HOUR = int(os.environ.get("POST_HOUR", "11"))
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))

HISTORY_PATH = Path(__file__).with_name("history.json")
HISTORY_KEEP = 40
THEME_COUNT = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("editor-bot")
router = Router()

CHANNEL_BRIEF = (
    "Канал про курортную недвижимость Краснодарского края: Сочи, Большой Сочи "
    "(Лазаревское, Дагомыс, Адлер, Имеретинка), Анапа, Геленджик, Кубань. "
    "Аудитория — частные инвесторы и покупатели жилья у моря: квартиры, апартаменты, "
    "дома, новостройки, доходная недвижимость под аренду."
)

SYSTEM_PROMPT = (
    "Ты — опытный SMM-редактор и эксперт по курортной недвижимости. "
    "Пишешь живые, полезные посты для Telegram-канала, без воды и кликбейта. "
    f"{CHANNEL_BRIEF}\n\n"
    "Правила:\n"
    "- Конкретика и польза: цифры, примеры, практические выводы.\n"
    "- Тон — экспертный, дружелюбный, на «вы».\n"
    "- Никакой markdown-разметки (* _ #) внутри тела — только обычный текст и переносы строк.\n"
    "- Не выдумывай несуществующие ЖК и точные цены как факты; числа — с оговоркой "
    "(«ориентировочно», «в среднем»).\n"
    "- Без обещаний гарантированной доходности и юридических гарантий."
)


# ---------- Структуры ----------
class Theme(BaseModel):
    title: str = Field(description="Короткое название темы поста, до 70 символов")
    hook: str = Field(description="Одна строка: чем тема интересна аудитории")


class Themes(BaseModel):
    themes: list[Theme]


class Post(BaseModel):
    title: str = Field(description="Цепляющий заголовок поста, до 80 символов")
    body: str = Field(description="Тело поста: 600–900 символов, обычный текст с переносами строк, 1–3 уместных эмодзи, без markdown и html")
    hashtags: list[str] = Field(description="3–5 релевантных хэштегов на русском, каждый начинается с #")


# Состояние в памяти на каждого редактора: текущие темы и черновик.
SESSIONS: dict[int, dict] = {}


class EditState(StatesGroup):
    waiting_edits = State()


# ---------- Генерация (Claude) ----------
_client = anthropic.Anthropic()  # ключ из ANTHROPIC_API_KEY


def _gen_themes(recent_titles: list[str]) -> list[Theme]:
    avoid = "\n".join(f"- {t}" for t in recent_titles) or "(пока пусто)"
    user = (
        f"Предложи {THEME_COUNT} разных тем для постов канала на сегодня. "
        "Темы должны быть разнообразными по формату (инвестразбор, гайд по району, "
        "юридический момент, сезонность, сравнение форматов, кейс с цифрами и т.п.).\n\n"
        f"НЕ повторяй по смыслу недавние посты:\n{avoid}\n\n"
        "Выдай строго по схеме (список themes из title и hook)."
    )
    r = _client.messages.parse(
        model=MODEL, max_tokens=1500,
        thinking={"type": "adaptive"}, output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_format=Themes,
    )
    return r.parsed_output.themes


def _gen_post(theme: Theme, recent_titles: list[str]) -> Post:
    avoid = "\n".join(f"- {t}" for t in recent_titles) or "(пока пусто)"
    user = (
        f"Напиши пост на тему: «{theme.title}» ({theme.hook}).\n\n"
        f"Не повторяй формулировки недавних постов:\n{avoid}\n\n"
        "Выдай строго по схеме (title, body, hashtags)."
    )
    r = _client.messages.parse(
        model=MODEL, max_tokens=2000,
        thinking={"type": "adaptive"}, output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_format=Post,
    )
    return r.parsed_output


def _revise_post(prev: Post, instruction: str) -> Post:
    user = (
        "Вот текущий черновик поста:\n"
        f"Заголовок: {prev.title}\n"
        f"Тело: {prev.body}\n"
        f"Хэштеги: {' '.join(prev.hashtags)}\n\n"
        f"Внеси правки по замечанию редактора: «{instruction}». "
        "Сохрани то, что не просили менять. Выдай новый вариант строго по схеме (title, body, hashtags)."
    )
    r = _client.messages.parse(
        model=MODEL, max_tokens=2000,
        thinking={"type": "adaptive"}, output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_format=Post,
    )
    return r.parsed_output


# ---------- Рендер и история ----------
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_post(post: Post) -> str:
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in post.hashtags)
    parts = [f"<b>{esc(post.title.strip())}</b>", "", esc(post.body.strip())]
    if tags:
        parts += ["", esc(tags)]
    return "\n".join(parts)[:4000]


def load_history() -> list[dict]:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_PATH.write_text(
        json.dumps(history[-HISTORY_KEEP:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def recent_titles() -> list[str]:
    return [h["title"] for h in load_history()[-HISTORY_KEEP:]]


# ---------- Клавиатуры ----------
def kb_themes(themes: list[Theme]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{i+1}. {t.title[:55]}", callback_data=f"theme:{i}")]
            for i, t in enumerate(themes)]
    rows.append([InlineKeyboardButton(text="🔄 Другие темы", callback_data="rethemes")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_draft() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="pub")],
        [InlineKeyboardButton(text="✏️ Внести правки", callback_data="edit"),
         InlineKeyboardButton(text="🔄 Другой вариант", callback_data="regen")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])


# ---------- Хэндлеры ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Я бот-редактор канала про курортную недвижимость.\n\n"
        f"Твой chat_id: <code>{message.chat.id}</code>\n"
        "Пропиши его в <code>EDITOR_CHAT_ID</code> в .env — и каждый день в "
        f"{POST_HOUR}:00 я буду присылать темы.\n\n"
        "Команда /themes — прислать темы прямо сейчас.",
        parse_mode="HTML",
    )


@router.message(Command("themes"))
async def cmd_themes(message: Message):
    await send_themes(message.bot, message.chat.id)


async def send_themes(bot: Bot, chat_id: int):
    msg = await bot.send_message(chat_id, "🧠 Подбираю темы…")
    try:
        themes = await asyncio.to_thread(_gen_themes, recent_titles())
    except Exception:
        log.exception("themes generation failed")
        await msg.edit_text("😔 Не получилось сгенерировать темы. Попробуйте /themes ещё раз.")
        return
    SESSIONS.setdefault(chat_id, {})["themes"] = themes
    text = "📋 <b>Темы на сегодня</b>\nВыберите тему кнопкой:\n\n" + "\n".join(
        f"<b>{i+1}.</b> {esc(t.title)}\n<i>{esc(t.hook)}</i>" for i, t in enumerate(themes)
    )
    await msg.edit_text(text, reply_markup=kb_themes(themes), parse_mode="HTML")


@router.callback_query(F.data == "rethemes")
async def cb_rethemes(call: CallbackQuery):
    await call.answer("Подбираю новые…")
    await send_themes(call.bot, call.message.chat.id)


@router.callback_query(F.data.startswith("theme:"))
async def cb_theme(call: CallbackQuery):
    idx = int(call.data.split(":")[1])
    sess = SESSIONS.get(call.message.chat.id, {})
    themes = sess.get("themes")
    if not themes or idx >= len(themes):
        await call.answer("Темы устарели, нажмите /themes", show_alert=True)
        return
    theme = themes[idx]
    sess["theme"] = theme
    await call.answer()
    await call.message.answer("✍️ Пишу пост…")
    await produce_and_show(call.bot, call.message.chat.id, theme)


async def produce_and_show(bot: Bot, chat_id: int, theme: Theme):
    try:
        post = await asyncio.to_thread(_gen_post, theme, recent_titles())
    except Exception:
        log.exception("post generation failed")
        await bot.send_message(chat_id, "😔 Не получилось написать пост. Выберите тему ещё раз: /themes")
        return
    SESSIONS.setdefault(chat_id, {})["draft"] = post
    await bot.send_message(
        chat_id,
        "📝 <b>Черновик:</b>\n\n" + render_post(post),
        reply_markup=kb_draft(), parse_mode="HTML",
    )


@router.callback_query(F.data == "regen")
async def cb_regen(call: CallbackQuery):
    sess = SESSIONS.get(call.message.chat.id, {})
    theme = sess.get("theme")
    if not theme:
        await call.answer("Сначала выберите тему: /themes", show_alert=True)
        return
    await call.answer("Готовлю другой вариант…")
    await produce_and_show(call.bot, call.message.chat.id, theme)


@router.callback_query(F.data == "edit")
async def cb_edit(call: CallbackQuery, state: FSMContext):
    if not SESSIONS.get(call.message.chat.id, {}).get("draft"):
        await call.answer("Нет черновика. /themes", show_alert=True)
        return
    await call.answer()
    await state.set_state(EditState.waiting_edits)
    await call.message.answer("✏️ Напишите, что поправить (например: «короче и добавь про ипотеку»).")


@router.message(EditState.waiting_edits)
async def on_edits(message: Message, state: FSMContext):
    sess = SESSIONS.get(message.chat.id, {})
    draft = sess.get("draft")
    if not draft:
        await state.clear()
        await message.answer("Черновик потерян. Начните заново: /themes")
        return
    await message.answer("🔧 Вношу правки…")
    try:
        post = await asyncio.to_thread(_revise_post, draft, message.text or "")
    except Exception:
        log.exception("revise failed")
        await message.answer("😔 Не получилось переписать. Попробуйте ещё раз сформулировать правку.")
        return
    await state.clear()
    sess["draft"] = post
    await message.answer(
        "📝 <b>Обновлённый черновик:</b>\n\n" + render_post(post),
        reply_markup=kb_draft(), parse_mode="HTML",
    )


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    SESSIONS.get(call.message.chat.id, {}).pop("draft", None)
    await call.answer("Отменено")
    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "pub")
async def cb_publish(call: CallbackQuery):
    if EDITOR_CHAT_ID and call.from_user.id != EDITOR_CHAT_ID:
        await call.answer("Публиковать может только редактор канала.", show_alert=True)
        return
    sess = SESSIONS.get(call.message.chat.id, {})
    draft: Post | None = sess.get("draft")
    if not draft:
        await call.answer("Нет черновика для публикации.", show_alert=True)
        return
    await call.answer("Публикую…")
    try:
        sent = await call.bot.send_message(
            CHANNEL_ID, render_post(draft), parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception as e:
        log.exception("publish failed")
        await call.message.answer(f"😔 Не удалось опубликовать: {e}")
        return

    history = load_history()
    history.append({
        "title": draft.title.strip(),
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    save_history(history)
    sess.pop("draft", None)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"✅ Опубликовано в {CHANNEL_ID} (id {sent.message_id}).")


# ---------- Запуск ----------
async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=TZ)
    if EDITOR_CHAT_ID:
        scheduler.add_job(send_themes, "cron", hour=POST_HOUR, minute=0,
                          args=[bot, EDITOR_CHAT_ID])
        scheduler.start()
        log.info("Ежедневная рассылка тем в %02d:00 (%s) → chat %s", POST_HOUR, TZ, EDITOR_CHAT_ID)
    else:
        log.warning("EDITOR_CHAT_ID не задан — авторассылка тем выключена, работают только команды.")

    log.info("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
