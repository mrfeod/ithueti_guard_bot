import html
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, Message, MessageId, MessageReactionUpdated

from guard_bot.config import Settings
from guard_bot.db import Database
from guard_bot.moderation import ModerationService

logger = logging.getLogger(__name__)

USER_REPLIES = ("Ясно", "Понятно", "Ок", "Принято", "Лады", "Дакси", "Угу", "Ну дык")
USER_COMMANDS = [BotCommand(command="status", description="проверить статус")]
ADMIN_COMMANDS = [
    BotCommand(command="help", description="команды админа"),
    BotCommand(command="status", description="проверить статус пользователя"),
    BotCommand(command="ban", description="забанить пользователя"),
    BotCommand(command="remove", description="забанить в чате и канале"),
    BotCommand(command="unban", description="разбанить пользователя"),
    BotCommand(command="reg", description="зарегистрировать пользователя"),
    BotCommand(command="unreg", description="убрать регистрацию"),
    BotCommand(command="mod", description="дать права модератора"),
    BotCommand(command="demod", description="забрать права модератора"),
    BotCommand(command="modlist", description="список модераторов"),
    BotCommand(command="ignorelist", description="список игнорируемых"),
    BotCommand(command="banlist", description="список забаненных"),
    BotCommand(command="reglist", description="список зарегистрированных"),
    BotCommand(command="sublist", description="список подписчиков канала"),
    BotCommand(command="ignore", description="не пересылать сообщения"),
    BotCommand(command="unignore", description="снова пересылать сообщения"),
]
ADMIN_COMMAND_NAMES = {
    "help",
    "commands",
    "status",
    "ban",
    "remove",
    "unban",
    "reg",
    "unreg",
    "mod",
    "demod",
    "modlist",
    "ignorelist",
    "banlist",
    "reglist",
    "sublist",
    "ignore",
    "unignore",
}
MODERATOR_CHAT_COMMAND_NAMES = {"ban", "remove", "unban"}
ADMIN_USER_ARGUMENT_COMMAND_NAMES = {
    "status",
    "ban",
    "remove",
    "unban",
    "reg",
    "unreg",
    "mod",
    "demod",
    "ignore",
    "unignore",
}
ADMIN_LIST_COMMAND_NAMES = {"modlist", "ignorelist", "banlist", "reglist", "sublist"}
ADMIN_PENDING_COMMANDS: dict[int, str] = {}
DEFAULT_LIST_LIMIT = 10


class ChatIdLoggingMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[Message | MessageReactionUpdated, dict[str, Any]], Awaitable[Any]],
        event: Message | MessageReactionUpdated,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            logger.info(
                (
                    "message update: chat_id=%s chat_type=%s chat_title=%r "
                    "from_user_id=%s sender_chat_id=%s is_automatic_forward=%s"
                ),
                event.chat.id,
                event.chat.type,
                event.chat.title,
                event.from_user.id if event.from_user else None,
                event.sender_chat.id if event.sender_chat else None,
                event.is_automatic_forward,
            )
            if event.from_user is not None:
                await self.db.upsert_seen_user(
                    user_id=event.from_user.id,
                    username=event.from_user.username,
                    first_name=event.from_user.first_name,
                    last_name=event.from_user.last_name,
                )
            if event.sender_chat is not None:
                await self.db.upsert_seen_chat(
                    chat_id=event.sender_chat.id,
                    username=event.sender_chat.username,
                    title=event.sender_chat.title,
                    chat_type=event.sender_chat.type,
                )
        elif isinstance(event, MessageReactionUpdated):
            logger.info(
                (
                    "reaction update: chat_id=%s chat_type=%s chat_title=%r "
                    "user_id=%s actor_chat_id=%s"
                ),
                event.chat.id,
                event.chat.type,
                event.chat.title,
                event.user.id if event.user else None,
                event.actor_chat.id if event.actor_chat else None,
            )
            if event.user is not None:
                await self.db.upsert_seen_user(
                    user_id=event.user.id,
                    username=event.user.username,
                    first_name=event.user.first_name,
                    last_name=event.user.last_name,
                )
            if event.actor_chat is not None:
                await self.db.upsert_seen_chat(
                    chat_id=event.actor_chat.id,
                    username=event.actor_chat.username,
                    title=event.actor_chat.title,
                    chat_type=event.actor_chat.type,
                )

        return await handler(event, data)


