# Бот-редактор канала про курортную недвижимость

Интерактивный сценарий с твоим контролем над каждым постом:

1. Каждый день в **11:00** бот присылает тебе варианты тем (кнопками).
2. Ты выбираешь тему кнопкой.
3. Бот пишет пост через Claude и показывает его тебе.
4. Ты даёшь правки текстом — бот переписывает (сколько нужно раз).
5. Жмёшь **✅ Опубликовать** — пост уходит в канал.

Файл: [editor_bot.py](editor_bot.py).
Рядом лежат:
- [autopost.py](autopost.py) — альтернатива с полностью автономной публикацией без подтверждения (если когда-нибудь захочется);
- [tg_to_max.py](tg_to_max.py) — зеркало канала в мессенджер MAX (см. раздел ниже).

## 1. Подготовка

1. **Бот.** Создай бота в [@BotFather](https://t.me/BotFather), получи `BOT_TOKEN`.
   Добавь бота в канал и сделай **администратором** с правом «Публикация сообщений».
2. **Канал.** Публичный — `@username`. Приватный — числовой id `-1001234567890`.
3. **Ключ Claude.** `ANTHROPIC_API_KEY` из [console.anthropic.com](https://console.anthropic.com).

## 2. Установка

```bash
cd autoposter
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполни ANTHROPIC_API_KEY, BOT_TOKEN, CHANNEL_ID
```

## 3. Первый запуск

```bash
set -a; source .env; set +a
python editor_bot.py
```

Напиши боту `/start` — он покажет твой `chat_id`. Впиши его в `EDITOR_CHAT_ID`
в `.env` и перезапусти бот: теперь в 11:00 он сам будет присылать темы.
Команда `/themes` — прислать темы прямо сейчас, не дожидаясь 11:00.

## 4. Автозапуск на macOS (launchd) — уже настроено

Бот запускается через LaunchAgent `~/Library/LaunchAgents/com.resortcapital.editorbot.plist`:
стартует при входе в систему (`RunAtLoad`) и автоматически перезапускается, если упадёт
(`KeepAlive`). Лог — `~/Library/Logs/resortcapital-editorbot.log`.

```bash
UID=$(id -u)
# перезапустить (после правки кода или .env)
launchctl kickstart -k gui/$UID/com.resortcapital.editorbot
# остановить / запустить
launchctl bootout    gui/$UID/com.resortcapital.editorbot
launchctl bootstrap  gui/$UID ~/Library/LaunchAgents/com.resortcapital.editorbot.plist
# статус и лог
launchctl print gui/$UID/com.resortcapital.editorbot | grep -E "state =|pid ="
tail -f ~/Library/Logs/resortcapital-editorbot.log
```

> ⚠️ **Про папку Desktop и launchd.** Проект лежит в `~/Desktop` — это TCC-защищённая папка.
> Поэтому launchd запускает **Python напрямую** (`venv/bin/python editor_bot.py`, не через `run.sh`),
> а лог пишет **вне Desktop** (`~/Library/Logs/...`) — иначе job падает с `EX_CONFIG`.
> Бот сам читает `.env` (`_load_dotenv`), `source .env` под launchd не нужен. Чтобы убрать
> TCC-нюансы совсем — перенеси папку из `~/Desktop` в обычный каталог в `~/`.

Для Linux-сервера аналог — systemd-сервис с `ExecStart=…/venv/bin/python editor_bot.py`
и `EnvironmentFile=…/.env`.

## 5. Зеркало канала в MAX

Всё, что публикуется в Telegram-канале (ботом или руками), автоматически
дублируется в канал мессенджера MAX: текст с форматированием, фото, альбомы,
видео, файлы. Правки текстовых постов тоже подтягиваются.

Два варианта — отличаются только стороной MAX:

| | А: личный аккаунт (`tg_to_max_user.py`) | Б: официальный бот (`tg_to_max.py`) |
|---|---|---|
| Нужна верификация | нет | да: ИП/юрлицо, с 15.06.2026 — и самозанятые |
| Как постит в MAX | от твоего имени (неофиц. API, [PyMax](https://github.com/MaxApiTeam/PyMax)) | от имени бота (офиц. MAX Bot API) |
| Риски | формально против ToS MAX; теоретически возможна блокировка аккаунта | нет |

Общий первый шаг для обоих вариантов — **второй TG-бот**: создай в
[@BotFather](https://t.me/BotFather) ещё одного бота (например, `..._mirror_bot`),
токен → `MIRROR_BOT_TOKEN` в `.env`. Добавь его **админом** в исходный
Telegram-канал (права публикации не нужны — достаточно статуса админа, чтобы
бот видел посты). Почему второй: editor_bot уже занимает getUpdates основного
токена, и Telegram не показывает боту его собственные посты — а второй бот видит всё.

### Вариант А — личный аккаунт MAX (без ИП)

1. Создай канал в приложении MAX (если ещё нет).
2. В `.env` впиши `MAX_PHONE` — номер телефона твоего аккаунта MAX (`+7…`).
3. Узнай id канала (первый запуск спросит SMS-код — введи его в терминале;
   сессия сохранится в `max_session/`, дальше код не нужен):
   ```bash
   cd autoposter && source venv/bin/activate
   python tg_to_max_user.py --max-chats    # покажет чаты аккаунта и их id
   ```
   Числовой id канала впиши в `MAX_CHAT_ID` в `.env`.
4. Проверка: `python tg_to_max_user.py --test` — в канале появится тестовый пост.
5. Запуск: `python tg_to_max_user.py` (автозапуск — ниже).

⚠️ `max_session/` — это доступ к твоему аккаунту MAX, не выкладывай её никуда
(в `.gitignore` уже добавлена). Библиотека PyMax неофициальная: если MAX поменяет
внутренний протокол, зеркало может сломаться до обновления библиотеки
(`pip install -U maxapi-python`).

### Вариант Б — официальный бот MAX (если появится ИП/самозанятость)

1. Пройди верификацию на [dev.max.ru](https://dev.max.ru) (через Госуслуги).
2. В MAX у `@masterbot` создай бота, токен → `MAX_BOT_TOKEN` в `.env`.
   Добавь бота в канал MAX **админом** с правом публикации.
3. `python tg_to_max.py --max-chats` → id в `MAX_CHAT_ID`,
   `python tg_to_max.py --test` — проверка, `python tg_to_max.py` — запуск.
4. В LaunchAgent (ниже) поменяй скрипт на `tg_to_max.py`.

### Автозапуск (macOS)

LaunchAgent `~/Library/LaunchAgents/com.resortcapital.tgmax.plist` уже создан
и указывает на вариант А. Сначала пройди первый вход вручную (SMS-код!),
потом включи:

```bash
UID=$(id -u)
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.resortcapital.tgmax.plist
# статус и лог
launchctl print gui/$UID/com.resortcapital.tgmax | grep -E "state =|pid ="
tail -f ~/Library/Logs/resortcapital-tgmax.log
```

### Что нужно знать

- Зеркалятся **только новые посты** (с момента запуска). Старые посты канала
  Bot API не отдаёт — при желании их можно перенести руками.
- Правки постов **с медиа** не зеркалятся (только текстовых), удаления постов
  Telegram ботам не сообщает.
- Состояние — в `tg_to_max_state.json` / `tg_to_max_user_state.json`.
- Большие видео (> 20 МБ) Bot API скачивать не даёт — пост уйдёт без вложения,
  в логе будет запись.

## Настройка под себя

- **Тематика/бренд** — `CHANNEL_BRIEF` в `editor_bot.py`.
- **Тон и правила** — `SYSTEM_PROMPT`.
- **Сколько тем в день** — `THEME_COUNT` (по умолчанию 5).
- **Час рассылки и таймзона** — `POST_HOUR` / `TZ` в `.env`.

## Заметки

- Состояние черновика хранится в памяти процесса: если бот перезапустить во время
  правок, незавершённый черновик потеряется (темы просто запросишь заново через `/themes`).
- `history.json` копит заголовки опубликованных постов — они передаются модели как
  «не повторять», чтобы темы и формулировки не дублировались.
