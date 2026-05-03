# ithueti guard bot

Telegram-бот модератор для чата комментариев канала.

## Что делает

- Проверяет незарегистрированных пользователей в модерируемом чате.
- На первое сообщение неизвестного пользователя отвечает вопросом с фразой `Мамой клянусь`.
- Если пользователь отвечает строго `Мамой клянусь` в течение минуты, бот регистрирует его.
- После успешной проверки бот удаляет свое сообщение с вопросом и ответ пользователя.
- Если пользователь не ответил за минуту, бот удаляет исходное сообщение и банит пользователя.
- Администраторы, участники чата и подписчики настроенного канала регистрируются автоматически.
- Реакции от незарегистрированных пользователей приводят к бану.
- Пользователь, которого забанил бот, может написать боту в личку `Я не шлюхобот`; бот разбанит и зарегистрирует его.
- Все личные сообщения боту пересылаются админам бота в виде `@username: текст сообщения`.
- Все баны, регистрации и разрегистрации отправляются админам в личку.

## Админы бота

Чтобы стать админом бота, нужно написать ему в личку значение `ADMIN_SECRET` из `.env`.

В ответ бот напишет:

```text
Что, новый хозяин, надо?!
```

После этого в личке доступны команды:

```text
help
@username
ban @username
unban @username
reg @username
unreg @username
```

Команды со слэшем тоже поддерживаются:

```text
/help
/reg @username
/unreg @username
```

Команда `@username` показывает один из статусов:

```text
зареган
забанен
неизвестен
```

`reg @username` и `unreg @username` работают даже для пользователей, которых бот еще не видел. В этом случае регистрация хранится по username и применяется, когда пользователь впервые появляется в модерируемом чате.

`ban @username` и `unban @username` требуют, чтобы бот уже видел пользователя, потому что Telegram Bot API не дает получить `user_id` по произвольному username.

`unban @username` работает только с банами, которые сделал сам бот и сохранил в базе. Если пользователя забанил другой админ вручную, бот такой бан не снимет.

В модерируемом чате админ может ответить на сообщение пользователя текстом:

```text
ban
```

Бот удалит команду, удалит сообщение-цель, удалит известные challenge-сообщения этого пользователя и забанит его в этом чате.

## Настройка

Создай виртуальное окружение и установи зависимости:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Заполни `.env`, затем запусти:

```bash
python -m guard_bot
```

Бот должен быть администратором в модерируемом чате с правами на удаление сообщений и бан пользователей.

## Переменные окружения

Основные переменные лежат в `.env`:

```env
BOT_TOKEN=123456:telegram-bot-token
ADMIN_SECRET=change-me
REQUIRED_CHANNEL=@ithueti
MODERATED_CHAT_IDS=-1001234567890
DATABASE_PATH=guard_bot.sqlite3
CHALLENGE_TIMEOUT_SECONDS=60
CHALLENGE_PHRASE=Мамой клянусь
UNBAN_PHRASE=Я не шлюхобот
LOG_LEVEL=INFO
```

`MODERATED_CHAT_IDS` можно указать списком через запятую:

```env
MODERATED_CHAT_IDS=-1001234567890,-1009876543210
```

## Как узнать chat_id

Временно оставь список модерируемых чатов пустым:

```env
MODERATED_CHAT_IDS=
LOG_LEVEL=INFO
```

Добавь бота в чат, отправь туда сообщение и посмотри логи. Бот напишет строку вида:

```text
message update: chat_id=-1001234567890 chat_type=supergroup chat_title='...'
```

Возьми `chat_id` из лога и впиши его в `MODERATED_CHAT_IDS`.

## Docker

Сборка и запуск через Docker Compose:

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

Compose читает `.env`, а SQLite-база хранится в директории `state` рядом с `.env`.

Сборка и запуск без Compose:

```bash
docker build -t ithueti-guard-bot .
mkdir -p state
docker run --env-file .env -e DATABASE_PATH=/state/guard_bot.sqlite3 -v "$PWD/state:/state" ithueti-guard-bot
```

## База данных

При локальном запуске база лежит по пути из `DATABASE_PATH`.

При запуске через Docker Compose база лежит в директории `state`:

```text
state/guard_bot.sqlite3
```

Директория `state/` добавлена в `.gitignore`, как и SQLite-файлы `*.sqlite3` и `*.sqlite3-*`.

Посмотреть файл базы:

```bash
ls -la state
```

Удалить локальную базу:

```bash
docker compose down
rm -rf state
```

## GitHub Actions

Workflow `.github/workflows/docker.yml` собирает Docker-образ на pull request и пушит его в GitHub Container Registry при пуше в `main`, при тегах вида `v1.2.3` и при ручном запуске.

Образ публикуется сюда:

```text
ghcr.io/<owner>/<repo>
```

Теги:

```text
latest
<branch>
<tag>
sha-<commit>
```
