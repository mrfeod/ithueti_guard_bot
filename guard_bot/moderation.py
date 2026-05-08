import asyncio
import contextlib
import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

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
        if await self.db.is_admin(user_id) or await self.db.is_moderator(user_id):
            return True

        if await self.db.is_registered(user_id):
            return True

        username = await self.db.get_username(user_id)
        if await self.db.is_moderator_username(username):
            await self.db.add_moderator(user_id, username)
            await self.db.remove_moderator_username(username or "")
            await self.notify_admins(f"модератор @{self.db.normalize_username(username)}")
            return True

        if await self.db.is_registered_username(username):
            await self.register_user(user_id, "admin_username_reg", username)
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

    async def is_channel_subscriber(self, user_id: int) -> bool:
        return await self._is_channel_subscriber(user_id)

    async def register_by_challenge(
        self,
        chat_id: int,
        user_id: int,
        answer_message_id: int,
        username: str | None = None,
    ) -> None:
        challenges = await self.db.get_user_challenges(chat_id, user_id)

        for challenge in challenges:
            await self._delete_message(chat_id, int(challenge["challenge_message_id"]))
        await self._delete_message(chat_id, answer_message_id)
        await self.db.delete_user_challenges(chat_id, user_id)
        await self.register_user(user_id, "challenge", username)

    async def register_user(
        self,
        user_id: int,
        source: str,
        username: str | None = None,
    ) -> None:
        was_registered = await self.db.is_registered(user_id)
        await self.db.register_user(user_id, source)
        if not was_registered:
            logger.info(
                "user registered: user_id=%s username=%r source=%s",
                user_id,
                username,
                source,
            )
            await self.notify_admins_user_registered(user_id, username)

    async def unregister_user(self, user_id: int, username: str | None = None) -> bool:
        was_registered = await self.db.unregister_user(user_id)
        was_username_registered = await self.db.unregister_username(username) if username else False
        if was_registered or was_username_registered:
            logger.info("user unregistered: user_id=%s username=%r", user_id, username)
            await self.notify_admins_user_unregistered(user_id, username)
        return was_registered or was_username_registered

    async def register_username(self, username: str, source: str) -> None:
        was_registered = await self.db.is_registered_username(username)
        await self.db.register_username(username, source)
        if not was_registered:
            logger.info("username registered: username=%r source=%s", username, source)
            await self.notify_admins(f"зареган @{self.db.normalize_username(username)}")

    async def unregister_username(self, username: str) -> bool:
        was_registered = await self.db.unregister_username(username)
        if was_registered:
            logger.info("username unregistered: username=%r", username)
            await self.notify_admins(f"разреган @{self.db.normalize_username(username)}")
        return was_registered

    async def create_challenge(
        self,
        chat_id: int,
        user_id: int,
        original_message_id: int | None,
        reply_to_message_id: int | None = None,
        delete_original: bool = True,
    ) -> int:
        existing_challenges = await self.db.get_user_challenges(chat_id, user_id)
        if existing_challenges:
            first_challenge = existing_challenges[0]
            if delete_original and original_message_id is not None:
                await self.db.add_challenge_original_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    original_message_id=original_message_id,
                    challenge_message_id=int(first_challenge["challenge_message_id"]),
                )
            return int(first_challenge["id"])

        message = await self.bot.send_photo(
            chat_id,
            FSInputFile(self.settings.challenge_image_path),
            reply_to_message_id=reply_to_message_id or original_message_id,
        )
        challenge_id = await self.db.add_challenge(
            chat_id=chat_id,
            user_id=user_id,
            original_message_id=original_message_id if delete_original and original_message_id else 0,
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

        challenges = await self.db.get_user_challenges(chat_id, user_id)
        for pending_challenge in challenges:
            original_message_id = int(pending_challenge["original_message_id"])
            if original_message_id:
                await self._delete_message(chat_id, original_message_id)
            await self._delete_message(chat_id, int(pending_challenge["challenge_message_id"]))
        await self.ban_user(chat_id, user_id, "challenge_timeout")
        await self.db.delete_user_challenges(chat_id, user_id)

    async def ban_user(
        self,
        chat_id: int,
        user_id: int,
        reason: str,
        username: str | None = None,
    ) -> None:
        try:
            if user_id < 0:
                await self.bot.ban_chat_sender_chat(chat_id, user_id)
            else:
                await self.bot.ban_chat_member(chat_id, user_id)
            await self.db.mark_banned(user_id, chat_id, reason)
            logger.info(
                "user banned: user_id=%s chat_id=%s reason=%s",
                user_id,
                chat_id,
                reason,
            )
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

    async def remove_user_everywhere(
        self,
        user_id: int,
        reason: str,
        username: str | None = None,
    ) -> int:
        removed = await self.ban_user_everywhere(user_id, reason, username)
        if await self.ban_user_in_required_channel(user_id, reason):
            removed += 1
            user_label = await self.get_user_label(user_id, username)
            await self.notify_admins(f"забанен в канале {user_label}")
        return removed

    async def ban_user_in_chat(self, chat_id: int, user_id: int, reason: str) -> bool:
        try:
            if user_id < 0:
                await self.bot.ban_chat_sender_chat(chat_id, user_id)
            else:
                await self.bot.ban_chat_member(chat_id, user_id)
            await self.db.mark_banned(user_id, chat_id, reason)
            logger.info(
                "user banned: user_id=%s chat_id=%s reason=%s",
                user_id,
                chat_id,
                reason,
            )
            return True
        except TelegramAPIError:
            logger.exception("failed to ban user %s in chat %s", user_id, chat_id)
            return False

    async def ban_user_in_required_channel(self, user_id: int, reason: str) -> bool:
        if user_id < 0:
            return False

        try:
            await self.bot.ban_chat_member(self.settings.required_channel, user_id)
            logger.info(
                "user banned in required channel: user_id=%s channel=%s reason=%s",
                user_id,
                self.settings.required_channel,
                reason,
            )
            return True
        except TelegramAPIError:
            logger.exception(
                "failed to ban user %s in channel %s",
                user_id,
                self.settings.required_channel,
            )
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
        if username:
            return f"@{username}"
        return await self.db.get_display_name(user_id) or str(user_id)

    async def unban_and_register(self, user_id: int, username: str | None = None) -> int:
        known_bans = await self.db.get_user_bans(user_id)
        chat_ids = {int(ban["chat_id"]) for ban in known_bans}
        if not chat_ids:
            return 0

        unbanned = 0
        for chat_id in chat_ids:
            try:
                if user_id < 0:
                    await self.bot.unban_chat_sender_chat(chat_id, user_id)
                else:
                    await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                await self.db.clear_user_ban(user_id, chat_id)
                logger.info("user unbanned: user_id=%s chat_id=%s", user_id, chat_id)
                unbanned += 1
            except TelegramAPIError:
                logger.exception("failed to unban user %s in chat %s", user_id, chat_id)

        await self.db.clear_user_bans(user_id)
        if unbanned:
            await self.notify_admins(f"разбанен {await self.get_user_label(user_id, username)}")
            await self.register_user(user_id, "private_unban_message", username)
        return unbanned

    async def unban_user_everywhere_and_register(
        self,
        user_id: int,
        username: str | None = None,
    ) -> int:
        unbanned = 0
        for chat_id in self.settings.moderated_chat_ids:
            if await self.unban_user_in_chat(chat_id, user_id):
                unbanned += 1

        if await self.unban_user_in_required_channel(user_id):
            unbanned += 1

        await self.db.clear_user_bans(user_id)
        if unbanned:
            await self.notify_admins(f"разбанен {await self.get_user_label(user_id, username)}")
            await self.register_user(user_id, "admin_private_unban", username)
        return unbanned

    async def unban_user_in_chat(self, chat_id: int, user_id: int) -> bool:
        try:
            if user_id < 0:
                await self.bot.unban_chat_sender_chat(chat_id, user_id)
            else:
                await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            await self.db.clear_user_ban(user_id, chat_id)
            logger.info("user unbanned: user_id=%s chat_id=%s", user_id, chat_id)
            return True
        except TelegramAPIError:
            logger.exception("failed to unban user %s in chat %s", user_id, chat_id)
            return False

    async def unban_user_in_required_channel(self, user_id: int) -> bool:
        if user_id < 0:
            return False

        try:
            await self.bot.unban_chat_member(
                self.settings.required_channel,
                user_id,
                only_if_banned=True,
            )
            logger.info(
                "user unbanned in required channel: user_id=%s channel=%s",
                user_id,
                self.settings.required_channel,
            )
            return True
        except TelegramAPIError:
            logger.exception(
                "failed to unban user %s in channel %s",
                user_id,
                self.settings.required_channel,
            )
            return False

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
