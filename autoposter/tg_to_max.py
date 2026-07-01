"""
Зеркало Telegram-канала в канал мессенджера MAX.

Как работает:
  1. Отдельный TG-бот (не тот, что editor_bot!) состоит админом в исходном канале
     и через long polling (getUpdates) получает все новые посты канала.
  2. Каждый пост конвертируется (текст + форматирование + фото/видео/файлы)
     и публикуется в канал MAX через MAX Bot API (platform-api2.max.ru).
  3. Правки текстовых постов в TG подтягиваются в MAX (PUT /messages).
  4. Смещение getUpdates и соответствие id сообщений хранятся в tg_to_max_state.json.

Зачем второй TG-бот: editor_bot уже держит getUpdates на основном токене
(два процесса на одном токене конфликтуют), и Telegram не присылает боту
его собственные посты — а второй бот видит всё: и ручные, и от editor_bot.

Запуск:            python tg_to_max.py
Список чатов MAX:  python tg_to_max.py --max-chats   (узнать MAX_CHAT_ID)
Тест отправки:     python tg_to_max.py --test

Нужны переменные окружения (см. .env.example):
  MIRROR_BOT_TOKEN — токен ВТОРОГО TG-бота (@BotFather), админ исходного канала
  CHANNEL_ID       — исходный TG-канал: @username или -100…
  MAX_BOT_TOKEN    — токен бота MAX (создаётся у @masterbot в приложении MAX)
  MAX_CHAT_ID      — числовой id канала MAX (бот должен быть его админом)
  MAX_API_BASE     — необязательно, по умолчанию https://platform-api2.max.ru
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests


def _load_dotenv() -> None:
    """Грузит .env из папки скрипта (не перетирая уже заданные) — как в editor_bot."""
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
MIRROR_BOT_TOKEN = os.environ.get("MIRROR_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
MAX_CHAT_ID = os.environ.get("MAX_CHAT_ID", "")
MAX_API_BASE = os.environ.get("MAX_API_BASE", "https://platform-api2.max.ru").rstrip("/")

STATE_PATH = Path(__file__).with_name("tg_to_max_state.json")
MAP_KEEP = 500          # сколько соответствий tg_id -> max_mid помнить (для правок)
GROUP_FLUSH_SEC = 2.5   # сколько ждать «хвост» альбома перед отправкой
TEXT_LIMIT = 4000       # лимит текста сообщения в MAX

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-to-max")


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


# ---------- Telegram ----------
def tg_call(method: str, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{MIRROR_BOT_TOKEN}/{method}",
        json=params, timeout=90,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} error: {data}")
    return data["result"]


def tg_download(file_id: str) -> tuple[bytes, str]:
    """Скачивает файл из Telegram, возвращает (bytes, имя файла)."""
    info = tg_call("getFile", file_id=file_id)
    path = info["file_path"]
    r = requests.get(
        f"https://api.telegram.org/file/bot{MIRROR_BOT_TOKEN}/{path}", timeout=300,
    )
    r.raise_for_status()
    return r.content, Path(path).name


def is_source_channel(chat: dict) -> bool:
    if CHANNEL_ID.startswith("@"):
        return ("@" + (chat.get("username") or "")).lower() == CHANNEL_ID.lower()
    return str(chat.get("id")) == str(CHANNEL_ID)


# ---------- Конвертация форматирования (TG entities -> HTML для MAX) ----------
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ENTITY_TAG = {"bold": "b", "italic": "i", "underline": "u",
               "strikethrough": "s", "code": "code", "pre": "pre"}


def entities_to_html(text: str, entities: list[dict] | None) -> str:
    """Смещения entities у Telegram — в UTF-16 code units, поэтому режем UTF-16-строку."""
    if not text:
        return ""
    if not entities:
        return esc(text)
    b = text.encode("utf-16-le")
    total = len(b) // 2

    def seg(start: int, end: int) -> str:
        return b[start * 2:end * 2].decode("utf-16-le")

    out, pos = [], 0
    for e in sorted(entities, key=lambda e: (e["offset"], -e["length"])):
        if e["offset"] < pos:            # вложенные/пересекающиеся — берём внешнюю
            continue
        kind = e["type"]
        if kind not in _ENTITY_TAG and kind != "text_link":
            continue
        start, end = e["offset"], e["offset"] + e["length"]
        out.append(esc(seg(pos, start)))
        inner = esc(seg(start, end))
        if kind == "text_link":
            out.append(f'<a href="{esc(e.get("url", ""))}">{inner}</a>')
        else:
            tag = _ENTITY_TAG[kind]
            out.append(f"<{tag}>{inner}</{tag}>")
        pos = end
    out.append(esc(seg(pos, total)))
    return "".join(out)


def message_html(msg: dict) -> str:
    if msg.get("text") is not None:
        return entities_to_html(msg["text"], msg.get("entities"))
    return entities_to_html(msg.get("caption", ""), msg.get("caption_entities"))


# ---------- MAX Bot API ----------
class MaxApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"MAX API {status}: {body}")
        self.status = status
        self.body = body


def max_request(method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None):
    r = requests.request(
        method, MAX_API_BASE + path,
        params=params, json=json_body,
        headers={"Authorization": MAX_BOT_TOKEN},
        timeout=60,
    )
    if r.status_code != 200:
        raise MaxApiError(r.status_code, r.text)
    return r.json()


def max_upload(kind: str, content: bytes, filename: str) -> dict:
    """Загружает медиа в MAX. kind: image | video | file. Возвращает payload вложения."""
    up = max_request("POST", "/uploads", params={"type": kind})
    r = requests.post(up["url"], files={"data": (filename, content)}, timeout=300)
    r.raise_for_status()
    # Токен вложения: для видео он приходит сразу в ответе /uploads,
    # для фото/файлов — в ответе сервера загрузки (у фото — внутри photos{}).
    token = up.get("token")
    if not token:
        try:
            resp = r.json()
        except ValueError:
            resp = {}
        token = resp.get("token")
        if not token and isinstance(resp.get("photos"), dict) and resp["photos"]:
            token = next(iter(resp["photos"].values())).get("token")
    if not token:
        raise RuntimeError(f"MAX upload({kind}): не получил token, ответ: {r.text[:300]}")
    return {"type": kind, "payload": {"token": token}}


def max_send(text: str, attachments: list[dict] | None = None) -> str:
    """Шлёт сообщение в канал MAX, возвращает mid. Ждёт обработку вложений."""
    body: dict = {"text": text[:TEXT_LIMIT], "notify": True}
    if text:
        body["format"] = "html"
    if attachments:
        body["attachments"] = attachments
    for attempt in range(15):
        try:
            data = max_request(
                "POST", "/messages",
                params={"chat_id": int(MAX_CHAT_ID), "disable_link_preview": "true"},
                json_body=body,
            )
            return data["message"]["body"]["mid"]
        except MaxApiError as e:
            # Вложение ещё обрабатывается на стороне MAX — подождать и повторить.
            if e.status == 400 and "attachment.not.ready" in e.body:
                time.sleep(2)
                continue
            raise
    raise RuntimeError("MAX: вложение так и не обработалось за 30 секунд")


def max_edit(mid: str, text: str) -> None:
    max_request("PUT", "/messages", params={"message_id": mid},
                json_body={"text": text[:TEXT_LIMIT], "format": "html"})


# ---------- Зеркалирование ----------
def build_attachments(msgs: list[dict]) -> list[dict]:
    atts = []
    for m in msgs:
        try:
            if m.get("photo"):
                largest = m["photo"][-1]           # варианты идут от меньшего к большему
                content, name = tg_download(largest["file_id"])
                atts.append(max_upload("image", content, name or "photo.jpg"))
            elif m.get("video") or m.get("animation"):
                media = m.get("video") or m.get("animation")
                content, name = tg_download(media["file_id"])
                atts.append(max_upload("video", content, name or "video.mp4"))
            elif m.get("document"):
                content, name = tg_download(m["document"]["file_id"])
                atts.append(max_upload("file", content,
                                       m["document"].get("file_name") or name))
        except Exception:
            log.exception("Не удалось перенести вложение из поста %s — шлю без него",
                          m.get("message_id"))
    return atts


def mirror(msgs: list[dict], state: dict) -> None:
    """Зеркалит пост (или альбом из нескольких сообщений) в MAX."""
    text = next((message_html(m) for m in msgs if message_html(m)), "")
    attachments = build_attachments(msgs)
    if not text and not attachments:
        log.info("Пост %s: нет ни текста, ни поддерживаемых вложений — пропускаю",
                 msgs[0].get("message_id"))
        return
    mid = max_send(text, attachments)
    kind = "media" if attachments else "text"
    for m in msgs:
        state["map"][str(m["message_id"])] = {"mid": mid, "kind": kind}
    log.info("Пост %s -> MAX %s (%s, вложений: %d)",
             "+".join(str(m["message_id"]) for m in msgs), mid, kind, len(attachments))


def mirror_edit(msg: dict, state: dict) -> None:
    entry = state["map"].get(str(msg["message_id"]))
    if not entry:
        log.info("Правка поста %s: соответствие в MAX не найдено, пропускаю",
                 msg["message_id"])
        return
    if entry["kind"] != "text":
        log.info("Правка поста %s: пост с медиа, правки не зеркалю", msg["message_id"])
        return
    try:
        max_edit(entry["mid"], message_html(msg))
        log.info("Правка поста %s -> MAX %s", msg["message_id"], entry["mid"])
    except Exception:
        log.exception("Не удалось отредактировать сообщение в MAX")


def run() -> None:
    for var in ("MIRROR_BOT_TOKEN", "CHANNEL_ID", "MAX_BOT_TOKEN", "MAX_CHAT_ID"):
        if not globals()[var]:
            sys.exit(f"Не задана переменная {var} (см. .env.example)")

    me_tg = tg_call("getMe")
    me_max = max_request("GET", "/me")
    log.info("Мост запущен: TG @%s -> MAX «%s» (chat %s), источник %s",
             me_tg.get("username"), me_max.get("name"), MAX_CHAT_ID, CHANNEL_ID)

    state = load_state()
    pending: dict[str, dict] = {}   # media_group_id -> {"items": [...], "last": ts}

    while True:
        try:
            timeout = 1 if pending else 25
            updates = tg_call(
                "getUpdates",
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
                    mirror_edit(edited, state)
                elif post.get("media_group_id"):
                    g = pending.setdefault(post["media_group_id"],
                                           {"items": [], "last": 0.0})
                    g["items"].append(post)
                    g["last"] = time.monotonic()
                else:
                    mirror([post], state)

            # Альбом отправляем, когда новые его части перестали приходить.
            now = time.monotonic()
            for gid in [g for g, v in pending.items()
                        if now - v["last"] > GROUP_FLUSH_SEC]:
                mirror(pending.pop(gid)["items"], state)

            save_state(state)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("Ошибка в цикле, продолжаю через 5 сек")
            time.sleep(5)


def cmd_max_chats() -> None:
    """Показывает чаты/каналы, где состоит MAX-бот, — чтобы узнать MAX_CHAT_ID."""
    if not MAX_BOT_TOKEN:
        sys.exit("Не задана переменная MAX_BOT_TOKEN")
    data = max_request("GET", "/chats")
    chats = data.get("chats", [])
    if not chats:
        print("Бот пока не состоит ни в одном чате/канале MAX.\n"
              "Добавь бота админом в канал MAX и повтори.")
        return
    for c in chats:
        print(f"{c.get('chat_id')}\t{c.get('type')}\t{c.get('title')}")
    print("\nЧисловой id из первой колонки впиши в MAX_CHAT_ID в .env")


def cmd_test() -> None:
    if not MAX_BOT_TOKEN or not MAX_CHAT_ID:
        sys.exit("Нужны MAX_BOT_TOKEN и MAX_CHAT_ID")
    mid = max_send("<b>Проверка моста TG → MAX</b>\n\nЕсли видишь это сообщение — "
                   "бот умеет публиковать в канал. ✅")
    print(f"Отправлено, mid={mid}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Зеркало Telegram-канала в канал MAX")
    ap.add_argument("--max-chats", action="store_true",
                    help="показать чаты MAX-бота (узнать MAX_CHAT_ID) и выйти")
    ap.add_argument("--test", action="store_true",
                    help="отправить тестовое сообщение в канал MAX и выйти")
    args = ap.parse_args()
    if args.max_chats:
        cmd_max_chats()
    elif args.test:
        cmd_test()
    else:
        run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
