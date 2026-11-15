from __future__ import annotations

import html
import re
import secrets

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def random_code(length: int = 5) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_proof_text(text: str | None) -> str | None:
    """Normalize an operation id / hash for duplicate detection."""
    if not text:
        return None
    norm = re.sub(r"[\s\-_:.#]", "", text).lower()
    return norm[:255] if len(norm) >= 6 else None


def user_display(first_name: str | None, last_name: str | None, username: str | None, user_id: int) -> str:
    name = " ".join(p for p in (first_name, last_name) if p) or "—"
    uname = f"@{username}" if username else "—"
    return f"{esc(name)} ({esc(uname)}, ID <code>{user_id}</code>)"
