import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message, MessageReactionUpdated

from guard_bot.config import Settings
from guard_bot.db import Database
from guard_bot.moderation import ModerationService

logger = logging.getLogger(__name__)


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
        await message.answer("Напиши код администратора или фразу для разбана.")

    @router.message(F.chat.type == ChatType.PRIVATE)
    async def private_message(message: Message) -> None:
        if message.from_user is None:
            return

        text = message.text or message.caption or ""
        user = message.from_user

        if text == settings.admin_secret:
            await db.add_admin(user.id, user.username)
            await message.answer("Что, новый хозяин, надо?!")
            return

        if await db.is_admin(user.id):
            command_result = await handle_private_admin_command(db, moderation, message, text)
            if command_result:
                return

        if await db.is_admin(user.id) and is_username_query(text):
            await message.answer(await db.get_status_by_username(text))
            return

        if text == settings.unban_phrase:
            unbanned = await moderation.unban_and_register(user.id, user.username)
            if unbanned:
                await message.answer("Разбанил и зарегистрировал.")
            else:
                await message.answer("Я тебя не банил.")

        await notify_admins(db, message)

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

    if command not in {"ban", "unban", "reg", "unreg"}:
        return False

    if not is_username_query(argument):
        await message.answer(
            "Нужно так: ban @username, unban @username, reg @username или unreg @username"
        )
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
        "help - показать список команд\n"
        "ban @username - забанить во всех модерируемых чатах\n"
        "unban @username - разбанить и зарегистрировать\n"
        "reg @username - зарегистрировать\n"
        "unreg @username - убрать регистрацию\n"
        "@username - проверить статус"
    )


def is_username_query(text: str) -> bool:
    username = text.removeprefix("@")
    return bool(username) and len(username) <= 32 and username.replace("_", "").isalnum()


async def notify_admins(db: Database, message: Message) -> None:
    if message.from_user is None:
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
            await message.bot.send_message(admin_id, notification)
        except Exception:
            logger.exception("failed to notify admin %s", admin_id)
