import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_modules_import_with_required_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")

    for module_name in ("config", "topics", "bot"):
        sys.modules.pop(module_name, None)
        importlib.import_module(module_name)

    import config

    assert config.DATABASE_URL.startswith("postgresql://")
