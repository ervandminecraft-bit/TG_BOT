"""Localization. All user-visible texts live in ru.py / en.py, not in the code.

Usage: t(lang, "key", name="value"). Texts use Telegram HTML markup;
values passed in must already be escaped with bot.utils.text.esc() if they come from users.
"""
from __future__ import annotations

import logging

from bot.i18n.en import TEXTS as EN
from bot.i18n.ru import TEXTS as RU

log = logging.getLogger(__name__)

LANGUAGES: dict[str, dict[str, str]] = {"ru": RU, "en": EN}
RU_LIKE = {"ru", "uk", "be", "kk", "uz", "ky", "tg", "hy", "az", "ka"}


def detect_language(language_code: str | None, default: str = "ru") -> str:
    if not language_code:
        return default
    code = language_code.split("-")[0].lower()
    if code == "en":
        return "en"
    if code in RU_LIKE:
        return "ru"
    return "en"


def t(lang: str | None, key: str, **kwargs: object) -> str:
    table = LANGUAGES.get(lang or "ru", RU)
    template = table.get(key) or RU.get(key)
    if template is None:
        log.error("Missing i18n key: %s", key)
        return key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            log.exception("Bad i18n formatting for key %s", key)
            return template
    return template