def create_router(db: Database, moderation: ModerationService, settings: Settings) -> Router:
    router = Router()
    router.message.outer_middleware(ChatIdLoggingMiddleware(db))
    router.message_reaction.outer_middleware(ChatIdLoggingMiddleware(db))

    @router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def start_private(message: Message) -> None:
        if message.from_user is None:
            return
        await message.bot.set_my_commands(
            USER_COMMANDS,
            scope=BotCommandScopeChat(chat_id=message.chat.id),
        )
        if await db.is_admin(message.from_user.id):
            await set_admin_commands(message)
        await message.answer(
            (
                f"Подпишись на {html.escape(settings.required_channel)} и проблем не будет. "
                "Если тебя забанил бот, напиши мне любое сообщение."
            ),
            parse_mode="HTML",
        )

    @router.message(F.chat.type == ChatType.PRIVATE)
    async def private_message(message: Message) -> None:
        if message.from_user is None:
            return

        text = message.text or message.caption or ""
        user = message.from_user

        if text == settings.admin_secret:
            await db.add_admin(user.id, user.username)
            logger.info("admin added: user_id=%s username=%r", user.id, user.username)
            await set_admin_commands(message)
            await message.answer("Что, новый хозяин, надо?!")
            return

        is_admin = await db.is_admin(user.id)
        if is_admin:
            command_result = await handle_private_admin_command(db, moderation, message, text)
            if command_result:
                return

            if await handle_admin_reply_to_user(db, message):
                return

        if is_private_status_command(text):
            await message.answer(await db.get_status_by_user(user.id, user.username))
            return

        if not is_admin:
            unbanned = await moderation.unban_and_register(user.id, user.username)
            if unbanned:
                await message.answer("Разбанил.")
                await notify_admins(db, message)
                return

        await notify_admins(db, message)
        await message.answer(random.choice(USER_REPLIES))

    @router.message(F.chat.id.in_(settings.moderated_chat_id_set))
    async def moderated_chat_message(message: Message) -> None:
        if message.new_chat_members:
            await handle_new_chat_members(db, moderation, message)
            return

        if message.sender_chat is not None:
            if is_required_sender_chat(message.sender_chat.username, settings.required_channel):
                return
            if message.sender_chat.type == ChatType.CHANNEL:
                await handle_sender_chat_message(db, moderation, settings, message)
            return

        if message.from_user is None:
            return

        user_id = message.from_user.id
        chat_id = message.chat.id
        text = message.text or ""

        is_admin = await db.is_admin(user_id)
        is_moderator = await db.is_moderator(user_id)
        if is_admin or is_moderator:
            if await handle_chat_admin_command(
                db,
                moderation,
                message,
                text,
                is_admin=is_admin,
            ):
                return

        challenges = await db.get_user_challenges(chat_id, user_id)
        if is_reply_to_this_bot(message):
            if challenges:
                await moderation.register_by_challenge(
                    chat_id,
                    user_id,
                    message.message_id,
                    username=message.from_user.username,
                )
            else:
                await moderation.delete_message(chat_id, message.message_id)
            return

        if await moderation.is_registered(chat_id, user_id):
            return

        await moderation.create_challenge(chat_id, user_id, message.message_id)

    @router.message_reaction()
    async def moderated_chat_reaction(event: MessageReactionUpdated) -> None:
        if event.chat.type == ChatType.PRIVATE:
            if await handle_admin_reaction_to_user(db, moderation, event):
                return

        if event.chat.id not in settings.moderated_chat_id_set:
            return
        if not event.new_reaction:
            return

        if event.user is not None:
            actor_id = event.user.id
        elif event.actor_chat is not None:
            if is_required_sender_chat(event.actor_chat.username, settings.required_channel):
                return
            actor_id = event.actor_chat.id
        else:
            return

        if await moderation.is_registered(event.chat.id, actor_id):
            return

        await moderation.ban_user(event.chat.id, actor_id, "unregistered_reaction")

    return router


