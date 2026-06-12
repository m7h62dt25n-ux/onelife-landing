"""
Клёвое Место PRO — Telegram-бот продаж.

Принимает оплату подписки (Telegram Payments или демо-режим), выдаёт логин/пароль
и записывает пользователя в users.json в GitHub-репозитории — оттуда веб-приложение
проверяет доступ при входе.

Запуск:  python bot.py   (нужны переменные окружения, см. .env.example / README.md)
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ---------- Конфигурация ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]                      # токен из @BotFather
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]                # PAT с правом repo
GITHUB_REPO = os.environ.get("GITHUB_REPO", "m7h62dt25n-ux/onelife-landing")
USERS_PATH = os.environ.get("USERS_PATH", "users.json")
APP_URL = os.environ.get("APP_URL", "https://m7h62dt25n-ux.github.io/onelife-landing/app.html")
# Токен платёжного провайдера из BotFather (ЮKassa и т.п.). Пусто = демо-оплата.
PROVIDER_TOKEN = os.environ.get("PAYMENT_PROVIDER_TOKEN", "")

AUTH_SALT = "km2026"  # должен совпадать с AUTH_SALT в app.html

PLANS = {
    "month": {"title": "Месяц", "price": 449_00, "days": 30},
    "year": {"title": "Год", "price": 3990_00, "days": 365},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("klevoe-bot")
router = Router()


# ---------- База пользователей: users.json в GitHub ----------
API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{USERS_PATH}"
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


async def db_read(session: aiohttp.ClientSession) -> tuple[dict, str]:
    """Возвращает (данные, sha файла)."""
    async with session.get(API_BASE, headers=HEADERS) as r:
        r.raise_for_status()
        payload = await r.json()
    data = json.loads(base64.b64decode(payload["content"]).decode())
    return data, payload["sha"]


async def db_write(session: aiohttp.ClientSession, data: dict, sha: str, message: str) -> None:
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode()
        ).decode(),
        "sha": sha,
    }
    async with session.put(API_BASE, headers=HEADERS, json=body) as r:
        r.raise_for_status()


def password_hash(login: str, password: str) -> str:
    return hashlib.sha256(f"{login}:{password}:{AUTH_SALT}".encode()).hexdigest()


async def upsert_user(login: str, password: str, days: int, tg_id: int) -> str:
    """Создаёт или продлевает пользователя. Возвращает дату окончания (строкой)."""
    async with aiohttp.ClientSession() as session:
        data, sha = await db_read(session)
        users = data.setdefault("users", [])
        now = datetime.now(timezone.utc)
        existing = next((u for u in users if u["login"].lower() == login.lower()), None)
        if existing:
            base = max(now, datetime.fromisoformat(existing["until"]).replace(tzinfo=timezone.utc))
            until = (base + timedelta(days=days)).date().isoformat()
            existing.update(hash=password_hash(login, password), until=until, tg_id=tg_id)
        else:
            until = (now + timedelta(days=days)).date().isoformat()
            users.append({
                "login": login,
                "hash": password_hash(login, password),
                "until": until,
                "tg_id": tg_id,
                "created": now.date().isoformat(),
            })
        await db_write(session, data, sha, f"bot: access for {login} until {until}")
        return until


async def login_taken(login: str) -> bool:
    async with aiohttp.ClientSession() as session:
        data, _ = await db_read(session)
    return any(u["login"].lower() == login.lower() for u in data.get("users", []))


async def find_by_tg(tg_id: int) -> dict | None:
    async with aiohttp.ClientSession() as session:
        data, _ = await db_read(session)
    return next((u for u in data.get("users", []) if u.get("tg_id") == tg_id), None)


def gen_login() -> str:
    return "fisher_" + "".join(secrets.choice(string.digits) for _ in range(4))


def gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


# ---------- FSM для собственного логина/пароля ----------
class SetCreds(StatesGroup):
    login = State()
    password = State()


# ---------- Клавиатуры ----------
def kb_plans() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎣 Месяц — 449 ₽", callback_data="buy:month")],
        [InlineKeyboardButton(text="🏆 Год — 3 990 ₽ (-26%)", callback_data="buy:year")],
    ])


def kb_creds() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Сгенерировать логин и пароль", callback_data="creds:gen")],
        [InlineKeyboardButton(text="✍️ Задать свои", callback_data="creds:custom")],
    ])


def kb_app() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть приложение", url=APP_URL)],
    ])


# ---------- Хэндлеры ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎣 <b>Клёвое Место PRO</b>\n\n"
        "Карта точек клёва всего Краснодарского края: Чёрное и Азовское море, "
        "Кубань, лиманы, водохранилища.\n\n"
        "• 37 проверенных точек с GPS\n"
        "• 18 видов рыбы: хищник, мирная, морская\n"
        "• Прогноз клёва на 7 дней\n"
        "• Календарь клёва на год\n\n"
        "Выберите подписку — после оплаты пришлю логин и пароль для входа:",
        reply_markup=kb_plans(),
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    user = await find_by_tg(message.from_user.id)
    if user:
        await message.answer(
            f"✅ Ваша подписка: логин <code>{user['login']}</code>, "
            f"действует до <b>{user['until']}</b>.",
            reply_markup=kb_app(),
        )
    else:
        await message.answer("У вас пока нет подписки. Оформить:", reply_markup=kb_plans())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.split(":")[1]
    plan = PLANS[plan_key]
    await state.update_data(plan=plan_key)
    await call.answer()

    if PROVIDER_TOKEN:
        # Реальная оплата через Telegram Payments (ЮKassa и др.)
        await call.message.answer_invoice(
            title=f"Клёвое Место PRO — {plan['title']}",
            description="Доступ к карте точек клёва Краснодарского края",
            payload=f"sub:{plan_key}",
            provider_token=PROVIDER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label=plan["title"], amount=plan["price"])],
        )
    else:
        # Демо-режим без провайдера
        await call.message.answer(
            f"💳 Тариф «{plan['title']}» — {plan['price'] // 100} ₽.\n\n"
            "<i>Демо-режим: платёжный провайдер не подключён, оплата имитируется.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Оплатить (демо)", callback_data="paid_demo")],
            ]),
        )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_payment(message: Message, state: FSMContext):
    plan_key = message.successful_payment.invoice_payload.split(":")[1]
    await state.update_data(plan=plan_key)
    await after_payment(message, state)


@router.callback_query(F.data == "paid_demo")
async def cb_paid_demo(call: CallbackQuery, state: FSMContext):
    await call.answer("Оплата прошла!")
    await after_payment(call.message, state)


async def after_payment(message: Message, state: FSMContext):
    await message.answer(
        "✅ <b>Оплата получена!</b>\n\n"
        "Как выдать доступ к приложению?",
        reply_markup=kb_creds(),
    )


@router.callback_query(F.data == "creds:gen")
async def cb_creds_gen(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    plan = PLANS[data.get("plan", "month")]
    login = gen_login()
    while await login_taken(login):
        login = gen_login()
    password = gen_password()
    await issue(call.message, state, login, password, plan["days"], call.from_user.id)


@router.callback_query(F.data == "creds:custom")
async def cb_creds_custom(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SetCreds.login)
    await call.message.answer(
        "Введите желаемый <b>логин</b> (латиница, цифры, от 3 символов):"
    )


@router.message(SetCreds.login)
async def fsm_login(message: Message, state: FSMContext):
    login = (message.text or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,20}", login):
        await message.answer("Логин — латиница, цифры и _, от 3 до 20 символов. Попробуйте ещё раз:")
        return
    if await login_taken(login):
        await message.answer("Этот логин уже занят. Введите другой:")
        return
    await state.update_data(login=login)
    await state.set_state(SetCreds.password)
    await message.answer("Теперь введите <b>пароль</b> (минимум 6 символов):")


@router.message(SetCreds.password)
async def fsm_password(message: Message, state: FSMContext):
    password = (message.text or "").strip()
    if len(password) < 6:
        await message.answer("Слишком короткий. Минимум 6 символов, попробуйте ещё раз:")
        return
    data = await state.get_data()
    plan = PLANS[data.get("plan", "month")]
    await issue(message, state, data["login"], password, plan["days"], message.from_user.id)


async def issue(message: Message, state: FSMContext, login: str, password: str, days: int, tg_id: int):
    await message.answer("⏳ Создаю доступ…")
    try:
        until = await upsert_user(login, password, days, tg_id)
    except Exception:
        log.exception("db write failed")
        await message.answer(
            "😔 Не получилось записать доступ — попробуйте ещё раз через минуту "
            "или напишите в поддержку."
        )
        return
    await state.clear()
    await message.answer(
        "🎉 <b>Доступ готов!</b>\n\n"
        f"Логин: <code>{login}</code>\n"
        f"Пароль: <code>{password}</code>\n"
        f"Подписка до: <b>{until}</b>\n\n"
        "⚠️ Сохраните эти данные. Сайт обновит базу в течение 1–2 минут — "
        "если вход не сработал сразу, подождите минуту и попробуйте снова.",
        reply_markup=kb_app(),
    )


# ---------- Запуск ----------
async def main():
    bot = Bot(BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot started (provider=%s)", "real" if PROVIDER_TOKEN else "demo")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
