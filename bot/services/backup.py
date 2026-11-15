"""Safe SQLite backups using the sqlite3 backup API (consistent even while the bot writes)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from bot.config import settings
from bot.utils.timeutil import to_local, utcnow

log = logging.getLogger(__name__)


def _backup_sync(db_file: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    src = sqlite3.connect(str(db_file))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            dst.execute("PRAGMA integrity_check")
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)


def rotate(backup_dir: Path, keep: int) -> list[Path]:
    files = sorted(backup_dir.glob("bot-*.sqlite3"))
    removed = []
    for old in files[:-keep] if keep > 0 else []:
        try:
            old.unlink()
            removed.append(old)
        except OSError as exc:
            log.warning("Cannot delete old backup %s: %s", old, exc)
    return removed


async def make_backup(db_file: Path | None = None, backup_dir: Path | None = None, keep: int | None = None) -> Path:
    db_file = db_file or settings.db_file
    backup_dir = backup_dir or settings.backup_path
    stamp = to_local(utcnow(), settings.tz).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"bot-{stamp}.sqlite3"
    await asyncio.to_thread(_backup_sync, db_file, target)
    rotate(backup_dir, keep if keep is not None else settings.backup_keep)
    log.info("Backup created: %s", target.name)
    return target
