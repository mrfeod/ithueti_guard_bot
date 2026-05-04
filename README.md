# ithueti guard bot

Telegram-бот для модерации чата комментариев канала.

## Функциональность

- Проверяет новых или неизвестных пользователей в модерируемых чатах.
- На первое сообщение неизвестного пользователя отвечает challenge-фразой.
- Если пользователь ответил правильной фразой за отведенное время, бот регистрирует его.
- Если пользователь не ответил, бот удаляет исходное сообщение и банит пользователя.
- Администраторы, участники чата и подписчики `REQUIRED_CHANNEL` регистрируются автоматически.
- Посты канала и автопосты из канала не проходят challenge-проверку.
- Реакции от незарегистрированных пользователей приводят к бану.
- В личке пользователь может проверить статус командой `/status`: отдельно статус в чате и ignore-статус в боте.
- Пользователь может написать `UNBAN_PHRASE`, чтобы бот снял сохраненный бан и зарегистрировал его.
- Прочие личные сообщения и медиа пересылаются админам.
- Админка в личке включается фразой `ADMIN_SECRET`.
- Админу доступны `/help`, `/status @username`, `/ban @username`, `/remove @username`, `/unban @username`, `/reg @username`, `/unreg @username`, `/ignore @username`, `/unignore @username`.
- `/remove @username` банит пользователя в модерируемых чатах и в `REQUIRED_CHANNEL`.
- `/unban @username` разбанивает пользователя в модерируемых чатах и в `REQUIRED_CHANNEL`, затем регистрирует его.
- Админские команды можно писать с username или ответом на сообщение пользователя. Reply-режим работает в чате и в личке бота на пересланных админу сообщениях.
- Пользователей из ignore-списка бот не пересылает админам в личке.

## .env

Бот читает настройки из `.env`:

```env
BOT_TOKEN=123456:telegram-bot-token
ADMIN_SECRET=change-me

REQUIRED_CHANNEL=@ithueti
MODERATED_CHAT_IDS=-1001234567890

DATABASE_PATH=/state/guard_bot.sqlite3
CHALLENGE_TIMEOUT_SECONDS=60
CHALLENGE_PHRASE=Мамой клянусь
UNBAN_PHRASE=Я не шлюхобот
LOG_LEVEL=INFO
```

Переменные:

- `BOT_TOKEN` - токен Telegram-бота из BotFather.
- `ADMIN_SECRET` - фраза, которая включает админку в личке бота.
- `REQUIRED_CHANNEL` - публичный канал, подписчики которого проходят регистрацию автоматически.
- `MODERATED_CHAT_IDS` - id модерируемых чатов через запятую.
- `DATABASE_PATH` - путь к SQLite-базе внутри контейнера.
- `CHALLENGE_TIMEOUT_SECONDS` - время на ответ challenge-фразой.
- `CHALLENGE_PHRASE` - фраза для регистрации в чате.
- `UNBAN_PHRASE` - фраза для разбана в личке.
- `LOG_LEVEL` - уровень логов.

## Запуск Через Docker

Создай `.env`, затем запусти:

```bash
docker compose up --build -d
```

Логи:

```bash
docker compose logs -f bot
```

Остановка:

```bash
docker compose down
```

## State

SQLite-база хранится в директории `state` рядом с проектом:

```text
state/guard_bot.sqlite3
```

`docker-compose.yml` монтирует эту директорию в контейнер как `/state`, поэтому для Docker-запуска `DATABASE_PATH` должен указывать на файл внутри `/state`.

Директория `state/` добавлена в `.gitignore`. Если удалить `state`, бот потеряет зарегистрированных пользователей, сохраненные баны, список админов, ignore-список и активные challenge-проверки.
