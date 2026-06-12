# Клёвое Место PRO — Telegram-бот продаж

Бот принимает оплату подписки, выдаёт логин/пароль и записывает их в `users.json`
в GitHub-репозитории. Веб-приложение (`app.html`) при входе сверяет логин и
SHA-256-хэш пароля с этим файлом.

## Запуск за 5 минут

1. **Создайте бота**: в Telegram откройте [@BotFather](https://t.me/BotFather) →
   `/newbot` → имя «Клёвое Место PRO» → username, например `klevoe_mesto_pro_bot`.
   Скопируйте токен.

2. **Впишите username бота** в `index.html` (константа `TG_BOT_URL` внизу файла)
   и запушьте.

3. **Настройте окружение**:
   ```bash
   cd bot
   cp .env.example .env        # и заполните BOT_TOKEN и GITHUB_TOKEN
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Запустите**:
   ```bash
   export $(grep -v '^#' .env | xargs)   # или задайте переменные вручную
   python bot.py
   ```

Бот работает на long polling — хватит обычного компьютера или дешёвого VPS.

## Реальная оплата

Без `PAYMENT_PROVIDER_TOKEN` бот работает в демо-режиме (кнопка «Оплатить (демо)»).

Для настоящих платежей: BotFather → ваш бот → **Bot Settings → Payments** →
подключите ЮKassa (нужен аккаунт ЮKassa и ИП/самозанятость) → полученный токен
впишите в `PAYMENT_PROVIDER_TOKEN`. Код уже поддерживает оба режима.

## Как устроен доступ

- Бот пишет в `users.json`: `{login, hash, until, tg_id}`, где
  `hash = sha256("login:password:km2026")`.
- GitHub Pages раздаёт файл, приложение сверяет хэш на клиенте.
- Продление: повторная покупка тем же логином прибавляет дни к текущей дате окончания.
- Команда `/status` показывает подписку пользователя.

⚠️ Это MVP-схема: файл с хэшами публичен, а проверка происходит на клиенте.
Для серьёзного продакшена перенесите проверку на бэкенд (Supabase / FastAPI / Cloudflare Workers).