async def handle_new_chat_members(
    db: Database,
    moderation: ModerationService,
    message: Message,
) -> None:
    if not message.new_chat_members:
        return

    for user in message.new_chat_members:
        await db.upsert_seen_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        if await moderation.is_registered(message.chat.id, user.id):
            continue
        await moderation.create_challenge(message.chat.id, user.id, message.message_id)


async def handle_sender_chat_message(
    db: Database,
    moderation: ModerationService,
    settings: Settings,
    message: Message,
) -> None:
    if message.sender_chat is None:
        return

    sender_chat_id = message.sender_chat.id
    chat_id = message.chat.id
    username = message.sender_chat.username

    challenges = await db.get_user_challenges(chat_id, sender_chat_id)
    if challenges:
        await moderation.register_by_challenge(
            chat_id,
            sender_chat_id,
            message.message_id,
            username=username,
        )
        return

    if await db.is_registered(sender_chat_id):
        return

    await moderation.create_challenge(chat_id, sender_chat_id, message.message_id)


async def handle_private_admin_command(
    db: Database,
    moderation: ModerationService,
    message: Message,
    text: str,
) -> bool:
    command, argument = parse_admin_command(text)
    admin_id = message.from_user.id if message.from_user is not None else None
    if command is None and admin_id is not None and admin_id in ADMIN_PENDING_COMMANDS:
        command = ADMIN_PENDING_COMMANDS.pop(admin_id)
        argument = text.strip()

    if command is None:
        return False

    if command in {"help", "commands"}:
        if admin_id is not None:
            ADMIN_PENDING_COMMANDS.pop(admin_id, None)
        await message.answer(admin_help_text())
        return True

    if command in ADMIN_LIST_COMMAND_NAMES:
        if admin_id is not None:
            ADMIN_PENDING_COMMANDS.pop(admin_id, None)
        limit = parse_list_limit(argument)
        if limit is None:
            await message.answer("укажи целое число больше 0")
            return True
        await answer_long(message, await admin_list_text(db, moderation, command, limit))
        return True

    if command not in ADMIN_COMMAND_NAMES:
        return False

    reply_target_user_id = None
    reply_target_username = None
    if message.from_user is not None and message.reply_to_message is not None:
        reply_target_user_id = await db.get_admin_message_link_user_id(
            admin_id=message.from_user.id,
            admin_message_id=message.reply_to_message.message_id,
        )
        if reply_target_user_id is not None:
            reply_target_username = await db.get_username(reply_target_user_id)

    target_user_id = parse_user_id_query(argument)
    if target_user_id is None and not is_username_query(argument):
        if reply_target_user_id is not None:
            if admin_id is not None:
                ADMIN_PENDING_COMMANDS.pop(admin_id, None)
            await handle_admin_user_command(
                db,
                moderation,
                message,
                command,
                reply_target_user_id,
                reply_target_username,
            )
            return True

        if command in ADMIN_USER_ARGUMENT_COMMAND_NAMES and not argument and admin_id is not None:
            ADMIN_PENDING_COMMANDS[admin_id] = command
            await message.answer(admin_username_prompt(command))
            return True

        await message.answer(admin_username_prompt(command))
        return True

    if admin_id is not None:
        ADMIN_PENDING_COMMANDS.pop(admin_id, None)

    if target_user_id is not None:
        target_username = await db.get_username(target_user_id)
        await handle_admin_user_command(
            db,
            moderation,
            message,
            command,
            target_user_id,
            target_username,
        )
        return True

    if command == "status":
        user_id = await resolve_username_target(db, message, argument)
        if user_id is not None:
            await message.answer(await db.get_status_by_user(user_id, argument.removeprefix("@")))
        else:
            await message.answer(await db.get_status_by_username(argument))
        return True

    user_id = await resolve_username_target(db, message, argument)
    if user_id is None:
        if await handle_unknown_username_admin_command(db, moderation, message, command, argument):
            return True

        await message.answer("неизвестен")
        return True

    await handle_admin_user_command(
        db,
        moderation,
        message,
        command,
        user_id,
        argument.removeprefix("@"),
    )
    return True


