import importlib
import sys


def _load_quiz_generation(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    sys.modules.pop("config", None)
    sys.modules.pop("quiz_generation", None)
    return importlib.import_module("quiz_generation")


def test_collect_question_errors_detects_duplicates_without_name_error(monkeypatch):
    quiz_generation = _load_quiz_generation(monkeypatch)

    questions = [
        {
            "question": "q",
            "options": ["Ναι!", "Ναι", "Όχι", "Ίσως"],
            "correctIndex": 0,
            "explanation": "e",
            "topic": "Глаголы",
            "type": "ru_to_gr",
        }
    ]

    errors = quiz_generation._collect_question_errors(questions)

    assert 0 in errors
    assert "duplicate options detected" in errors[0]
