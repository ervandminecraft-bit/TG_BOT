import re
import string
from pathlib import Path

from bot.i18n import detect_language, t
from bot.i18n.en import TEXTS as EN
from bot.i18n.ru import TEXTS as RU
from bot.services.backup import make_backup
from bot.utils.money import fmt_amount, parse_amount
from bot.utils.text import normalize_proof_text

ROOT = Path(__file__).resolve().parent.parent


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_i18n_same_keys_and_placeholders():
    assert set(RU) == set(EN), set(RU) ^ set(EN)
    for key in RU:
        assert _placeholders(RU[key]) == _placeholders(EN[key]), key


def test_every_key_used_in_code_exists():
    """Scan the code for string literals passed to t()/ask()/retry() and check they exist."""
    code = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "bot").rglob("*.py") if "i18n" not in p.parts)
    literal = re.compile(r"""(?:\bt\(\s*[^,()]+,\s*|ask\([^)]*?,\s*"[a-z_]+",\s*|retry\(ctx,\s*|notify_admins_t\(\s*bot,\s*session,\s*|header_key=|key\s*=\s*)"([a-z][a-z0-9_]+)\"""")
    keys = set(literal.findall(code))
    dynamic = {
        "status_": ["awaiting_payment", "pending_review", "approved", "rejected", "cancelled", "expired"],
        "rej_tpl_": ["not_found", "wrong_amount", "unreadable", "wrong_code"],
        "stats_period_": ["day", "week", "month"],
        "bc_seg_": ["all", "active", "expired"],
        "promo_kind_btn_": ["percent", "fixed", "none"],
    }
    for prefix, suffixes in dynamic.items():
        keys |= {prefix + s for s in suffixes}
    keys |= {"promo_not_found", "promo_expired", "promo_exhausted", "promo_already_used",
             "order_unavailable", "order_too_many_open", "ch_err_not_found", "ch_err_not_admin",
             "ch_err_no_invite", "ch_err_no_restrict", "ref_reward_applied_sub", "ref_reward_applied_balance",
             "remind_first", "remind_second", "adm_grant_done", "adm_grant_done_link_failed"}
    missing = sorted(k for k in keys if k not in RU)
    assert len(keys) > 150
    assert not missing, missing


def test_t_formats_and_falls_back():
    assert "5" in t("en", "duration_days", days=5)
    assert t("xx", "btn_back") == RU["btn_back"]
    assert t("ru", "no_such_key") == "no_such_key"


def test_detect_language():
    assert detect_language("ru") == "ru"
    assert detect_language("uk-UA") == "ru"
    assert detect_language("en-US") == "en"
    assert detect_language("de") == "en"
    assert detect_language(None, "ru") == "ru"


def test_money():
    assert parse_amount("1490") == 149000
    assert parse_amount("9,99") == 999
    assert parse_amount("abc") is None
    assert parse_amount("-1") is None
    assert fmt_amount(149000, "RUB") == "1 490 RUB"
    assert fmt_amount(999, "USD") == "9.99 USD"


def test_proof_normalization():
    assert normalize_proof_text("Op: 1234-5678") == normalize_proof_text("op12345678")
    assert normalize_proof_text("abc") is None  # too short to compare


async def test_backup_and_rotation(sm, tmp_path):
    db = tmp_path / "test.sqlite3"
    out = tmp_path / "backups"
    for n in range(4):
        (out).mkdir(exist_ok=True)
        (out / f"bot-2020010{n}-000000.sqlite3").write_bytes(b"old")
    path = await make_backup(db, out, keep=3)
    assert path.exists() and path.stat().st_size > 0
    files = sorted(out.glob("bot-*.sqlite3"))
    assert len(files) == 3 and path in files

    import sqlite3

    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert {"orders", "subscriptions", "users"} <= tables
