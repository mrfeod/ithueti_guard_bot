from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = Field(alias="BOT_TOKEN")
    admin_secret: str = Field(alias="ADMIN_SECRET")
    required_channel: str = Field(default="@ithueti", alias="REQUIRED_CHANNEL")
    moderated_chat_ids_raw: str = Field(default="", alias="MODERATED_CHAT_IDS")
    database_path: str = Field(default="guard_bot.sqlite3", alias="DATABASE_PATH")
    challenge_timeout_seconds: int = Field(default=60, alias="CHALLENGE_TIMEOUT_SECONDS")
    challenge_image_path: str = Field(default="/state/cptch.png", alias="CHALLENGE_IMAGE_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @cached_property
    def moderated_chat_ids(self) -> list[int]:
        return [int(item.strip()) for item in self.moderated_chat_ids_raw.split(",") if item.strip()]

    @cached_property
    def moderated_chat_id_set(self) -> set[int]:
        return set(self.moderated_chat_ids)
