import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from guard_bot.config import Settings
from guard_bot.db import Database
from guard_bot.handlers import create_router
from guard_bot.moderation import ModerationService


async def run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Database(settings.database_path)
    await db.connect()
    await db.migrate()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    moderation = ModerationService(bot, db, settings)
    dispatcher.include_router(create_router(db, moderation, settings))

    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await db.close()


def main() -> None:
    asyncio.run(run())
