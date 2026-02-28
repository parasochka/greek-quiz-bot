import importlib
import sys
from datetime import date


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
