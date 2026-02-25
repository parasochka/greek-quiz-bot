"""
Stub out external dependencies before any test imports bot.py.
"""
import os
import json
import sys
from unittest.mock import MagicMock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ.setdefault("TELEGRAM_TOKEN", "123456789:test-token")
os.environ.setdefault("GOOGLE_SHEET_ID", "test-sheet-id")
os.environ.setdefault("GOOGLE_CREDS_JSON", json.dumps({
    "type": "service_account",
    "project_id": "test",
    "private_key_id": "test-key-id",
    "private_key": "test-private-key",
    "client_email": "test@test.iam.gserviceaccount.com",
    "client_id": "123456",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}))

# Stub heavy libraries so tests never touch real network / credentials
for mod in ["gspread", "gspread.exceptions", "google", "google.oauth2",
            "google.oauth2.service_account", "anthropic",
            "telegram", "telegram.ext"]:
    sys.modules.setdefault(mod, MagicMock())
