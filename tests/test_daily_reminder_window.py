import importlib
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo


def _load_bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    sys.modules.pop("config", None)
    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


def test_daily_reminder_time_is_deterministic(monkeypatch):
    bot = _load_bot(monkeypatch)

    d = date(2026, 1, 20)
    first = bot._daily_reminder_local_time(12345, d)
    second = bot._daily_reminder_local_time(12345, d)

    assert first == second


def test_daily_reminder_time_falls_within_17_to_20(monkeypatch):
    bot = _load_bot(monkeypatch)

    d = date(2026, 1, 20)
    _, hour, minute = bot._daily_reminder_local_time(54321, d)

    assert 17 <= hour < 20
    assert 0 <= minute < 60


def test_reminder_is_sent_within_grace_window(monkeypatch):
    bot = _load_bot(monkeypatch)
    tz = ZoneInfo("Europe/Athens")
    due_at = datetime(2026, 1, 20, 18, 10, tzinfo=tz)

    assert bot._is_reminder_send_time(datetime(2026, 1, 20, 18, 12, tzinfo=tz), due_at)
    assert bot._is_reminder_send_time(datetime(2026, 1, 20, 18, 30, tzinfo=tz), due_at)


def test_reminder_is_not_sent_too_early_or_too_late(monkeypatch):
    bot = _load_bot(monkeypatch)
    tz = ZoneInfo("Europe/Athens")
    due_at = datetime(2026, 1, 20, 19, 0, tzinfo=tz)

    assert not bot._is_reminder_send_time(datetime(2026, 1, 20, 18, 59, tzinfo=tz), due_at)
    assert not bot._is_reminder_send_time(datetime(2026, 1, 20, 19, 21, tzinfo=tz), due_at)
