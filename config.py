import os
from telegram import InlineKeyboardButton


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"ERROR: Required environment variable '{name}' is not set.\n"
            f"Add it to your deployment settings (Railway → Variables)."
        )
    return value


TG_TOKEN = require_env("TELEGRAM_TOKEN")
DATABASE_URL = require_env("DATABASE_URL").replace("postgres://", "postgresql://", 1)
OPENAI_KEY = require_env("OPENAI_API_KEY")

OPENAI_REQUEST_TIMEOUT_SEC = float(os.environ.get("OPENAI_REQUEST_TIMEOUT_SEC", "45"))
QUIZ_GENERATION_TIMEOUT_SEC = float(os.environ.get("QUIZ_GENERATION_TIMEOUT_SEC", "120"))
OPENAI_MAX_ATTEMPTS = int(os.environ.get("OPENAI_MAX_ATTEMPTS", "3"))
OPENAI_TEMPERATURE = float(os.environ.get("OPENAI_TEMPERATURE", "0.55"))
PAUSED_SESSION_TTL_HOURS = int(os.environ.get("PAUSED_SESSION_TTL_HOURS", "24"))

QUIZ_QUESTION_COUNT = 20

LETTERS = ["А", "Б", "В", "Г"]

OWNER_USERNAME = "aparasochka"
ALLOWED_USERNAMES = {OWNER_USERNAME, "immangosteen", "holycolorama", "akulovv", "xaaru"}
TRIBUTE_URL = os.environ.get("TRIBUTE_URL", "https://t.me/tribute")

STATE_ONBOARDING = "onboarding"
STATE_SETTINGS_EDIT = "settings_edit"

ONBOARDING_STEPS = [
    {"key": "display_name", "q": "Как тебя называть?",                              "type": "text"},
    {"key": "age",          "q": "Сколько тебе лет?",                               "type": "text"},
    {"key": "city",         "q": "В каком городе проживаешь?",                      "type": "text"},
    {"key": "native_lang",  "q": "Твой родной язык:",                               "type": "choice",
     "options": ["Русский", "Украинский", "Другой"]},
    {"key": "other_langs",  "q": "Другие языки кроме родного:",                     "type": "choice",
     "options": ["Английский (хорошо)", "Английский (базовый)", "Нет других"]},
    {"key": "occupation",   "q": "Чем занимаешься? (работа, учёба)",                "type": "text"},
    {"key": "family",       "q": "Семья - дети, партнёр? (или напиши «нет»)",       "type": "text"},
    {"key": "hobbies",      "q": "Хобби и интересы:",                               "type": "text"},
    {"key": "greek_goal",   "q": "Где планируешь применять греческий? (например: кафе, соседи, работа)", "type": "text"},
    {"key": "exam_date",    "q": "Есть дата экзамена? (ДД.ММ.ГГГГ или «нет»)",     "type": "text"},
]

WELCOME_TEXT = (
    "👋 Привет! Я помогу тебе учить греческий язык (уровень A2).\n\n"
    "🤖 <b>Как это работает:</b>\n"
    "• Квизы из 20 вопросов - сколько хочешь в день\n"
    "• Все вопросы генерирует AI на основе твоего профиля\n"
    "• Первые 3 дня - знакомство с твоим уровнем\n"
    "• С 4-го дня - умная адаптация: слабые темы чаще, сильные реже\n"
    "• После каждого ответа - объяснение правила\n\n"
    "💶 <b>Стоимость:</b> первые 3 дня бесплатно, затем <b>10 € в месяц</b>.\n"
    "Подписка через Tribute покрывает AI-токены для генерации вопросов.\n\n"
    "⚠️ <i>Вопросы созданы искусственным интеллектом - возможны неточности.</i>\n\n"
    "Чтобы начать, расскажи немного о себе - займёт 2 минуты."
)

MAIN_MENU_KEYBOARD = [
    [InlineKeyboardButton("🎯 Начать квиз", callback_data="menu_quiz")],
    [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_stats")],
    [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
    [InlineKeyboardButton("ℹ️ О боте", callback_data="menu_about")],
]
