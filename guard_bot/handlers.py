import html
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, Message, MessageId, MessageReactionUpdated, User

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
    BotCommand(command="unban", description="разбанить пользователя"),
    BotCommand(command="reg", description="зарегистрировать пользователя"),
    BotCommand(command="unreg", description="убрать регистрацию"),
    BotCommand(command="ignore", description="не пересылать сообщения"),
    BotCommand(command="unignore", description="снова пересылать сообщения"),
]


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
        elif isinstance(event, MessageReactionUpdated):
            logger.info(
                "reaction update: chat_id=%s chat_type=%s chat_title=%r user_id=%s",
                event.chat.id,
                event.chat.type,
                event.chat.title,
                event.user.id if event.user else None,
            )
            if event.user is not None:
                await self.db.upsert_seen_user(
                    user_id=event.user.id,
                    username=event.user.username,
                    first_name=event.user.first_name,
                    last_name=event.user.last_name,
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
                "Если тебя забанил бот напиши мне:\n"
                f"<code>{html.escape(settings.unban_phrase)}</code>"
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

        if await db.is_admin(user.id):
            if await handle_admin_reply_to_user(db, message):
                return

            command_result = await handle_private_admin_command(db, moderation, message, text)
            if command_result:
                return

        if is_private_status_command(text):
            await message.answer(await db.get_status_by_user(user.id, user.username))
            return

        if is_unban_phrase(text, settings.unban_phrase):
            unbanned = await moderation.unban_and_register(user.id, user.username)
            if unbanned:
                await message.answer("Разбанил.")
            else:
                await message.answer("Ты не был забанен. Пока.")
            await notify_admins(db, message)
            return

        await notify_admins(db, message)
        await message.answer(random.choice(USER_REPLIES))

    @router.message(F.chat.id.in_(settings.moderated_chat_id_set))
    async def moderated_chat_message(message: Message) -> None:
        if message.sender_chat is not None or message.is_automatic_forward:
            return

        if message.from_user is None:
            return

        user_id = message.from_user.id
        chat_id = message.chat.id
        text = message.text or ""

        if text == "ban" and message.reply_to_message is not None and await db.is_admin(user_id):
            target = message.reply_to_message.from_user
            if target is not None:
                await moderation.delete_known_user_messages(chat_id, target.id)
                await moderation.delete_message(chat_id, message.reply_to_message.message_id)
                await moderation.delete_message(chat_id, message.message_id)
                await moderation.ban_user(
                    chat_id,
                    target.id,
                    "admin_reply_ban",
                    username=target.username,
                )
                return

        if text == settings.challenge_phrase:
            challenges = await db.get_user_challenges(chat_id, user_id)
            if challenges:
                await moderation.register_by_challenge(
                    chat_id,
                    user_id,
                    message.message_id,
                    username=message.from_user.username,
                )
                return
            if await moderation.is_registered(chat_id, user_id):
                return
            await moderation.create_challenge(chat_id, user_id, message.message_id)
            return

        if await moderation.is_registered(chat_id, user_id):
            return

        await moderation.create_challenge(chat_id, user_id, message.message_id)

    @router.message_reaction()
    async def moderated_chat_reaction(event: MessageReactionUpdated) -> None:
        if event.chat.id not in settings.moderated_chat_id_set:
            return
        if event.user is None:
            return
        if not event.new_reaction:
            return

        if await moderation.is_registered(event.chat.id, event.user.id):
            return

        await moderation.ban_user(
            event.chat.id,
            event.user.id,
            "unregistered_reaction",
            username=event.user.username,
        )

    return router


async def handle_private_admin_command(
    db: Database,
    moderation: ModerationService,
    message: Message,
    text: str,
) -> bool:
    command, _, argument = text.partition(" ")
    command = command.removeprefix("/").lower()
    argument = argument.strip()
    if command in {"help", "commands"}:
        await message.answer(admin_help_text())
        return True

    if command not in {"status", "ban", "unban", "reg", "unreg", "ignore", "unignore"}:
        return False

    if not is_username_query(argument):
        await message.answer(
            (
                "Нужно так: /status @username, /ban @username, /unban @username, "
                "/reg @username, /unreg @username, /ignore @username или /unignore @username"
            )
        )
        return True

    if command == "status":
        await message.answer(await db.get_status_by_username(argument))
        return True

    user_id = await db.get_user_id_by_username(argument)
    if user_id is None:
        if command == "reg":
            await moderation.register_username(argument, "admin_private_username_reg")
            await message.answer("зареган")
            return True

        if command == "unreg":
            unregistered = await moderation.unregister_username(argument)
            await message.answer("разреган" if unregistered else "не был зареган")
            return True

        await message.answer("неизвестен")
        return True

    if command == "ignore":
        await db.ignore_user(user_id)
        logger.info("user ignored: user_id=%s username=%r", user_id, argument)
        await moderation.notify_admins(f"игнорируется {argument}")
        await message.answer("игнорируется")
        return True

    if command == "unignore":
        unignored = await db.unignore_user(user_id)
        if unignored:
            logger.info("user unignored: user_id=%s username=%r", user_id, argument)
            await moderation.notify_admins(f"больше не игнорируется {argument}")
        await message.answer("больше не игнорируется" if unignored else "не игнорировался")
        return True

    if command == "ban":
        banned = await moderation.ban_user_everywhere(
            user_id,
            "admin_private_ban",
            username=argument.removeprefix("@"),
        )
        await message.answer("забанен" if banned else "не смог забанить")
        return True

    username = argument.removeprefix("@")
    if command == "unban":
        unbanned = await moderation.unban_and_register(user_id, username)
        await message.answer("разбанен" if unbanned else "бот его не банил")
        return True

    if command == "reg":
        await moderation.register_user(user_id, "admin_private_reg", username)
        await message.answer("зареган")
        return True

    unregistered = await moderation.unregister_user(user_id, username)
    await message.answer("разреган" if unregistered else "не был зареган")
    return True


def admin_help_text() -> str:
    return (
        "Команды админа:\n"
        "/help - показать список команд\n"
        "/status @username - проверить статус\n"
        "/ban @username - забанить во всех модерируемых чатах\n"
        "/unban @username - разбанить и зарегистрировать\n"
        "/reg @username - зарегистрировать\n"
        "/unreg @username - убрать регистрацию\n"
        "/ignore @username - не пересылать личные сообщения пользователя\n"
        "/unignore @username - снова пересылать личные сообщения пользователя"
    )


def is_username_query(text: str) -> bool:
    username = text.removeprefix("@")
    return bool(username) and len(username) <= 32 and username.replace("_", "").isalnum()


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
    service_message = f"Сообщение от {user_contact_html(message.from_user)}"

    admin_ids = await db.get_admin_ids()
    for admin_id in admin_ids:
        if admin_id == message.from_user.id:
            continue
        try:
            service = await message.bot.send_message(
                admin_id,
                service_message,
                parse_mode="HTML",
            )
            await db.add_admin_message_link(admin_id, service.message_id, message.from_user.id)

            delivered = await forward_or_fallback(message, admin_id, notification)
            await db.add_admin_message_link(
                admin_id,
                delivered_message_id(delivered),
                message.from_user.id,
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


async def set_admin_commands(message: Message) -> None:
    await message.bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=message.chat.id),
    )


def is_private_status_command(text: str) -> bool:
    return text.partition(" ")[0].split("@", 1)[0].lower() == "/status"


def is_unban_phrase(text: str, unban_phrase: str) -> bool:
    return text.strip().casefold() == unban_phrase.casefold()


def user_contact_html(user: User) -> str:
    name_parts = [user.first_name, user.last_name]
    name = " ".join(part for part in name_parts if part) or user.username or str(user.id)
    username = f" (@{user.username})" if user.username else ""
    escaped_label = html.escape(f"{name}{username}")
    return f'<a href="tg://user?id={user.id}">{escaped_label}</a>'


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
