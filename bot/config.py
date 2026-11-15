"""Bot configuration loaded from the .env file (see .env.example)."""
from __future__ import annotations

from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_ids(raw: str) -> frozenset[int]:
    result: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            result.add(int(part))
    return frozenset(result)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")
    whitelist_ids_raw: str = Field(default="", alias="WHITELIST_IDS")
    support_contact: str = Field(default="", alias="SUPPORT_CONTACT")

    # --- Locale / time ---
    timezone: str = Field(default="Europe/Moscow", alias="TIMEZONE")
    default_language: str = Field(default="ru", alias="DEFAULT_LANGUAGE")

    # --- Paths ---
    db_path: str = Field(default="data/bot.sqlite3", alias="DB_PATH")
    backup_dir: str = Field(default="backups", alias="BACKUP_DIR")
    log_dir: str = Field(default="logs", alias="LOG_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Orders ---
    order_ttl_hours: int = Field(default=24, alias="ORDER_TTL_HOURS", ge=1)
    review_remind_after_hours: float = Field(default=3, alias="REVIEW_REMIND_AFTER_HOURS", gt=0)
    review_remind_interval_hours: float = Field(default=3, alias="REVIEW_REMIND_INTERVAL_HOURS", gt=0)
    max_open_orders_per_user: int = Field(default=3, alias="MAX_OPEN_ORDERS_PER_USER", ge=1)
    max_proof_file_mb: int = Field(default=10, alias="MAX_PROOF_FILE_MB", ge=1, le=20)

    # --- Subscriptions / access ---
    invite_link_ttl_hours: int = Field(default=24, alias="INVITE_LINK_TTL_HOURS", ge=1)
    grace_days: int = Field(default=0, alias="GRACE_DAYS", ge=0)
    remind_first_days: int = Field(default=3, alias="REMIND_FIRST_DAYS", ge=1)
    remind_second_days: int = Field(default=1, alias="REMIND_SECOND_DAYS", ge=1)
    auto_remove_unpaid: bool = Field(default=False, alias="AUTO_REMOVE_UNPAID")
    reconcile_hour: int = Field(default=10, alias="RECONCILE_HOUR", ge=0, le=23)

    # --- Referrals ---
    ref_bonus_days: int = Field(default=7, alias="REF_BONUS_DAYS", ge=0)
    ref_discount_percent: int = Field(default=10, alias="REF_DISCOUNT_PERCENT", ge=0, le=90)
    ref_max_rewards_per_month: int = Field(default=20, alias="REF_MAX_REWARDS_PER_MONTH", ge=0)

    # --- Backups ---
    backup_hour: int = Field(default=4, alias="BACKUP_HOUR", ge=0, le=23)
    backup_keep: int = Field(default=14, alias="BACKUP_KEEP", ge=1)
    backup_send_to_admin: bool = Field(default=False, alias="BACKUP_SEND_TO_ADMIN")

    # --- Anti-flood / broadcast ---
    flood_max_messages: int = Field(default=8, alias="FLOOD_MAX_MESSAGES", ge=1)
    flood_window_seconds: int = Field(default=10, alias="FLOOD_WINDOW_SECONDS", ge=1)
    flood_max_files: int = Field(default=5, alias="FLOOD_MAX_FILES", ge=1)
    broadcast_rate_per_second: float = Field(default=20, alias="BROADCAST_RATE_PER_SECOND", gt=0, le=30)

    @field_validator("default_language")
    @classmethod
    def _lang(cls, v: str) -> str:
        v = v.strip().lower()
        return v if v in ("ru", "en") else "ru"

    @cached_property
    def admin_ids(self) -> frozenset[int]:
        return _parse_ids(self.admin_ids_raw)

    @cached_property
    def whitelist_ids(self) -> frozenset[int]:
        return _parse_ids(self.whitelist_ids_raw)

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _abs(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def db_file(self) -> Path:
        return self._abs(self.db_path)

    @property
    def backup_path(self) -> Path:
        return self._abs(self.backup_dir)

    @property
    def log_path(self) -> Path:
        return self._abs(self.log_dir)

    @property
    def heartbeat_file(self) -> Path:
        return self.db_file.parent / "heartbeat"


settings = Settings()
