"""Time helpers. The database stores naive UTC datetimes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=UTC).astimezone(tz)


def fmt_dt(dt: datetime | None, tz: ZoneInfo, with_time: bool = True) -> str:
    if dt is None:
        return "—"
    local = to_local(dt, tz)
    return local.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def local_today_start_utc(now: datetime, tz: ZoneInfo) -> datetime:
    """Start of the current local day, expressed in naive UTC."""
    local = to_local(now, tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC).replace(tzinfo=None)


def days_left(expires_at: datetime, now: datetime) -> int:
    """Whole days left, rounded up (0 if already passed)."""
    seconds = (expires_at - now).total_seconds()
    if seconds <= 0:
        return 0
    return int((seconds + 86399) // 86400)


def unix(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp())


__all__ = ["utcnow", "to_local", "fmt_dt", "local_today_start_utc", "days_left", "unix", "timedelta"]