async def handle_unknown_username_admin_command(
    db: Database,
    moderation: ModerationService,
    message: Message,
    command: str,
    username: str,
    *,
    silent: bool = False,
) -> bool:
    if command == "reg":
        await moderation.register_username(username, "admin_private_username_reg")
        if not silent:
            await message.answer("зареган")
        return True

    if command == "unreg":
        unregistered = await moderation.unregister_username(username)
        if not silent:
            await message.answer("разреган" if unregistered else "не был зареган")
        return True

    if command == "mod":
        await db.add_moderator_username(username)
        normalized = db.normalize_username(username)
        logger.info("moderator username added: username=%r", normalized)
        await moderation.notify_admins(f"модератор @{normalized}")
        if not silent:
            await message.answer("модератор")
        return True

    if command == "demod":
        removed = await db.remove_moderator_username(username)
        normalized = db.normalize_username(username)
        if removed:
            logger.info("moderator username removed: username=%r", normalized)
            await moderation.notify_admins(f"больше не модератор @{normalized}")
        if not silent:
            await message.answer("больше не модератор" if removed else "не был модератором")
        return True

    return False


async def handle_admin_user_command(
    db: Database,
    moderation: ModerationService,
    message: Message,
    command: str,
    user_id: int,
    username: str | None = None,
    *,
    silent: bool = False,
) -> None:
    user_label = f"@{username}" if username else await moderation.get_user_label(user_id)

    if command == "status":
        if not silent:
            await message.answer(await db.get_status_by_user(user_id, username))
        return

    if command == "ignore":
        await db.ignore_user(user_id)
        logger.info("user ignored: user_id=%s username=%r", user_id, username)
        await moderation.notify_admins(f"игнорируется {user_label}")
        if not silent:
            await message.answer("игнорируется")
        return

    if command == "unignore":
        unignored = await db.unignore_user(user_id)
        if unignored:
            logger.info("user unignored: user_id=%s username=%r", user_id, username)
            await moderation.notify_admins(f"больше не игнорируется {user_label}")
        if not silent:
            await message.answer("больше не игнорируется" if unignored else "не игнорировался")
        return

    if command == "ban":
        banned = await moderation.ban_user_everywhere(
            user_id,
            "admin_private_ban",
            username=username,
        )
        if not silent:
            await message.answer("забанен" if banned else "не смог забанить")
        return

    if command == "remove":
        removed = await moderation.remove_user_everywhere(
            user_id,
            "admin_private_remove",
            username=username,
        )
        if not silent:
            await message.answer("удален" if removed else "не смог удалить")
        return

    if command == "unban":
        unbanned = await moderation.unban_user_everywhere_and_register(user_id, username)
        if not silent:
            await message.answer("разбанен" if unbanned else "бот его не банил")
        return

    if command == "reg":
        await moderation.register_user(user_id, "admin_private_reg", username)
        if not silent:
            await message.answer("зареган")
        return

    if command == "mod":
        await db.add_moderator(user_id, username)
        logger.info("moderator added: user_id=%s username=%r", user_id, username)
        await moderation.notify_admins(f"модератор {user_label}")
        if not silent:
            await message.answer("модератор")
        return

    if command == "demod":
        removed = await db.remove_moderator(user_id)
        if removed:
            logger.info("moderator removed: user_id=%s username=%r", user_id, username)
            await moderation.notify_admins(f"больше не модератор {user_label}")
        if not silent:
            await message.answer("больше не модератор" if removed else "не был модератором")
        return

    unregistered = await moderation.unregister_user(user_id, username)
    if not silent:
        await message.answer("разреган" if unregistered else "не был зареган")


