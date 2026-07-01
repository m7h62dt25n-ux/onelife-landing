"""
Зеркало Telegram-канала в канал MAX через ЛИЧНЫЙ аккаунт (без бота MAX).

Вариант для случая, когда бота в MAX создать нельзя (нет ИП/самозанятости):
вместо MAX Bot API постит от имени твоего обычного аккаунта MAX через
неофициальную библиотеку PyMax (pip: maxapi-python). Вход — по номеру
телефона и SMS-коду, сессия сохраняется в max_session/session.db,
код нужен только при первом запуске.

⚠️ Неофициальное API: формально может нарушать условия сервиса MAX —
теоретический риск блокировки аккаунта. Публикация собственного контента
в собственный канал — минимально рискованный сценарий, но знать об этом стоит.

Telegram-сторона та же, что у tg_to_max.py (и код переиспользуется оттуда):
отдельный TG-бот-наблюдатель, getUpdates, альбомы, форматирование.

Запуск:            python tg_to_max_user.py          (первый раз — из терминала, спросит SMS-код)
Список чатов MAX:  python tg_to_max_user.py --max-chats
Тест отправки:     python tg_to_max_user.py --test

Нужны переменные окружения (см. .env.example):
  MIRROR_BOT_TOKEN — токен ВТОРОГО TG-бота (@BotFather), админ исходного канала
  CHANNEL_ID       — исходный TG-канал: @username или -100…
  MAX_PHONE        — номер телефона аккаунта MAX (+7…)
  MAX_CHAT_ID      — числовой id канала MAX (аккаунт — его владелец/админ)
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from pymax import Client, File, Photo, Video

# Телеграм-сторона и утилиты — из соседнего tg_to_max.py (он же грузит .env).
from tg_to_max import GROUP_FLUSH_SEC, MAP_KEEP, is_source_channel, tg_call, tg_download

MAX_PHONE = os.environ.get("MAX_PHONE", "")
MAX_CHAT_ID = os.environ.get("MAX_CHAT_ID", "")

STATE_PATH = Path(__file__).with_name("tg_to_max_user_state.json")
SESSION_DIR = Path(__file__).with_name("max_session")
TEXT_LIMIT = 4000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-to-max-user")


# ---------- Состояние ----------
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"offset": 0, "map": {}}


def save_state(state: dict) -> None:
    if len(state["map"]) > MAP_KEEP:
        keep = sorted(state["map"], key=int)[-MAP_KEEP:]
        state["map"] = {k: state["map"][k] for k in keep}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Конвертация форматирования (TG entities -> markdown PyMax) ----------
_MD_MARK = {"bold": "**", "italic": "_", "underline": "__",
            "strikethrough": "~~", "code": "`", "pre": "```"}


def entities_to_markdown(text: str, entities: list[dict] | None) -> str:
    """Смещения entities у Telegram — в UTF-16 code units, поэтому режем UTF-16-строку."""
    if not text:
        return ""
    if not entities:
        return text
    b = text.encode("utf-16-le")
    total = len(b) // 2

    def seg(start: int, end: int) -> str:
        return b[start * 2:end * 2].decode("utf-16-le")

    out, pos = [], 0
    for e in sorted(entities, key=lambda e: (e["offset"], -e["length"])):
        if e["offset"] < pos:            # вложенные/пересекающиеся — берём внешнюю
            continue
        kind = e["type"]
        if kind not in _MD_MARK and kind != "text_link":
            continue
        start, end = e["offset"], e["offset"] + e["length"]
        out.append(seg(pos, start))
        inner = seg(start, end)
        if kind == "text_link":
            out.append(f"[{inner}]({e.get('url', '')})")
        elif "\n" in inner and kind != "pre":
            out.append(inner)            # маркеры **…** не работают через перенос строки
        else:
            mark = _MD_MARK[kind]
            out.append(f"{mark}{inner}{mark}")
        pos = end
    out.append(seg(pos, total))
    return "".join(out)


def message_markdown(msg: dict) -> str:
    if msg.get("text") is not None:
        return entities_to_markdown(msg["text"], msg.get("entities"))
    return entities_to_markdown(msg.get("caption", ""), msg.get("caption_entities"))


# ---------- Вложения ----------
async def build_attachments(msgs: list[dict]) -> list:
    atts = []
    for m in msgs:
        try:
            if m.get("photo"):
                largest = m["photo"][-1]           # варианты идут от меньшего к большему
                content, name = await asyncio.to_thread(tg_download, largest["file_id"])
                atts.append(Photo(raw=content, name=name or "photo.jpg"))
            elif m.get("video") or m.get("animation"):
                media = m.get("video") or m.get("animation")
                content, name = await asyncio.to_thread(tg_download, media["file_id"])
                atts.append(Video(raw=content, name=name or "video.mp4"))
            elif m.get("document"):
                content, name = await asyncio.to_thread(tg_download, m["document"]["file_id"])
                atts.append(File(raw=content, name=m["document"].get("file_name") or name))
        except Exception:
            log.exception("Не удалось перенести вложение из поста %s — шлю без него",
                          m.get("message_id"))
    return atts


# ---------- Зеркалирование ----------
async def mirror(client: Client, msgs: list[dict], state: dict) -> None:
    text = next((message_markdown(m) for m in msgs if message_markdown(m)), "")
    attachments = await build_attachments(msgs)
    if not text and not attachments:
        log.info("Пост %s: нет ни текста, ни поддерживаемых вложений — пропускаю",
                 msgs[0].get("message_id"))
        return
    sent = await client.send_message(
        chat_id=int(MAX_CHAT_ID), text=text[:TEXT_LIMIT],
        attachments=attachments or None, notify=True,
    )
    if sent is None:
        raise RuntimeError("MAX не подтвердил отправку сообщения")
    kind = "media" if attachments else "text"
    for m in msgs:
        state["map"][str(m["message_id"])] = {"mid": sent.id, "kind": kind}
    log.info("Пост %s -> MAX %s (%s, вложений: %d)",
             "+".join(str(m["message_id"]) for m in msgs), sent.id, kind, len(attachments))


async def mirror_edit(client: Client, msg: dict, state: dict) -> None:
    entry = state["map"].get(str(msg["message_id"]))
    if not entry:
        log.info("Правка поста %s: соответствие в MAX не найдено, пропускаю",
                 msg["message_id"])
        return
    if entry["kind"] != "text":
        log.info("Правка поста %s: пост с медиа, правки не зеркалю", msg["message_id"])
        return
    try:
        await client.edit_message(chat_id=int(MAX_CHAT_ID), message_id=entry["mid"],
                                  text=message_markdown(msg)[:TEXT_LIMIT])
        log.info("Правка поста %s -> MAX %s", msg["message_id"], entry["mid"])
    except Exception:
        log.exception("Не удалось отредактировать сообщение в MAX")


# ---------- Запуск клиента MAX ----------
async def start_client() -> tuple[Client, asyncio.Task]:
    SESSION_DIR.mkdir(exist_ok=True)
    client = Client(phone=MAX_PHONE, work_dir=str(SESSION_DIR))
    started = asyncio.Event()

    @client.on_start()
    async def _on_start(_client: Client) -> None:
        started.set()

    runner = asyncio.create_task(client.start(), name="pymax-runner")
    started_waiter = asyncio.create_task(started.wait())
    done, _ = await asyncio.wait({runner, started_waiter},
                                 return_when=asyncio.FIRST_COMPLETED,
                                 timeout=600)
    if runner in done:                    # клиент завершился, не стартовав — ошибка входа
        started_waiter.cancel()
        exc = runner.exception()
        raise exc if exc else RuntimeError("Клиент MAX завершился до авторизации")
    if not done:
        runner.cancel()
        raise RuntimeError("Не дождался авторизации в MAX за 10 минут")
    me = client.me
    log.info("Вошёл в MAX как %s (id %s)",
             getattr(me, "names", None) or getattr(me, "phone", "?"),
             getattr(me, "id", "?"))
    return client, runner


async def run() -> None:
    for var in ("MAX_PHONE", "MAX_CHAT_ID"):
        if not os.environ.get(var):
            sys.exit(f"Не задана переменная {var} (см. .env.example)")
    if not os.environ.get("MIRROR_BOT_TOKEN") or not os.environ.get("CHANNEL_ID"):
        sys.exit("Не заданы MIRROR_BOT_TOKEN / CHANNEL_ID (см. .env.example)")

    me_tg = await asyncio.to_thread(tg_call, "getMe")
    client, runner = await start_client()
    log.info("Мост запущен: TG @%s -> MAX chat %s, источник %s",
             me_tg.get("username"), MAX_CHAT_ID, os.environ["CHANNEL_ID"])

    state = load_state()
    pending: dict[str, dict] = {}   # media_group_id -> {"items": [...], "last": ts}

    while True:
        if runner.done():           # соединение с MAX умерло окончательно
            exc = runner.exception()
            raise exc if exc else RuntimeError("Клиент MAX остановился")
        try:
            timeout = 1 if pending else 25
            updates = await asyncio.to_thread(
                tg_call, "getUpdates",
                offset=state["offset"], timeout=timeout,
                allowed_updates=["channel_post", "edited_channel_post"],
            )
            for u in updates:
                state["offset"] = u["update_id"] + 1
                post, edited = u.get("channel_post"), u.get("edited_channel_post")
                msg = post or edited
                if not msg or not is_source_channel(msg["chat"]):
                    continue
                if edited:
                    await mirror_edit(client, edited, state)
                elif post.get("media_group_id"):
                    g = pending.setdefault(post["media_group_id"],
                                           {"items": [], "last": 0.0})
                    g["items"].append(post)
                    g["last"] = time.monotonic()
                else:
                    await mirror(client, [post], state)

            # Альбом отправляем, когда новые его части перестали приходить.
            now = time.monotonic()
            for gid in [g for g, v in pending.items()
                        if now - v["last"] > GROUP_FLUSH_SEC]:
                await mirror(client, pending.pop(gid)["items"], state)

            save_state(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в цикле, продолжаю через 5 сек")
            await asyncio.sleep(5)


async def cmd_max_chats() -> None:
    """Показывает чаты/каналы аккаунта MAX — чтобы узнать MAX_CHAT_ID."""
    if not MAX_PHONE:
        sys.exit("Не задана переменная MAX_PHONE")
    client, runner = await start_client()
    chats = client.chats or []
    if not chats:
        print("MAX не вернул ни одного чата (канал точно создан?)")
    for c in chats:
        print(f"{c.id}\t{c.type}\t{getattr(c, 'title', '') or ''}")
    print("\nЧисловой id канала из первой колонки впиши в MAX_CHAT_ID в .env")
    await _shutdown(client, runner)


async def cmd_test() -> None:
    if not MAX_PHONE or not MAX_CHAT_ID:
        sys.exit("Нужны MAX_PHONE и MAX_CHAT_ID")
    client, runner = await start_client()
    sent = await client.send_message(
        chat_id=int(MAX_CHAT_ID),
        text="**Проверка моста TG → MAX**\n\nЕсли видишь это сообщение — "
             "аккаунт умеет публиковать в канал. ✅",
    )
    print(f"Отправлено, id={getattr(sent, 'id', sent)}")
    await _shutdown(client, runner)


async def _shutdown(client: Client, runner: asyncio.Task) -> None:
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await runner
    with contextlib.suppress(Exception):
        await client.stop()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Зеркало Telegram-канала в канал MAX через личный аккаунт")
    ap.add_argument("--max-chats", action="store_true",
                    help="показать чаты аккаунта MAX (узнать MAX_CHAT_ID) и выйти")
    ap.add_argument("--test", action="store_true",
                    help="отправить тестовое сообщение в канал MAX и выйти")
    args = ap.parse_args()
    if args.max_chats:
        asyncio.run(cmd_max_chats())
    elif args.test:
        asyncio.run(cmd_test())
    else:
        asyncio.run(run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
