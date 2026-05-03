import asyncio
import contextlib
import html
import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

from guard_bot.config import Settings
from guard_bot.db import Database

logger = logging.getLogger(__name__)

REGISTERED_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
}


class ModerationService:
    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings

    async def is_registered(self, chat_id: int, user_id: int) -> bool:
        if await self.db.is_registered(user_id):
            return True

        username = await self.db.get_username(user_id)
        if await self.db.is_registered_username(username):
            await self.register_user(user_id, "admin_username_reg", username)
            return True

        if await self._is_chat_member(chat_id, user_id):
            await self.register_user(user_id, "chat_member")
            return True

        if await self._is_channel_subscriber(user_id):
            await self.register_user(user_id, "channel_subscriber")
            return True

        return False

    async def _is_chat_member(self, chat_id: int, user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError as error:
            logger.debug("failed to get chat member %s in %s: %s", user_id, chat_id, error)
            return False
        return member.status in REGISTERED_STATUSES

    async def _is_channel_subscriber(self, user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(self.settings.required_channel, user_id)
        except TelegramAPIError as error:
            logger.debug(
                "failed to get channel subscriber %s in %s: %s",
                user_id,
                self.settings.required_channel,
                error,
            )
            return False
        return member.status in REGISTERED_STATUSES

    async def register_by_challenge(
        self,
        chat_id: int,
        user_id: int,
        answer_message_id: int,
        username: str | None = None,
    ) -> None:
        challenges = await self.db.get_user_challenges(chat_id, user_id)
        await self.register_user(user_id, "challenge", username)

        for challenge in challenges:
            await self._delete_message(chat_id, int(challenge["challenge_message_id"]))
        await self._delete_message(chat_id, answer_message_id)
        await self.db.delete_user_challenges(chat_id, user_id)

    async def register_user(
        self,
        user_id: int,
        source: str,
        username: str | None = None,
    ) -> None:
        was_registered = await self.db.is_registered(user_id)
        await self.db.register_user(user_id, source)
        if not was_registered:
            await self.notify_admins_user_registered(user_id, username)

    async def unregister_user(self, user_id: int, username: str | None = None) -> bool:
        was_registered = await self.db.unregister_user(user_id)
        was_username_registered = await self.db.unregister_username(username) if username else False
        if was_registered or was_username_registered:
            await self.notify_admins_user_unregistered(user_id, username)
        return was_registered or was_username_registered

    async def register_username(self, username: str, source: str) -> None:
        was_registered = await self.db.is_registered_username(username)
        await self.db.register_username(username, source)
        if not was_registered:
            await self.notify_admins(f"зареган @{self.db.normalize_username(username)}")

    async def unregister_username(self, username: str) -> bool:
        was_registered = await self.db.unregister_username(username)
        if was_registered:
            await self.notify_admins(f"разреган @{self.db.normalize_username(username)}")
        return was_registered

    async def create_challenge(self, chat_id: int, user_id: int, original_message_id: int) -> int:
        escaped_phrase = html.escape(self.settings.challenge_phrase)
        message = await self.bot.send_message(
            chat_id,
            (
                "А не бот-ли ты часом? Ответь мне сообщением - "
                f"<code>{escaped_phrase}</code>, у тебя 1 минута. "
                "Чтобы не попадать под проверку в следующий раз, подпишись на канал."
            ),
            reply_to_message_id=original_message_id,
            parse_mode="HTML",
        )
        challenge_id = await self.db.add_challenge(
            chat_id=chat_id,
            user_id=user_id,
            original_message_id=original_message_id,
            challenge_message_id=message.message_id,
        )
        asyncio.create_task(self.expire_challenge(challenge_id))
        return challenge_id

    async def expire_challenge(self, challenge_id: int) -> None:
        await asyncio.sleep(self.settings.challenge_timeout_seconds)
        challenge = await self.db.get_challenge(challenge_id)
        if challenge is None:
            return

        user_id = int(challenge["user_id"])
        chat_id = int(challenge["chat_id"])
        if await self.db.is_registered(user_id):
            await self.db.delete_challenge(challenge_id)
            return

        await self._delete_message(chat_id, int(challenge["original_message_id"]))
        await self._delete_message(chat_id, int(challenge["challenge_message_id"]))
        await self.ban_user(chat_id, user_id, "challenge_timeout")
        await self.db.delete_challenge(challenge_id)

    async def ban_user(
        self,
        chat_id: int,
        user_id: int,
        reason: str,
        username: str | None = None,
    ) -> None:
        try:
            await self.bot.ban_chat_member(chat_id, user_id)
            await self.db.mark_banned(user_id, chat_id, reason)
            await self.notify_admins_user_banned(user_id, username)
        except TelegramAPIError:
            logger.exception("failed to ban user %s in chat %s", user_id, chat_id)

    async def ban_user_everywhere(
        self,
        user_id: int,
        reason: str,
        username: str | None = None,
    ) -> int:
        banned = 0
        for chat_id in self.settings.moderated_chat_ids:
            if await self.ban_user_in_chat(chat_id, user_id, reason):
                banned += 1
        if banned:
            await self.notify_admins_user_banned(user_id, username)
        return banned

    async def ban_user_in_chat(self, chat_id: int, user_id: int, reason: str) -> bool:
        try:
            await self.bot.ban_chat_member(chat_id, user_id)
            await self.db.mark_banned(user_id, chat_id, reason)
            return True
        except TelegramAPIError:
            logger.exception("failed to ban user %s in chat %s", user_id, chat_id)
            return False

    async def notify_admins_user_banned(self, user_id: int, username: str | None = None) -> None:
        user_label = await self.get_user_label(user_id, username)
        admin_ids = await self.db.get_admin_ids()
        for admin_id in admin_ids:
            try:
                await self.bot.send_message(admin_id, f"забанен {user_label}")
            except TelegramAPIError:
                logger.exception("failed to notify admin %s about ban", admin_id)

    async def notify_admins_user_registered(
        self,
        user_id: int,
        username: str | None = None,
    ) -> None:
        await self.notify_admins(f"зареган {await self.get_user_label(user_id, username)}")

    async def notify_admins_user_unregistered(
        self,
        user_id: int,
        username: str | None = None,
    ) -> None:
        await self.notify_admins(f"разреган {await self.get_user_label(user_id, username)}")

    async def notify_admins(self, text: str) -> None:
        admin_ids = await self.db.get_admin_ids()
        for admin_id in admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                logger.exception("failed to notify admin %s", admin_id)

    async def get_user_label(self, user_id: int, username: str | None = None) -> str:
        username = username or await self.db.get_username(user_id)
        return f"@{username}" if username else str(user_id)

    async def unban_and_register(self, user_id: int, username: str | None = None) -> int:
        known_bans = await self.db.get_user_bans(user_id)
        chat_ids = {int(ban["chat_id"]) for ban in known_bans}
        if not chat_ids:
            return 0

        unbanned = 0
        for chat_id in chat_ids:
            try:
                await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                await self.db.clear_user_ban(user_id, chat_id)
                unbanned += 1
            except TelegramAPIError:
                logger.exception("failed to unban user %s in chat %s", user_id, chat_id)

        await self.db.clear_user_bans(user_id)
        if unbanned:
            await self.register_user(user_id, "private_unban_phrase", username)
        return unbanned

    async def delete_known_user_messages(self, chat_id: int, user_id: int) -> None:
        challenges = await self.db.get_user_challenges(chat_id, user_id)
        for challenge in challenges:
            await self._delete_message(chat_id, int(challenge["original_message_id"]))
            await self._delete_message(chat_id, int(challenge["challenge_message_id"]))
        await self.db.delete_user_challenges(chat_id, user_id)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self._delete_message(chat_id, message_id)

    async def _delete_message(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(TelegramAPIError):
            await self.bot.delete_message(chat_id, message_id)