async def handle_chat_admin_command(
    db: Database,
    moderation: ModerationService,
    message: Message,
    text: str,
    *,
    is_admin: bool,
) -> bool:
    command, _argument = parse_admin_command(text)
    if command is None:
        return False
    if not is_admin and command not in MODERATOR_CHAT_COMMAND_NAMES:
        return False
    if command in {"help", "commands"}:
        await moderation.delete_message(message.chat.id, message.message_id)
        return True
    if command in ADMIN_LIST_COMMAND_NAMES:
        await moderation.delete_message(message.chat.id, message.message_id)
        return True
    if command not in ADMIN_COMMAND_NAMES:
        return False

    target_user_id = None
    target_username = None
    command, argument = parse_admin_command(text)
    target_user_id = parse_user_id_query(argument)
    if target_user_id is not None:
        target_username = await db.get_username(target_user_id)
    elif is_username_query(argument):
        target_user_id = await resolve_username_target(db, message, argument)
        target_username = argument.removeprefix("@")
        if target_user_id is None:
            if await handle_unknown_username_admin_command(
                db,
                moderation,
                message,
                command,
                argument,
                silent=True,
            ):
                await moderation.delete_message(message.chat.id, message.message_id)
                return True
            await moderation.delete_message(message.chat.id, message.message_id)
            return True
    elif message.reply_to_message is not None:
        target_user_id, target_username = reply_target_identity(message.reply_to_message)
        if target_user_id is None:
            return False
    else:
        return False

    await handle_admin_user_command(
        db,
        moderation,
        message,
        command,
        target_user_id,
        target_username,
        silent=True,
    )

    if command not in {"ban", "remove"}:
        await moderation.delete_message(message.chat.id, message.message_id)

    if command in {"ban", "remove"}:
        await moderation.delete_known_user_messages(message.chat.id, target_user_id)
        if message.reply_to_message is not None:
            await moderation.delete_message(message.chat.id, message.reply_to_message.message_id)
        await moderation.delete_message(message.chat.id, message.message_id)

    return True


def admin_help_text() -> str:
    return (
        "Команды админа:\n"
        "/help - показать список команд\n"
        "/status @username - проверить статус\n"
        "/ban @username - забанить во всех модерируемых чатах\n"
        "/remove @username - забанить во всех чатах и в канале\n"
        "/unban @username - разбанить в чатах и канале, затем зарегистрировать\n"
        "/reg @username - зарегистрировать\n"
        "/unreg @username - убрать регистрацию\n"
        "/mod @username - дать права модератора\n"
        "/demod @username - забрать права модератора\n"
        "/modlist [число] - список модераторов\n"
        "/ignorelist [число] - список игнорируемых\n"
        "/banlist [число] - список забаненных\n"
        "/reglist [число] - список зарегистрированных\n"
        "/sublist [число] - список известных подписчиков канала\n"
        "/ignore @username - не пересылать личные сообщения пользователя\n"
        "/unignore @username - снова пересылать личные сообщения пользователя\n"
        "Эти же команды можно писать ответом на сообщение пользователя или канала без аргумента."
    )


def admin_username_prompt(command: str) -> str:
    return f"Пришли username для /{command}: @username"


async def admin_list_text(
    db: Database,
    moderation: ModerationService,
    command: str,
    limit: int,
) -> str:
    if command == "modlist":
        return await modlist_text(db, limit)
    if command == "ignorelist":
        return await ignorelist_text(db, limit)
    if command == "banlist":
        return await banlist_text(db, limit)
    if command == "reglist":
        return await reglist_text(db, limit)
    return await sublist_text(db, moderation, limit)


async def modlist_text(db: Database, limit: int) -> str:
    moderators = await db.get_moderators(limit)
    if not moderators:
        return "модераторов нет"

    lines = ["Модераторы:"]
    for moderator in moderators:
        username = moderator["username"]
        if username:
            lines.append(f"@{username}")
        else:
            lines.append(str(moderator["user_id"]))
    return "\n".join(lines)


async def ignorelist_text(db: Database, limit: int) -> str:
    ignored_users = await db.get_ignored_users(limit)
    if not ignored_users:
        return "ignore-список пуст"

    lines = ["Ignore:"]
    for user in ignored_users:
        lines.append(user_label(user["user_id"], user["username"]))
    return "\n".join(lines)


async def banlist_text(db: Database, limit: int) -> str:
    banned_users = await db.get_banned_users(limit)
    if not banned_users:
        return "банлист пуст"

    lines = ["Баны:"]
    for user in banned_users:
        lines.append(
            f"{user_label(user['user_id'], user['username'], user['first_name'])} - {user['bans']}"
        )
    return "\n".join(lines)


async def reglist_text(db: Database, limit: int) -> str:
    registered_entries = await db.get_registered_entries(limit)
    if not registered_entries:
        return "регистраций нет"

    lines = ["Зарегистрированы:"]
    for entry in registered_entries:
        user_id = entry["user_id"]
        username = entry["username"]
        if user_id is None:
            lines.append(f"@{username}")
        else:
            lines.append(user_label(user_id, username, entry["first_name"]))
    return "\n".join(lines)


async def sublist_text(db: Database, moderation: ModerationService, limit: int) -> str:
    seen_users = await db.get_seen_users()
    subscribers = []
    for user in seen_users:
        user_id = int(user["user_id"])
        if user_id < 0:
            continue
        if await moderation.is_channel_subscriber(user_id):
            subscribers.append(user_label(user_id, user["username"]))
        if len(subscribers) >= limit:
            break

    if not subscribers:
        return "известных подписчиков нет"

    return "Известные подписчики канала:\n" + "\n".join(subscribers)


def user_label(user_id: int | None, username: str | None, first_name: str | None = None) -> str:
    if username:
        return f"@{username}"
    if user_id is not None and first_name:
        return first_name
    return str(user_id)


async def answer_long(message: Message, text: str) -> None:
    max_length = 4000
    if len(text) <= max_length:
        await message.answer(text)
        return

    lines = text.splitlines()
    chunk = ""
    for line in lines:
        if len(line) > max_length:
            if chunk:
                await message.answer(chunk)
                chunk = ""
            for start in range(0, len(line), max_length):
                await message.answer(line[start : start + max_length])
            continue

        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > max_length:
            await message.answer(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await message.answer(chunk)


def parse_admin_command(text: str) -> tuple[str | None, str]:
    command, _, argument = text.partition(" ")
    command = command.removeprefix("/").split("@", 1)[0].lower()
    if command not in ADMIN_COMMAND_NAMES:
        return None, ""
    return command, argument.strip()


def parse_list_limit(argument: str) -> int | None:
    if not argument:
        return DEFAULT_LIST_LIMIT
    if not argument.isdecimal():
        return None
    limit = int(argument)
    return limit if limit > 0 else None


async def resolve_username_target(
    db: Database,
    message: Message,
    username: str,
) -> int | None:
    user_id = await db.get_user_id_by_username(username)
    if user_id is not None:
        return user_id

    normalized = username if username.startswith("@") else f"@{username}"
    try:
        chat = await message.bot.get_chat(normalized)
    except TelegramAPIError:
        logger.info("failed to resolve username target: username=%r", username)
        return None

    if chat.type in {ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP}:
        await db.upsert_seen_chat(
            chat_id=chat.id,
            username=chat.username,
            title=chat.title,
            chat_type=str(chat.type),
        )
        return chat.id

    if chat.type == ChatType.PRIVATE:
        await db.upsert_seen_user(
            user_id=chat.id,
            username=chat.username,
            first_name=getattr(chat, "first_name", None),
            last_name=getattr(chat, "last_name", None),
        )
        return chat.id

    return None


def is_username_query(text: str) -> bool:
    username = text.removeprefix("@")
    return bool(username) and len(username) <= 32 and username.replace("_", "").isalnum()


def parse_user_id_query(text: str) -> int | None:
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_required_sender_chat(sender_username: str | None, required_channel: str) -> bool:
    if sender_username is None:
        return False
    return sender_username.lower() == required_channel.removeprefix("@").lower()


def reply_target_identity(message: Message) -> tuple[int | None, str | None]:
    if message.sender_chat is not None and message.sender_chat.type == ChatType.CHANNEL:
        return message.sender_chat.id, message.sender_chat.username

    if message.from_user is not None:
        return message.from_user.id, message.from_user.username

    return None, None


def is_reply_to_this_bot(message: Message) -> bool:
    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        return False
    return reply.from_user.id == message.bot.id


async def notify_admins(db: Database, message: Message) -> None:
    if message.from_user is None:
        return

    if await db.is_ignored(message.from_user.id):
        logger.info(
            "ignored private message: user_id=%s message_id=%s",
            message.from_user.id,
            message.message_id,
        )
        return

    username = message.from_user.username
    sender = f"@{username}" if username else str(message.from_user.id)
    text = message.text or message.caption or "<non-text message>"
    notification = f"{sender}: {text}"

    admin_ids = await db.get_admin_ids()
    for admin_id in admin_ids:
        if admin_id == message.from_user.id:
            continue
        try:
            delivered = await forward_or_fallback(message, admin_id, notification)
            await db.add_admin_message_link(
                admin_id,
                delivered_message_id(delivered),
                message.from_user.id,
                message.message_id,
            )
        except Exception:
            logger.exception("failed to notify admin %s", admin_id)


async def handle_admin_reply_to_user(db: Database, message: Message) -> bool:
    if message.from_user is None or message.reply_to_message is None:
        return False

    target_user_id = await db.get_admin_message_link_user_id(
        admin_id=message.from_user.id,
        admin_message_id=message.reply_to_message.message_id,
    )
    if target_user_id is None:
        return False

    try:
        await message.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception:
        logger.exception(
            "failed to send admin reply %s to user %s",
            message.message_id,
            target_user_id,
        )
        await message.answer("Не смог отправить.")
        return True

    logger.info(
        "admin reply sent: admin_id=%s user_id=%s message_id=%s",
        message.from_user.id,
        target_user_id,
        message.message_id,
    )
    await message.answer("Отправил.")
    return True


async def handle_admin_reaction_to_user(
    db: Database,
    moderation: ModerationService,
    event: MessageReactionUpdated,
) -> bool:
    if event.user is None:
        return False
    if not event.new_reaction:
        return False
    if not await db.is_admin(event.user.id):
        return False

    link = await db.get_admin_message_link(
        admin_id=event.user.id,
        admin_message_id=event.message_id,
    )
    if link is None:
        return False

    user_message_id = int(link["user_message_id"])
    if not user_message_id:
        await moderation.bot.send_message(event.chat.id, "Не смог ответить.")
        return True

    try:
        await moderation.bot.set_message_reaction(
            chat_id=int(link["user_id"]),
            message_id=user_message_id,
            reaction=event.new_reaction,
        )
    except Exception:
        logger.exception(
            "failed to set reaction for user %s message %s",
            int(link["user_id"]),
            user_message_id,
        )
        await moderation.bot.send_message(event.chat.id, "Не смог ответить.")
        return True

    logger.info(
        "admin reaction sent: admin_id=%s user_id=%s message_id=%s",
        event.user.id,
        int(link["user_id"]),
        user_message_id,
    )
    await moderation.bot.send_message(event.chat.id, "Ответил.")
    return True


async def set_admin_commands(message: Message) -> None:
    await message.bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=message.chat.id),
    )


def is_private_status_command(text: str) -> bool:
    return text.partition(" ")[0].split("@", 1)[0].lower() == "/status"


async def forward_or_fallback(message: Message, admin_id: int, fallback: str) -> Message | MessageId:
    try:
        return await message.bot.forward_message(
            chat_id=admin_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception:
        logger.exception("failed to forward message %s to admin %s", message.message_id, admin_id)

    try:
        return await message.bot.copy_message(
            chat_id=admin_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception:
        logger.exception("failed to copy message %s to admin %s", message.message_id, admin_id)

    return await message.bot.send_message(admin_id, fallback)


def delivered_message_id(message: Message | MessageId) -> int:
    return message.message_id
