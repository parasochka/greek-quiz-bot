import os
import signal
import json
import html
import random
import asyncio
import difflib
import contextlib
import unicodedata
import asyncpg
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest, Conflict
from openai import AsyncOpenAI

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"ERROR: Required environment variable '{name}' is not set.\n"
            f"Add it to your deployment settings (Railway → Variables)."
        )
    return value

TG_TOKEN = _require_env("TELEGRAM_TOKEN")
DATABASE_URL = _require_env("DATABASE_URL").replace("postgres://", "postgresql://", 1)

# The bot now uses only ChatGPT API for quiz generation.
OPENAI_KEY = _require_env("OPENAI_API_KEY")
OPENAI_REQUEST_TIMEOUT_SEC = float(os.environ.get("OPENAI_REQUEST_TIMEOUT_SEC", "45"))
QUIZ_GENERATION_TIMEOUT_SEC = float(os.environ.get("QUIZ_GENERATION_TIMEOUT_SEC", "120"))
OPENAI_MAX_ATTEMPTS = int(os.environ.get("OPENAI_MAX_ATTEMPTS", "3"))
OPENAI_TEMPERATURE = float(os.environ.get("OPENAI_TEMPERATURE", "0.55"))

# How long a paused (in-progress) quiz session is kept in the DB before expiry.
# Configurable via env var PAUSED_SESSION_TTL_HOURS (default: 24 hours).
PAUSED_SESSION_TTL_HOURS = int(os.environ.get("PAUSED_SESSION_TTL_HOURS", "24"))

db_pool = None
QUIZ_QUESTION_COUNT = 20


@contextlib.asynccontextmanager
async def _acquire():
    async with db_pool.acquire() as conn:
        yield conn


LETTERS = ["А", "Б", "В", "Г"]

OWNER_USERNAME = "aparasochka"
ALLOWED_USERNAMES = {OWNER_USERNAME, "immangosteen", "holycolorama", "akulovv", "xaaru"}
TRIBUTE_URL = os.environ.get("TRIBUTE_URL", "https://t.me/tribute")


def is_access_allowed(user) -> bool:
    """Currently only allowed users have access. Future: check subscription_status."""
    return user.username in ALLOWED_USERNAMES


# ─── Onboarding ───────────────────────────────────────────────────────────────

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
    [InlineKeyboardButton("🎯 Начать квиз",    callback_data="menu_quiz")],
    [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_stats")],
    [InlineKeyboardButton("⚙️ Настройки",      callback_data="menu_settings")],
    [InlineKeyboardButton("ℹ️ О боте",          callback_data="menu_about")],
]

# Canonical topic names — used to detect unseen topics and enforce consistent Stats keys.
# ChatGPT is instructed to use EXACTLY these strings in the "topic" field of each question.
MASTER_TOPICS = [
    "Глаголы",
    "Прошедшее время",
    "Будущее время",
    "Отрицание",
    "Местоимения",
    "Артикли",
    "Существительные",
    "Прилагательные",
    "Указательные местоимения",
    "Числа",
    "Вопросительные слова",
    "Предлоги и союзы",
    "Бытовые ситуации",
    "Время и дата",
    "Семья",
    "Части тела",
    "Погода",
    "Дом и квартира",
    "Еда и продукты",
    "Одежда",
    "Наречия",
]


def normalize_topic(topic: str) -> str:
    """Map API-returned topic to the nearest canonical MASTER_TOPICS name.

    The model may occasionally mix in visually similar Greek characters (e.g. ο, ι, Και)
    inside otherwise-Cyrillic topic names. difflib finds the closest match so
    statistics are always recorded under the correct canonical key.
    """
    if topic in MASTER_TOPICS:
        return topic
    matches = difflib.get_close_matches(topic, MASTER_TOPICS, n=1, cutoff=0.6)
    return matches[0] if matches else topic


def h(text):
    return html.escape(str(text))

# ─── Database (Railway PostgreSQL) ─────────────────────────────────────────────
#
# FOUR tables:
#   users        — registered Telegram users
#   quiz_sessions — one row per completed quiz
#   answers      — raw audit log: topic, type, correct per question
#   topic_stats  — per-topic all-time aggregates (upserted after each quiz)
#
# build_prompt() uses topic_stats + quiz_sessions only → token cost is O(topics).
# answers is kept for /stats display and future analysis.

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username    VARCHAR(255),
                first_name  VARCHAR(255),
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                onboarding_complete BOOLEAN DEFAULT FALSE,
                subscription_status VARCHAR(20) DEFAULT 'free',
                subscription_expires_at TIMESTAMPTZ
            )
        """)
        # Add new columns to existing table (idempotent for existing deployments)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_complete BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20) DEFAULT 'free'")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMPTZ")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id       BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                display_name  VARCHAR(100),
                age           INT,
                city          VARCHAR(100),
                native_lang   VARCHAR(100),
                other_langs   VARCHAR(200),
                occupation    TEXT,
                family_status TEXT,
                hobbies       TEXT,
                greek_goal    TEXT,
                exam_date     DATE,
                updated_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quiz_sessions (
                id              SERIAL PRIMARY KEY,
                user_id         BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                session_date    DATE NOT NULL,
                completed_at    TIMESTAMPTZ DEFAULT NOW(),
                correct_answers INT,
                total_questions INT DEFAULT 20
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS answers (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                session_id    INT REFERENCES quiz_sessions(id) ON DELETE CASCADE,
                answered_at   TIMESTAMPTZ DEFAULT NOW(),
                topic         VARCHAR(100) NOT NULL,
                question_type VARCHAR(20)  NOT NULL,
                correct       BOOLEAN      NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS topic_stats (
                user_id   BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                topic     VARCHAR(100) NOT NULL,
                correct   INT  DEFAULT 0,
                total     INT  DEFAULT 0,
                last_seen DATE,
                PRIMARY KEY (user_id, topic)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS paused_sessions (
                user_id       BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                questions     JSONB NOT NULL,
                current_idx   INT NOT NULL DEFAULT 0,
                answers       JSONB NOT NULL DEFAULT '[]',
                session_dates JSONB NOT NULL DEFAULT '[]',
                updated_at    TIMESTAMPTZ DEFAULT NOW(),
                expires_at    TIMESTAMPTZ NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS topic_memory (
                user_id      BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                topic        VARCHAR(100) NOT NULL,
                mastery      DOUBLE PRECISION NOT NULL DEFAULT 0.25,
                stability    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                due_at       DATE NOT NULL DEFAULT CURRENT_DATE,
                last_seen    DATE,
                review_count INT NOT NULL DEFAULT 0,
                lapses       INT NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, topic)
            )
        """)


async def register_user(user):
    async with _acquire() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, username, first_name) "
            "VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
            user.id, user.username, user.first_name,
        )


async def _is_onboarding_complete(user_id: int) -> bool:
    async with _acquire() as conn:
        val = await conn.fetchval(
            "SELECT onboarding_complete FROM users WHERE telegram_id = $1", user_id,
        )
    return bool(val)


async def _load_profile(user_id: int):
    """Load user profile as dict, or None if not found."""
    async with _acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_profiles WHERE user_id = $1", user_id,
        )
    return dict(row) if row else None


async def _save_profile(user_id: int, data: dict):
    """Save profile from onboarding data dict and mark onboarding complete."""
    age = None
    if data.get("age"):
        try:
            age = int(data["age"])
        except ValueError:
            pass

    exam_date = None
    if data.get("exam_date"):
        s = data["exam_date"].strip().lower()
        if s not in ("нет", "no", "-", ""):
            for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
                try:
                    exam_date = datetime.strptime(s, fmt).date()
                    break
                except ValueError:
                    pass

    async with _acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO user_profiles "
                "(user_id, display_name, age, city, native_lang, other_langs, "
                " occupation, family_status, hobbies, greek_goal, exam_date, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                " display_name=EXCLUDED.display_name, age=EXCLUDED.age, city=EXCLUDED.city, "
                " native_lang=EXCLUDED.native_lang, other_langs=EXCLUDED.other_langs, "
                " occupation=EXCLUDED.occupation, family_status=EXCLUDED.family_status, "
                " hobbies=EXCLUDED.hobbies, greek_goal=EXCLUDED.greek_goal, "
                " exam_date=EXCLUDED.exam_date, updated_at=NOW()",
                user_id,
                data.get("display_name"),
                age,
                data.get("city"),
                data.get("native_lang"),
                data.get("other_langs"),
                data.get("occupation"),
                data.get("family"),
                data.get("hobbies"),
                data.get("greek_goal"),
                exam_date,
            )
            await conn.execute(
                "UPDATE users SET onboarding_complete = TRUE WHERE telegram_id = $1",
                user_id,
            )


async def _update_profile_field(user_id: int, field: str, value: str):
    """Update a single profile field."""
    col_map = {
        "display_name": "display_name", "age": "age", "city": "city",
        "native_lang": "native_lang", "other_langs": "other_langs",
        "occupation": "occupation", "family": "family_status",
        "hobbies": "hobbies", "greek_goal": "greek_goal", "exam_date": "exam_date",
    }
    col = col_map.get(field)
    if not col:
        return

    if col == "age":
        try:
            val = int(value)
        except ValueError:
            val = None
    elif col == "exam_date":
        s = value.strip().lower()
        val = None
        if s not in ("нет", "no", "-", ""):
            for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
                try:
                    val = datetime.strptime(s, fmt).date()
                    break
                except ValueError:
                    pass
    else:
        val = value

    async with _acquire() as conn:
        await conn.execute(
            f"UPDATE user_profiles SET {col} = $1, updated_at = NOW() WHERE user_id = $2",
            val, user_id,
        )


async def _reset_profile(user_id: int):
    """Delete profile and reset onboarding flag."""
    async with _acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM user_profiles WHERE user_id = $1", user_id)
            await conn.execute(
                "UPDATE users SET onboarding_complete = FALSE WHERE telegram_id = $1",
                user_id,
            )


async def _load_compact_data(user_id: int):
    """
    Load topic_stats + session dates — compact, fast, fixed size regardless of history length.
    Returns:
      stats        — {topic: {correct, total, last_seen}}
      session_dates — sorted list of YYYY-MM-DD strings
    """
    async with _acquire() as conn:
        stats_rows = await conn.fetch(
            "SELECT topic, correct, total, last_seen FROM topic_stats WHERE user_id=$1",
            user_id,
        )
        date_rows = await conn.fetch(
            "SELECT DISTINCT session_date FROM quiz_sessions "
            "WHERE user_id=$1 ORDER BY session_date",
            user_id,
        )
    stats = {
        r["topic"]: {
            "correct":   r["correct"],
            "total":     r["total"],
            "last_seen": str(r["last_seen"]) if r["last_seen"] else "",
        }
        for r in stats_rows
    }
    session_dates = [str(r["session_date"]) for r in date_rows]
    return stats, session_dates


async def _load_topic_memory(user_id: int) -> dict:
    """Load per-topic spaced-repetition state."""
    async with _acquire() as conn:
        rows = await conn.fetch(
            "SELECT topic, mastery, stability, due_at, last_seen, review_count, lapses "
            "FROM topic_memory WHERE user_id=$1",
            user_id,
        )
    return {
        r["topic"]: {
            "mastery": float(r["mastery"]),
            "stability": float(r["stability"]),
            "due_at": str(r["due_at"]) if r["due_at"] else None,
            "last_seen": str(r["last_seen"]) if r["last_seen"] else None,
            "review_count": int(r["review_count"]),
            "lapses": int(r["lapses"]),
        }
        for r in rows
    }


def build_topic_sequence(stats: dict, session_dates: list, topic_memory: dict, total_questions: int = QUIZ_QUESTION_COUNT) -> list[str]:
    """Server-side topic scheduler for spaced repetition (returns exact per-slot topics)."""
    today = date.today()
    learning_mode = len(session_dates) < 3

    def acc(topic: str) -> float:
        s = stats.get(topic, {"correct": 0, "total": 0})
        return (s["correct"] / s["total"]) if s.get("total") else 0.0

    def overdue_days(topic: str) -> int:
        mem = topic_memory.get(topic) or {}
        due_s = mem.get("due_at")
        if not due_s:
            return 0
        try:
            due = date.fromisoformat(due_s)
        except ValueError:
            return 0
        return max((today - due).days, 0)

    def last_seen_days(topic: str) -> int:
        mem = topic_memory.get(topic) or {}
        last_s = mem.get("last_seen")
        if not last_s:
            s = stats.get(topic, {})
            last_s = s.get("last_seen")
        if not last_s:
            return 999
        try:
            last = date.fromisoformat(last_s)
        except ValueError:
            return 999
        return (today - last).days

    seen_topics = {t for t, s in stats.items() if s.get("total", 0) > 0}
    unseen_topics = [t for t in MASTER_TOPICS if t not in seen_topics]
    weak_topics = [t for t in MASTER_TOPICS if t in seen_topics and acc(t) < 0.60]
    medium_topics = [t for t in MASTER_TOPICS if t in seen_topics and 0.60 <= acc(t) < 0.85]
    strong_topics = [t for t in MASTER_TOPICS if t in seen_topics and acc(t) >= 0.85]

    def sort_pool(pool: list[str], weakest_first: bool) -> list[str]:
        return sorted(
            pool,
            key=lambda t: (
                -overdue_days(t),
                acc(t) if weakest_first else -acc(t),
                -last_seen_days(t),
            ),
        )

    sequence = []

    def fill_from_pool(pool: list[str], n: int, weakest_first: bool = True):
        if n <= 0 or not pool:
            return
        ordered = sort_pool(pool, weakest_first=weakest_first)
        i = 0
        while len(sequence) < total_questions and n > 0 and ordered:
            sequence.append(ordered[i % len(ordered)])
            i += 1
            n -= 1

    if learning_mode:
        fill_from_pool(unseen_topics, min(5, total_questions), weakest_first=True)
        fill_from_pool([t for t in MASTER_TOPICS if t not in unseen_topics],
                       total_questions - len(sequence), weakest_first=True)
    else:
        quotas = {
            "weak": round(total_questions * 0.35),
            "medium": round(total_questions * 0.25),
            "strong": round(total_questions * 0.10),
            "unseen": total_questions - round(total_questions * 0.35) - round(total_questions * 0.25) - round(total_questions * 0.10),
        }
        fill_from_pool(weak_topics, quotas["weak"], weakest_first=True)
        fill_from_pool(medium_topics, quotas["medium"], weakest_first=True)
        fill_from_pool(strong_topics, quotas["strong"], weakest_first=False)
        fill_from_pool(unseen_topics, quotas["unseen"], weakest_first=True)

        if len(sequence) < total_questions:
            fill_from_pool(MASTER_TOPICS, total_questions - len(sequence), weakest_first=True)

    if len(sequence) < total_questions:
        fill_from_pool(MASTER_TOPICS, total_questions - len(sequence), weakest_first=True)

    random.shuffle(sequence)
    return sequence[:total_questions]


async def _update_topic_memory_for_answer(conn, user_id: int, topic: str, correct: bool) -> None:
    """Update spaced-repetition state for one topic attempt."""
    row = await conn.fetchrow(
        "SELECT mastery, stability, review_count, lapses FROM topic_memory "
        "WHERE user_id=$1 AND topic=$2",
        user_id, topic,
    )
    if row:
        mastery = float(row["mastery"])
        stability = float(row["stability"])
        review_count = int(row["review_count"])
        lapses = int(row["lapses"])
    else:
        mastery = 0.25
        stability = 1.0
        review_count = 0
        lapses = 0

    if correct:
        mastery = min(1.0, mastery + 0.08)
        stability = min(45.0, max(1.0, stability * 1.4))
    else:
        mastery = max(0.0, mastery - 0.12)
        stability = max(1.0, stability * 0.6)
        lapses += 1

    review_count += 1
    due_at = date.today() + timedelta(days=max(1, round(stability)))

    await conn.execute(
        """
        INSERT INTO topic_memory (user_id, topic, mastery, stability, due_at, last_seen, review_count, lapses)
        VALUES ($1, $2, $3, $4, $5, CURRENT_DATE, $6, $7)
        ON CONFLICT (user_id, topic) DO UPDATE SET
            mastery = EXCLUDED.mastery,
            stability = EXCLUDED.stability,
            due_at = EXCLUDED.due_at,
            last_seen = CURRENT_DATE,
            review_count = EXCLUDED.review_count,
            lapses = EXCLUDED.lapses
        """,
        user_id, topic, mastery, stability, due_at, review_count, lapses,
    )


async def _load_history_for_stats(user_id: int):
    """Load full answers only for /stats display (infrequent). Not used on quiz start."""
    try:
        async with _acquire() as conn:
            rows = await conn.fetch(
                "SELECT topic, question_type AS type, correct FROM answers WHERE user_id=$1",
                user_id,
            )
        return [{"topic": r["topic"], "type": r["type"], "correct": r["correct"]} for r in rows]
    except Exception as e:
        print(f"Load history error: {e}")
        return []


async def _save_all(user_id: int, answers: list):
    """
    Persist one quiz session atomically:
      1. Insert a quiz_sessions row
      2. Bulk-insert raw answer rows
      3. Upsert topic_stats (increment correct/total, update last_seen)
    """
    upsert_sql = (
        "INSERT INTO topic_stats (user_id, topic, correct, total, last_seen) "
        "VALUES ($1, $2, $3, 1, CURRENT_DATE) "
        "ON CONFLICT (user_id, topic) DO UPDATE SET "
        "  correct   = topic_stats.correct + $3, "
        "  total     = topic_stats.total + 1, "
        "  last_seen = CURRENT_DATE"
    )
    async with _acquire() as conn:
        async with conn.transaction():
            correct_count = sum(1 for a in answers if a["correct"])
            session_id = await conn.fetchval(
                "INSERT INTO quiz_sessions (user_id, session_date, correct_answers, total_questions) "
                "VALUES ($1, CURRENT_DATE, $2, $3) RETURNING id",
                user_id, correct_count, len(answers),
            )
            await conn.executemany(
                "INSERT INTO answers (user_id, session_id, topic, question_type, correct) "
                "VALUES ($1, $2, $3, $4, $5)",
                [(user_id, session_id, a["topic"], a["type"], a["correct"]) for a in answers],
            )
            for a in answers:
                await conn.execute(
                    upsert_sql,
                    user_id, a["topic"], 1 if a["correct"] else 0,
                )
                await _update_topic_memory_for_answer(
                    conn, user_id, a["topic"], a["correct"],
                )


async def _clear_all(user_id: int):
    """
    Wipe answers, quiz_sessions, topic_stats, and any paused session for this user.
    Returns number of answers deleted.
    """
    async with _acquire() as conn:
        async with conn.transaction():
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM answers WHERE user_id=$1", user_id,
            )
            await conn.execute("DELETE FROM answers WHERE user_id=$1", user_id)
            await conn.execute("DELETE FROM quiz_sessions WHERE user_id=$1", user_id)
            await conn.execute("DELETE FROM topic_stats WHERE user_id=$1", user_id)
            await conn.execute("DELETE FROM topic_memory WHERE user_id=$1", user_id)
            await conn.execute("DELETE FROM paused_sessions WHERE user_id=$1", user_id)
            return count


async def save_result(user_id: int, answers: list):
    await _save_all(user_id, answers)


async def clear_history(user_id: int):
    return await _clear_all(user_id)


# ─── Paused-session persistence (cross-device / bot-restart resume) ─────────────

async def _save_paused_session(user_id: int, session: dict) -> None:
    """Upsert the current in-progress quiz state to the DB so it survives restarts
    and can be resumed from any device."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=PAUSED_SESSION_TTL_HOURS)
    async with _acquire() as conn:
        await conn.execute(
            """
            INSERT INTO paused_sessions (user_id, questions, current_idx, answers, session_dates, updated_at, expires_at)
            VALUES ($1, $2::jsonb, $3, $4::jsonb, $5::jsonb, NOW(), $6)
            ON CONFLICT (user_id) DO UPDATE SET
                questions     = EXCLUDED.questions,
                current_idx   = EXCLUDED.current_idx,
                answers       = EXCLUDED.answers,
                session_dates = EXCLUDED.session_dates,
                updated_at    = NOW(),
                expires_at    = EXCLUDED.expires_at
            """,
            user_id,
            json.dumps(session["questions"]),
            session["current"],
            json.dumps(session["answers"]),
            json.dumps(session["session_dates"]),
            expires_at,
        )


async def _load_paused_session(user_id: int) -> dict | None:
    """Return the paused session dict if one exists and has not expired, else None."""
    async with _acquire() as conn:
        row = await conn.fetchrow(
            "SELECT questions, current_idx, answers, session_dates "
            "FROM paused_sessions "
            "WHERE user_id = $1 AND expires_at > NOW()",
            user_id,
        )
    if not row:
        return None
    return {
        "questions":     json.loads(row["questions"]),
        "current":       row["current_idx"],
        "answers":       json.loads(row["answers"]),
        "awaiting":      True,
        "session_dates": json.loads(row["session_dates"]),
    }


async def _delete_paused_session(user_id: int) -> None:
    """Remove the paused session row after the quiz is completed or abandoned."""
    async with _acquire() as conn:
        await conn.execute("DELETE FROM paused_sessions WHERE user_id = $1", user_id)


# ─── Stats helpers ─────────────────────────────────────────────────────────────

def calc_streak(session_dates):
    """session_dates: sorted list of YYYY-MM-DD strings."""
    if not session_dates:
        return 0, 0

    parsed_dates = [date.fromisoformat(d) for d in session_dates]
    best = cur = 1
    for i in range(1, len(parsed_dates)):
        diff = (parsed_dates[i] - parsed_dates[i - 1]).days
        if diff == 1:
            cur += 1
            best = max(best, cur)
        elif diff > 1:
            cur = 1

    diff = (date.today() - parsed_dates[-1]).days
    current = cur if diff <= 1 else 0
    return current, best

def days_since_last_session(session_dates):
    if not session_dates:
        return 99
    return (date.today() - date.fromisoformat(session_dates[-1])).days

def type_stats_all(history):
    """Per question-type accuracy from full history (used only in /stats display)."""
    stats = {}
    for r in history:
        qt = r.get("type", "")
        if not qt:
            continue
        stats.setdefault(qt, {"correct": 0, "total": 0})
        stats[qt]["total"] += 1
        if r.get("correct"):
            stats[qt]["correct"] += 1
    return stats

# ─── AI prompt ─────────────────────────────────────────────────────────────────

PROMPT_STATIC = """КРИТИЧЕСКИ ВАЖНО:
- Только стандартный современный греческий язык (νέα ελληνική γλώσσα).
- Никакого кипрского диалекта, кипрских слов, кипрского произношения.
- Ученик не использует греческую клавиатуру. Все вопросы только с вариантами ответа, без ввода текста.
- КАЖДЫЙ вопрос обязан быть встроен в мини-ситуацию из жизни ученика. Используй данные профиля выше (город, работу, хобби, семью, интересы) для создания персональных ситуаций. Текст вопроса начинай с короткого сценария (1-2 предложения), потом задавай языковую задачу.
  Плохо: «Как сказать по-гречески: "31 декабря"?»
  Хорошо: «Ты договариваешься с коллегой о корпоративе. Как сказать: "Вечеринка будет 31 декабря"?»
  Плохо: «Вставь артикль: ___ γυναίκα είναι όμορφη.»
  Хорошо: «Ты рассказываешь соседу о жене. Вставь нужный артикль: "___ γυναίκα μου είναι πολύ ωραία."»
- Разнообразь собеседников и места действия: сосед, коллега, врач, продавец, турист, незнакомец на улице, официант, кассир, ребёнок. Места: кафе, рынок, автобус, поликлиника, магазин, пляж, аптека, офис, лифт. Чередуй эмоциональные контексты: просьба, удивление, проблема, срочность, радость, неловкость. Не повторяй один и тот же сценарий дважды в одном квизе.
- АНТИПОВТОРЫ МЕЖДУ ДНЯМИ: не копируй дословно вопросы из прошлых квизов. Даже при той же теме меняй сюжет, роли, детали, формулировку и целевую фразу. Избегай шаблонов вида «в магазине купи хлеб» из квиза в квиз.
- Перед генерацией составь внутренний «план разнообразия» на 20 вопросов: минимум 10 разных микросценариев, минимум 8 разных ролей собеседника, минимум 8 разных локаций. План не выводи в ответ, используй только для самопроверки.
- Персонализация ОБЯЗАТЕЛЬНА: каждый вопрос должен опираться на профиль ученика (город, работа/занятие, семья, хобби, цели). Если профильная деталь уже встречалась, используй другой аспект профиля или новый ракурс этой детали.

Приоритеты при подборе тем для ЭТОГО квиза:
- 🔴 Темы ниже 60% → 35% вопросов (слабые места, приоритет)
- 🟡 Темы 60-85%   → 25% вопросов (закрепление)
- 🟢 Темы выше 85% → 10% вопросов (поддержание)
- ⚪ Темы без практики (0 вопросов) → 30% вопросов (новый материал)
  • Вводи 2-4 новые темы за квиз, не больше — не перегружай ученика
  • Выбирай новые темы, которые логично сочетаются с текущим квизом
- Темы с большим числом дней с последней практики — включать чаще

КРИТИЧЕСКИ ВАЖНО — поле "topic":
Используй ТОЛЬКО эти точные названия тем (скопируй строку целиком, без изменений):
Глаголы, Прошедшее время, Будущее время, Отрицание, Местоимения, Артикли, Существительные, Прилагательные, Указательные местоимения, Числа, Вопросительные слова, Предлоги и союзы, Бытовые ситуации, Время и дата, Семья, Части тела, Погода, Дом и квартира, Еда и продукты, Одежда, Наречия
ЗАПРЕЩЕНО: использовать греческие буквы внутри названия темы. Названия тем — строго кириллица, точно как в списке выше.
Тема = то, что ПРОВЕРЯЕТСЯ в вопросе, а не то, что упомянуто в качестве примера или контекста.
Если вопрос проверяет предлог перед названием дня — тема "Предлоги и союзы", не "Время и дата".
Если вопрос проверяет падеж существительного на примере еды — тема "Существительные", не "Еда и продукты".

Полный перечень тем (все темы должны встречаться со временем):
ГЛАГОЛЫ: είμαι, έχω, θέλω, κάνω, πάω, μπορώ, ξέρω, βλέπω, τρώω, πίνω, μιλάω, λέω, μένω, δουλεύω, αγοράζω, πληρώνω, παίρνω, δίνω, ανοίγω, κλείνω, αρχίζω, τελειώνω
  → проверяет: выбор нужного глагола по смыслу, спряжение в настоящем времени
ПРОШЕДШЕЕ ВРЕМЯ: αόριστος — πήγα, είπα, έκανα, ήθελα, είχα, ήμουν, αγόρασα, πλήρωσα, πήρα, είδα и др.
  → проверяет: форму глагола в прошедшем времени (αόριστος)
БУДУЩЕЕ ВРЕМЯ: θα + глагол — θα πάω, θα κάνω, θα αγοράσω, θα μιλήσω и др.
  → проверяет: форму глагола в будущем времени (θα + глагол)
ОТРИЦАНИЕ: δεν, μην
  → проверяет: выбор δεν или μην в нужном контексте
МЕСТОИМЕНИЯ: личные (εγώ/εσύ/αυτός/αυτή/αυτό/εμείς/εσείς/αυτοί), слабые и сильные формы, притяжательные
  → проверяет: выбор и форму местоимения (личного или притяжательного)
АРТИКЛИ: определенный и неопределенный, все роды, все падежи
  → проверяет: выбор артикля по роду, числу и падежу существительного
СУЩЕСТВИТЕЛЬНЫЕ: три рода, именительный/винительный/родительный падежи, единственное и множественное число
  → проверяет: правильный падеж и число существительного
ПРИЛАГАТЕЛЬНЫЕ: согласование с существительным по роду, числу, падежу
  → проверяет: согласование прилагательного с существительным
УКАЗАТЕЛЬНЫЕ МЕСТОИМЕНИЯ: αυτός/αυτή/αυτό, εκείνος/εκείνη/εκείνο
  → проверяет: выбор и форму указательного местоимения по роду/числу/падежу
ЧИСЛА: от 0 до 1000, изменение по роду (1/3/4), даты, время
  → проверяет: знание числительных и их форм; как назвать цену, количество, номер
ВОПРОСИТЕЛЬНЫЕ СЛОВА: πού, πότε, τι, ποιος, πώς, πόσο, γιατί, από πού
  → проверяет: выбор нужного вопросительного слова по смыслу
ПРЕДЛОГИ И СОЮЗЫ: σε, από, με, για, και, αλλά, ή, γιατί, όταν
  → проверяет: выбор нужного предлога или союза, в том числе перед днями, местами и существительными
БЫТОВЫЕ СИТУАЦИИ: приветствие и знакомство, кафе и ресторан, магазин и рынок, транспорт и направления, врач и аптека, гостиница, почта, банк
  → проверяет: готовые фразы и реплики в конкретных жизненных ситуациях
ВРЕМЯ И ДАТА: дни недели, месяцы, времена года, который час, когда
  → проверяет: знание слов (как называется день/месяц/сезон/время суток); НЕ предлоги или падежи при них
СЕМЬЯ: μαμά, μπαμπάς, παιδί, διδύμια, γυναίκα, άντρας, αδερφός, αδερφή, παππούς, γιαγιά, οικογένεια, παντρεμένος
  → проверяет: словарный запас — названия членов семьи и родственных отношений
ЧАСТИ ТЕЛА: κεφάλι, χέρι, πόδι, στομάχι, πλάτη, μάτι, αυτί, μύτη, στόμα, δόντι, λαιμός
  → проверяет: словарный запас — названия частей тела
ПОГОДА: ήλιος, βροχή, ζέστη, κρύο, αέρας, θερμοκρασία, συννεφιά, θάλασσα, καιρός
  → проверяет: словарный запас — как описать погодные условия
ДОМ/КВАРТИРА: σπίτι, δωμάτιο, κουζίνα, μπάνιο, σαλόνι, μπαλκόνι, ενοίκιο, γείτονας, διαμέρισμα
  → проверяет: словарный запас — названия помещений и бытовых реалий
ЕДА/ПРОДУКТЫ: ψωμί, κρέας, λαχανικά, φρούτα, γάλα, τυρί, ψάρι, νερό, καφές, σούπερ μάρκετ, αγορά
  → проверяет: словарный запас — названия еды и продуктов; НЕ падеж или артикль на примере еды
ОДЕЖДА: ρούχα, παπούτσια, φόρεμα, παντελόνι, μπλούζα, μέγεθος, χρώμα, φοράω
  → проверяет: словарный запас — названия одежды, как сказать что надеть или купить
НАРЕЧИЯ: πάντα, ποτέ, συχνά, μερικές φορές, ήδη, ακόμα, σύντομα, αμέσως, μαζί, μόνος, πολύ, λίγο
  → проверяет: выбор нужного наречия по смыслу и его место в предложении

Типы вопросов — распределяй по смыслу темы, не механически:
1. ru_to_gr — перевод фразы в контексте ситуации: "Ты в кафе, официант ждёт заказ. Как сказать: «Я хочу кофе и воду»?" — 4 варианта на греческом
2. gr_to_ru — понимание греческой реплики из ситуации: "На остановке незнакомец говорит тебе: «Πού είναι η στάση;» — что он спросил?" — 4 варианта на русском
3. choose_form — выбор правильной формы в предложении-ситуации: "Ты говоришь другу, кого видишь у Агоры: «Βλέπω ___ (красивая женщина).»" — 4 варианта с разными формами
4. fill_blank — вставить слово в диалог или фразу из ситуации: "Сосед спрашивает где ты живёшь. Ты отвечаешь: «Εγώ ___ στη Λεμεσό.»" — 4 варианта на греческом

Выбор типа вопроса по теме:
- Темы-словарь (Время и дата, Еда и продукты, Семья, Части тела, Погода, Одежда, Дом и квартира, Бытовые ситуации):
  предпочитай ru_to_gr и gr_to_ru — проверяй знание слов и выражений
- Темы-грамматика (Глаголы, Артикли, Существительные, Прилагательные, Местоимения, Предлоги и союзы, Прошедшее время, Будущее время, Отрицание, Указательные местоимения, Наречия):
  предпочитай choose_form и fill_blank — проверяй правильную форму
- Числа, Вопросительные слова: любые типы, по ситуации
Общий баланс типов по всему квизу — примерно поровну (по ~5 каждого).

Сгенерируй СТРОГО 20 вопросов. Верни ТОЛЬКО валидный JSON без markdown, без пояснений вне JSON.

Каждый объект в массиве:
{
  "question": "текст вопроса на русском языке",
  "options": ["вариант1", "вариант2", "вариант3", "вариант4"],
  "correctIndex": 2,
  "explanation": "пояснение почему этот вариант правильный — полными словами без сокращений, 1-2 предложения на русском",
  "topic": "название темы",
  "type": "ru_to_gr | gr_to_ru | choose_form | fill_blank"
}

Требования к пояснениям:
- Полные слова, без грамматических сокращений (не 'им.п.' а 'именительный падеж').
- Объясни конкретное правило. 1-2 предложения.

Варианты ответа должны быть перемешаны случайным образом — correctIndex указывает реальную позицию правильного варианта.
Неправильные варианты — правдоподобные: похожие формы, близкие слова, частые ошибки.
ОБЯЗАТЕЛЬНО: все 4 варианта в каждом вопросе должны быть разными строками — никаких повторений внутри одного вопроса.
ЖЁСТКОЕ ПРАВИЛО: запрещены варианты-двойники, которые отличаются только точкой, запятой, лишним пробелом или регистром. Если ты сомневаешься — перепиши дистрактор полностью.
ПЛОХО: ["иду домой", "иду домой.", "Иду домой", "иду  домой"] — это повторы.
ХОРОШО: каждый вариант несёт разный смысл или разную грамматическую форму.

СТРУКТУРА JSON НЕИЗМЕННА — независимо от содержания вопросов:
• Верни JSON-объект верхнего уровня с единственным полем "questions".
• В поле "questions" — ровно 20 объектов (не меньше и не больше).
• Каждый объект содержит ровно 6 полей: question, options, correctIndex, explanation, topic, type.
• options — массив ровно из 4 строк.
• correctIndex — целое число: 0, 1, 2 или 3.
• Никакого текста вне JSON-объекта, никаких ```json``` обёрток, никаких комментариев.
• Каждый question, option и explanation должен быть обычной JSON-строкой (без необработанных переносов строк, без служебных комментариев).

Перед отправкой ответа сделай САМОПРОВЕРКУ:
1) JSON верхнего уровня — объект с единственным ключом "questions".
2) В массиве questions ровно 20 объектов.
3) В каждом объекте ровно 6 нужных полей.
4) В каждом объекте options содержит ровно 4 УНИКАЛЬНЫЕ строки.
5) correctIndex указывает именно на правильный вариант.
6) topic входит в фиксированный список, type входит в: ru_to_gr, gr_to_ru, choose_form, fill_blank.
7) Выводишь ТОЛЬКО JSON-объект, без префиксов/суффиксов.
Вся творческая свобода — в тексте: живые и неожиданные ситуации, яркие сценарии, богатые объяснения."""


def build_profile_section(profile: dict) -> str:
    """Build the personal section of the system prompt from user profile data."""
    name = profile.get("display_name") or "Ученик"
    age = profile.get("age")
    city = profile.get("city") or "?"
    native_lang = profile.get("native_lang") or "?"
    other_langs = profile.get("other_langs") or ""
    occupation = profile.get("occupation") or "?"
    family = profile.get("family_status") or "нет данных"
    hobbies = profile.get("hobbies") or "?"
    greek_goal = profile.get("greek_goal") or "изучение греческого"
    exam_date = profile.get("exam_date")

    age_str = f", {age} лет" if age else ""
    other_langs_line = ""
    if other_langs and other_langs.lower() not in ("нет других", "нет", "no"):
        other_langs_line = f" Другие языки: {other_langs}."

    goal_line = f"Место применения: {greek_goal}."
    if exam_date:
        if isinstance(exam_date, date):
            goal_line += f" Сдать экзамен {exam_date.strftime('%d.%m.%Y')}."
        else:
            goal_line += f" Сдать экзамен {exam_date}."

    return (
        f"Ученик: {name}{age_str}, живёт в {city}.\n"
        f"Родной язык: {native_lang}.{other_langs_line}\n"
        f"Работа/занятие: {occupation}.\n"
        f"Семья: {family}.\n"
        f"Хобби: {hobbies}.\n"
        f"{goal_line}"
    )


def build_system_prompt(profile: dict) -> str:
    """Combine intro + personal profile + static quiz rules into the full system prompt."""
    profile_section = build_profile_section(profile)
    return (
        "Ты генератор вопросов для квиза по греческому языку уровня A2.\n\n"
        + profile_section
        + "\n\n"
        + PROMPT_STATIC
    )


def build_dynamic_prompt(stats, session_dates, profile, required_topics=None):
    """
    Returns only the dynamic part of the prompt — per-session stats + conditional notes.

    stats        — {topic: {correct, total, last_seen}}  (from topic_stats, compact)
    session_dates — sorted list of date strings          (from quiz_sessions, compact)

    Dynamic prompt size is O(number_of_topics) — never grows with raw history length.
    """
    # Learning period: first 3 unique quiz days — collect broad statistics before adapting
    learning_days = len(session_dates)
    is_learning = learning_days < 3

    days_away = days_since_last_session(session_dates)
    today = datetime.now().strftime("%Y-%m-%d")

    # Seen topics sorted weakest-first, with recency indicator
    hist_lines = []
    for topic, s in sorted(stats.items(),
                           key=lambda x: x[1]["correct"] / max(x[1]["total"], 1)):
        if s["total"] == 0:
            continue  # listed separately below as unseen
        pct = round(s["correct"] / s["total"] * 100)
        bar = "🔴" if pct < 60 else "🟡" if pct < 85 else "🟢"
        recency = ""
        if s.get("last_seen"):
            ds = (datetime.strptime(today, "%Y-%m-%d") -
                  datetime.strptime(s["last_seen"], "%Y-%m-%d")).days
            recency = f", {ds}д назад" if ds > 0 else ", сегодня"
        hist_lines.append(f"  {bar} {topic}: {pct}% ({s['total']} вопр.{recency})")

    hist_summary = "\n".join(hist_lines) if hist_lines else "  (история пуста — первая сессия)"

    # Unseen topics — explicitly listed so the model knows exactly what hasn't been practiced
    unseen = [t for t in MASTER_TOPICS if t not in stats or stats[t]["total"] == 0]
    if unseen:
        hist_summary += (
            f"\n\n⚪ Темы без практики ({len(unseen)} шт.) — вводи по 2-4 за квиз:\n"
            + "\n".join(f"  ⚪ {t}" for t in unseen)
        )

    learning_note = ""
    if is_learning:
        learning_note = (
            f"РЕЖИМ ОБУЧЕНИЯ (день {learning_days + 1} из 3): статистики пока недостаточно для точной адаптации. "
            f"Игнорируй процентные приоритеты по слабым/сильным темам из системного промпта — они применяются только после 3 дней обучения. "
            f"Равномерно охватывай все темы, вводи 4-5 новых тем за квиз. "
            f"Цель — собрать базовую статистику по максимуму тем.\n"
        )

    review_note = ""
    if not is_learning and days_away >= 2:
        review_note = (
            "ВАЖНО: ученик не занимался более 2 дней. "
            "Первые 8 вопросов строго из уже пройденного материала (повторение). "
            "Только после них переходи к новому.\n"
        )

    exam_date_obj = profile.get("exam_date") if profile else None
    exam_line = ""
    pre_exam_note = ""
    if exam_date_obj:
        if isinstance(exam_date_obj, date):
            days_left = max((datetime.combine(exam_date_obj, datetime.min.time()) - datetime.now()).days, 0)
        else:
            days_left = 0
        if days_left > 0:
            exam_line = f"До экзамена: {days_left} дней.\n"
            if days_left <= 30:
                pre_exam_note = (
                    "ПРЕДЭКЗАМЕНАЦИОННЫЙ РЕЖИМ: из 20 вопросов ровно 6 должны быть в формате "
                    "короткий текст или диалог на греческом (3-5 строк) + вопрос на понимание прочитанного. "
                    "Эти 6 вопросов входят в общий лимит 20, не сверх него.\n"
                )

    variety_hint = random.choice([
        "фокус на бытовые диалоги и быстрые реплики",
        "фокус на мини-истории с неожиданной деталью",
        "фокус на вежливые просьбы и уточняющие вопросы",
        "фокус на живые разговоры с эмоциями",
        "фокус на практические ситуации из повседневной рутины",
    ])

    topic_plan_block = ""
    if required_topics:
        numbered = "\n".join(f"  {i+1}. {topic}" for i, topic in enumerate(required_topics))
        topic_plan_block = (
            "\n\nСЕРВЕРНЫЙ ПЛАН ТЕМ (ОБЯЗАТЕЛЬНО):\n"
            "Для вопроса i (от 1 до 20) поле topic должно быть РОВНО как в строке i ниже.\n"
            f"{numbered}\n"
        )

    return (
        f"{exam_line}"
        f"{learning_note}"
        f"{review_note}"
        f"{pre_exam_note}"
        f"Вариативный фокус этого квиза: {variety_hint}. Используй его как стиль, не нарушая приоритет тем.\n"
        f"Статистика ученика по темам (накопленная за всё время):\n"
        f"{hist_summary}"
        f"{topic_plan_block}"
    )


def _extract_questions(raw: str, provider_name: str, expected_count: int = 20) -> list:
    """Parse AI JSON and return questions array with exact expected_count."""
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Не удалось распарсить ответ {provider_name}: {e}\nСырой ответ: {raw[:300]}")

    if isinstance(parsed, list):
        questions = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
        questions = parsed["questions"]
    else:
        raise ValueError(
            f"{provider_name}: корневой JSON должен быть массивом вопросов "
            "или объектом с полем 'questions'"
        )

    if len(questions) != expected_count:
        raise ValueError(f"{provider_name}: ожидается ровно {expected_count} вопросов, получено {len(questions)}")

    return questions


def _collect_question_errors(questions: list) -> dict:
    """Return {index: reason} for per-question schema/content errors."""
    errors = {}

    def canonicalize_option(option: str) -> str:
        normalized = unicodedata.normalize("NFKD", option)
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        normalized = "".join(ch for ch in normalized if not unicodedata.category(ch).startswith("P"))
        normalized = " ".join(normalized.split())
        return normalized.casefold()

    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            errors[i] = "объект вопроса должен быть JSON-объектом"
            continue
        required = {"question", "options", "correctIndex", "explanation", "topic", "type"}
        if set(q.keys()) != required:
            errors[i] = f"неверные поля: {sorted(q.keys())}"
            continue
        if not isinstance(q["question"], str) or not q["question"].strip():
            errors[i] = "поле 'question' должно быть непустой строкой"
            continue
        if not isinstance(q["explanation"], str) or not q["explanation"].strip():
            errors[i] = "поле 'explanation' должно быть непустой строкой"
            continue
        if q["type"] not in TYPE_LABELS:
            errors[i] = f"недопустимый type={q['type']!r}"
            continue
        opts = q.get("options")
        if not isinstance(opts, list) or len(opts) != 4:
            errors[i] = "options должен содержать ровно 4 варианта"
            continue
        if any((not isinstance(o, str) or not o.strip()) for o in opts):
            errors[i] = "каждый вариант в options должен быть непустой строкой"
            continue
        if not isinstance(q.get("correctIndex"), int) or not (0 <= q.get("correctIndex", -1) < len(opts)):
            errors[i] = f"correctIndex={q.get('correctIndex')} out of range"
            continue

    for i, q in enumerate(questions):
        if i in errors:
            continue
        opts = q["options"]
        canonical_opts = [canonicalize_option(o) for o in opts]
        if len(canonical_opts) != len(set(canonical_opts)):
            errors[i] = f"duplicate options detected: {opts}"

    return errors




def _collect_topic_plan_errors(questions: list, required_topics: list[str] | None) -> dict:
    """Return {index: reason} when question topic does not match server-side required slot."""
    if not required_topics:
        return {}
    errors = {}
    for i, q in enumerate(questions):
        if i >= len(required_topics):
            break
        expected = required_topics[i]
        actual = q.get("topic") if isinstance(q, dict) else None
        if actual != expected:
            errors[i] = f"topic должен быть '{expected}', получено '{actual}'"
    return errors

def _finalize_questions(questions: list) -> list:
    """Normalize topics, enforce validity and shuffle options server-side."""
    errors = _collect_question_errors(questions)
    if errors:
        first_i = min(errors)
        raise ValueError(f"Question {first_i}: {errors[first_i]}")

    # Normalise topic names — guard against mixed Greek/Cyrillic characters
    for q in questions:
        q["topic"] = normalize_topic(q["topic"])

    # Server-side shuffle — correct answer is never stuck at position 0
    for q in questions:
        correct_text = q["options"][q["correctIndex"]]
        random.shuffle(q["options"])
        q["correctIndex"] = q["options"].index(correct_text)

    return questions


async def _repair_questions_openai(client, system_prompt: str, questions: list, invalid: dict) -> list:
    """Regenerate only invalid question slots and return same-length replacement list."""
    bad_payload = [
        {
            "index": idx,
            "reason": reason,
            "original": questions[idx],
        }
        for idx, reason in sorted(invalid.items())
    ]
    n = len(bad_payload)
    repair_prompt = (
        "Исправь только проблемные вопросы. Верни ТОЛЬКО JSON-объект с полем questions, "
        f"в котором ровно {n} новых валидных вопросов в том же порядке, что и список ниже. "
        "Для каждого вопроса нужно 4 уникальных варианта (без дублей по регистру/пробелам/пунктуации), "
        "пустые строки запрещены, correctIndex должен соответствовать правильному варианту. "
        "Проблемные вопросы:\n"
        f"{json.dumps(bad_payload, ensure_ascii=False)}"
    )

    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        max_tokens=1800,
        temperature=OPENAI_TEMPERATURE,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "quiz_question_repairs",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["questions"],
                    "properties": {
                        "questions": {
                            "type": "array",
                            "minItems": n,
                            "maxItems": n,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["question", "options", "correctIndex", "explanation", "topic", "type"],
                                "properties": {
                                    "question": {"type": "string"},
                                    "options": {
                                        "type": "array",
                                        "minItems": 4,
                                        "maxItems": 4,
                                        "items": {"type": "string"},
                                    },
                                    "correctIndex": {"type": "integer", "minimum": 0, "maximum": 3},
                                    "explanation": {"type": "string"},
                                    "topic": {"type": "string", "enum": MASTER_TOPICS},
                                    "type": {"type": "string", "enum": list(TYPE_LABELS.keys())},
                                },
                            },
                        },
                    },
                },
            },
        },
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": repair_prompt},
        ],
    )
    raw = (response.choices[0].message.content or "").strip()
    return _extract_questions(raw, "gpt-4.1-mini repair", expected_count=n)


async def _generate_questions_openai(stats, session_dates, profile, required_topics=None):
    import time
    client = AsyncOpenAI(api_key=OPENAI_KEY, timeout=OPENAI_REQUEST_TIMEOUT_SEC)
    system_prompt = build_system_prompt(profile or {})
    dynamic_prompt = build_dynamic_prompt(stats, session_dates, profile or {}, required_topics=required_topics)
    max_attempts = OPENAI_MAX_ATTEMPTS
    retry_hint = ""
    last_error = None

    print("[openai] creating async client …", flush=True)
    for attempt in range(1, max_attempts + 1):
        t0 = time.monotonic()
        user_prompt = f"{dynamic_prompt}{retry_hint}"
        print(
            f"[openai] sending request to gpt-4.1-mini (attempt {attempt}/{max_attempts}, prompt ~{len(user_prompt)} chars) …",
            flush=True,
        )
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            max_tokens=4500,
            temperature=OPENAI_TEMPERATURE,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "quiz_questions",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["questions"],
                        "properties": {
                            "questions": {
                                "type": "array",
                                "minItems": 20,
                                "maxItems": 20,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["question", "options", "correctIndex", "explanation", "topic", "type"],
                                    "properties": {
                                        "question": {"type": "string"},
                                        "options": {
                                            "type": "array",
                                            "minItems": 4,
                                            "maxItems": 4,
                                            "items": {"type": "string"},
                                        },
                                        "correctIndex": {"type": "integer", "minimum": 0, "maximum": 3},
                                        "explanation": {"type": "string"},
                                        "topic": {"type": "string", "enum": MASTER_TOPICS},
                                        "type": {"type": "string", "enum": list(TYPE_LABELS.keys())},
                                    },
                                },
                            },
                        },
                    },
                },
            },
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        elapsed = time.monotonic() - t0
        print(f"[openai] response received in {elapsed:.1f}s", flush=True)
        choice = response.choices[0]
        finish = choice.finish_reason
        raw = (choice.message.content or "").strip()
        print(f"[openai] finish_reason={finish!r}, content length={len(raw)} chars", flush=True)
        if finish == "length":
            raise ValueError("gpt-4.1-mini обрезал ответ по лимиту токенов (finish_reason='length').")
        if not raw:
            last_error = ValueError(f"gpt-4.1-mini вернул пустой ответ (finish_reason={finish!r})")
        else:
            try:
                parsed = _extract_questions(raw, "gpt-4.1-mini")

                # Fast path: repair only broken question slots instead of full-regenerating all 20.
                for repair_round in range(1, 3):
                    errors = _collect_question_errors(parsed)
                    if not errors:
                        break
                    print(
                        f"[openai] attempting targeted repair round {repair_round}: {len(errors)} invalid question(s)",
                        flush=True,
                    )
                    repaired = await _repair_questions_openai(client, system_prompt, parsed, errors)
                    for repl, idx in zip(repaired, sorted(errors)):
                        parsed[idx] = repl

                topic_plan_errors = _collect_topic_plan_errors(parsed, required_topics)
                for repair_round in range(1, 3):
                    if not topic_plan_errors:
                        break
                    print(
                        f"[openai] enforcing server topic plan, repair round {repair_round}: {len(topic_plan_errors)} slot(s)",
                        flush=True,
                    )
                    repaired = await _repair_questions_openai(client, system_prompt, parsed, topic_plan_errors)
                    for repl, idx in zip(repaired, sorted(topic_plan_errors)):
                        repl["topic"] = required_topics[idx]
                        parsed[idx] = repl
                    topic_plan_errors = _collect_topic_plan_errors(parsed, required_topics)

                parsed = _finalize_questions(parsed)
                print(f"[openai] parsed response ok ({len(raw)} chars)", flush=True)
                return parsed
            except ValueError as e:
                last_error = e

        if attempt < max_attempts:
            print(f"[openai] validation failed, retrying: {last_error}", flush=True)
            retry_hint = (
                "\n\nВАЖНО: Исправь предыдущую ошибку валидации и сгенерируй новый JSON с нуля. "
                "Во всех вопросах должны быть ровно 4 уникальных варианта ответа (без дубликатов даже по регистру/пробелам). "
                f"Ошибка предыдущей попытки: {last_error}"
            )

    raise ValueError(f"Не удалось сгенерировать валидный квиз за {max_attempts} попытки. Последняя ошибка: {last_error}")


async def generate_questions(stats, session_dates, profile, required_topics=None):
    return await _generate_questions_openai(stats, session_dates, profile, required_topics=required_topics)

# ─── Session storage ───────────────────────────────────────────────────────────

user_sessions = {}

# ─── Handlers ──────────────────────────────────────────────────────────────────

TYPE_LABELS = {
    "ru_to_gr":    "🇷🇺 → 🇬🇷 Перевод",
    "gr_to_ru":    "🇬🇷 → 🇷🇺 Перевод",
    "choose_form": "📝 Выбор формы",
    "fill_blank":  "✏️ Заполни пропуск",
}

TYPE_NAMES_RU = {
    "ru_to_gr":    "Перевод RU→GR",
    "gr_to_ru":    "Перевод GR→RU",
    "choose_form": "Выбор формы",
    "fill_blank":  "Заполни пропуск",
}

async def _send_onboarding_step(message, step_index, context):
    """Send the next onboarding question to the user."""
    step = ONBOARDING_STEPS[step_index]
    context.user_data["state"] = STATE_ONBOARDING
    context.user_data["step"] = step_index

    num = f"({step_index + 1}/{len(ONBOARDING_STEPS)}) "
    if step["type"] == "choice":
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"onb_{step['key']}_{i}")]
            for i, opt in enumerate(step["options"])
        ]
        await message.reply_text(
            f"📝 {num}{step['q']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await message.reply_text(f"📝 {num}{step['q']}")


async def _finish_onboarding(message, user_id, context):
    """Save collected profile data and show main menu."""
    data = context.user_data.get("onboarding_data", {})
    await _save_profile(user_id, data)
    context.user_data.clear()
    await message.reply_text(
        "✅ Отлично! Анкета заполнена.\n"
        "Теперь квизы будут персональными - вопросы из твоей жизни.\n\n"
        "Можно начинать!",
        reply_markup=InlineKeyboardMarkup(MAIN_MENU_KEYBOARD),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await register_user(update.effective_user)
    context.user_data.clear()

    if await _is_onboarding_complete(update.effective_user.id):
        await update.message.reply_text(
            "📋 Главное меню:",
            reply_markup=InlineKeyboardMarkup(MAIN_MENU_KEYBOARD),
        )
    else:
        keyboard = [[InlineKeyboardButton("📋 Заполнить анкету", callback_data="start_onboarding")]]
        await update.message.reply_text(
            WELCOME_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text(
        "📋 Главное меню:",
        reply_markup=InlineKeyboardMarkup(MAIN_MENU_KEYBOARD),
    )

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    if not await _is_onboarding_complete(update.effective_user.id):
        keyboard = [[InlineKeyboardButton("📋 Заполнить анкету", callback_data="start_onboarding")]]
        await update.message.reply_text(
            "Сначала заполни анкету - без этого квиз не запустится.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    await start_quiz(update.message, update.effective_user.id, username=update.effective_user.username)

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    if query.data == "menu_quiz":
        if not await _is_onboarding_complete(query.from_user.id):
            keyboard = [[InlineKeyboardButton("📋 Заполнить анкету", callback_data="start_onboarding")]]
            await query.message.reply_text(
                "Сначала заполни анкету - без этого квиз не запустится.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        await query.message.reply_text("⏳ Запускаю квиз...")
        await start_quiz(query.message, query.from_user.id, username=query.from_user.username)

    elif query.data == "menu_stats":
        await show_stats(query.message, query.from_user.id)

    elif query.data == "menu_settings":
        await settings_menu(query.message)

    elif query.data == "menu_about":
        await query.message.reply_text(
            "📖 <b>О боте</b>\n\n"
            "Помогает учить современный греческий язык (уровень A2).\n\n"
            "<b>Как работает:</b>\n"
            "• Квизы из 20 вопросов - сколько хочешь в день\n"
            "• Все вопросы генерирует AI на основе твоего профиля\n"
            "• Первые 3 дня - режим обучения: бот охватывает все темы\n"
            "• С 4-го дня - адаптивный режим: слабые темы чаще, сильные реже\n"
            "• После каждого ответа - объяснение правила\n\n"
            "<b>Команды:</b>\n"
            "/quiz - начать квиз\n"
            "/stats - статистика\n"
            "/settings - настройки профиля\n"
            "/menu - главное меню\n\n"
            "⚠️ Вопросы генерирует AI - возможны неточности.\n\n"
            "Автор: @aparasochka",
            parse_mode="HTML",
        )

async def start_quiz(message, user_id, username=None):
    # Restore a paused session if one exists (survives bot restarts and device switches).
    paused = await _load_paused_session(user_id)
    if paused:
        answered = paused["current"]
        total = len(paused["questions"])
        # Owner gets a choice: resume paused session or start fresh (for testing).
        if username == OWNER_USERNAME:
            keyboard = [[
                InlineKeyboardButton("▶️ Продолжить", callback_data="quiz_resume"),
                InlineKeyboardButton("🔄 Начать заново", callback_data="quiz_restart"),
            ]]
            await message.reply_text(
                f"⏸ Есть незавершённый квиз ({answered} из {total} вопросов пройдено).\n\n"
                f"Продолжить или начать новый?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        user_sessions[user_id] = paused
        await message.reply_text(
            f"⏸ Продолжаю незавершённый квиз ({answered} из {total} вопросов пройдено).",
        )
        await send_question(message, user_id)
        return

    await _start_new_quiz(message, user_id)


async def _start_new_quiz(message, user_id):
    """Generate fresh questions and start a new quiz, discarding any paused state."""
    import time
    import traceback
    msg = await message.reply_text("⏳ Готовлю квиз... Это займёт около минуты.")
    try:
        t_start = time.monotonic()
        print(f"[quiz] user={user_id} loading data …", flush=True)
        stats, session_dates = await _load_compact_data(user_id)
        topic_memory = await _load_topic_memory(user_id)
        profile = await _load_profile(user_id) or {}
        required_topics = build_topic_sequence(stats, session_dates, topic_memory, total_questions=QUIZ_QUESTION_COUNT)
        print(f"[quiz] user={user_id} data loaded in {time.monotonic()-t_start:.1f}s, generating questions …", flush=True)

        t_gen = time.monotonic()
        questions = await asyncio.wait_for(
            generate_questions(stats, session_dates, profile, required_topics=required_topics),
            timeout=QUIZ_GENERATION_TIMEOUT_SEC,
        )
        print(f"[quiz] user={user_id} questions generated in {time.monotonic()-t_gen:.1f}s", flush=True)

        session = {
            "questions": questions,
            "current": 0,
            "answers": [],
            "awaiting": True,
            "session_dates": session_dates,
        }
        user_sessions[user_id] = session
        await _save_paused_session(user_id, session)
        await msg.delete()
        await send_question(message, user_id)
    except asyncio.TimeoutError:
        print(f"[quiz] user={user_id} TIMEOUT: OpenAI did not respond in {QUIZ_GENERATION_TIMEOUT_SEC:.0f}s", flush=True)
        try:
            await msg.edit_text(
                f"❌ OpenAI не ответил за {QUIZ_GENERATION_TIMEOUT_SEC:.0f} секунд.\n\n"
                "Попробуй ещё раз через /quiz"
            )
        except Exception:
            pass
    except Exception as e:
        print(f"[quiz] user={user_id} ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        try:
            await msg.edit_text(f"❌ Ошибка: {type(e).__name__}: {e}\n\nПопробуй ещё раз через /quiz")
        except Exception:
            pass

async def send_question(message, user_id):
    session = user_sessions[user_id]
    q = session["questions"][session["current"]]
    num = session["current"] + 1
    total = len(session["questions"])

    type_label = TYPE_LABELS.get(q.get("type", ""), "❓ Вопрос")
    keyboard = [
        [InlineKeyboardButton(f"{LETTERS[i]}. {opt}", callback_data=f"ans_{i}")]
        for i, opt in enumerate(q["options"])
    ]
    await message.reply_text(
        f"<b>Вопрос {num} из {total}</b>  •  {type_label}\n"
        f"📌 <i>Тема: {h(q['topic'])}</i>\n\n"
        f"❓ {h(q['question'])}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_access_allowed(query.from_user):
        await query.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    user_id = query.from_user.id
    data = query.data

    # ── Menu ──
    if data.startswith("menu_"):
        await handle_menu(update, context)
        return

    # ── Onboarding start ──
    if data == "start_onboarding":
        try:
            await query.answer()
        except Exception:
            pass
        context.user_data["state"] = STATE_ONBOARDING
        context.user_data["step"] = 0
        context.user_data["onboarding_data"] = {}
        await _send_onboarding_step(query.message, 0, context)
        return

    # ── Onboarding choice answer ──
    if data.startswith("onb_"):
        try:
            await query.answer()
        except Exception:
            pass

        # Remove "onb_" prefix then split on the LAST underscore so that
        # keys containing underscores (e.g. "native_lang", "other_langs") are
        # parsed correctly.
        remainder = data[4:]  # strip "onb_"
        key, sep, opt_idx_str = remainder.rpartition("_")
        if not sep or not key or not opt_idx_str.isdigit():
            await query.answer("Некорректные данные", show_alert=True)
            return

        step = next((s for s in ONBOARDING_STEPS if s["key"] == key and s.get("type") == "choice"), None)
        if not step:
            await query.answer("Некорректные данные", show_alert=True)
            return

        opt_idx = int(opt_idx_str)
        options = step.get("options") or []
        if not (0 <= opt_idx < len(options)):
            await query.answer("Некорректные данные", show_alert=True)
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        value = options[opt_idx]
        context.user_data.setdefault("onboarding_data", {})[key] = value
        next_step = context.user_data.get("step", 0) + 1
        context.user_data["step"] = next_step
        if next_step >= len(ONBOARDING_STEPS):
            await _finish_onboarding(query.message, user_id, context)
        else:
            await _send_onboarding_step(query.message, next_step, context)
        return

    # ── Settings ──
    if data == "settings_view":
        try:
            await query.answer()
        except Exception:
            pass
        profile = await _load_profile(user_id)
        if not profile:
            await query.message.reply_text("Профиль не заполнен. Нажми /start чтобы пройти анкету.")
        else:
            await query.message.reply_text(
                _format_profile(profile),
                parse_mode="HTML",
            )
        return

    if data == "settings_edit_menu":
        try:
            await query.answer()
        except Exception:
            pass
        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"setedit_{key}")]
            for key, label in PROFILE_FIELD_LABELS.items()
        ]
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="settings_back")])
        await query.message.reply_text(
            "✏️ Выбери поле для редактирования:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("setedit_"):
        try:
            await query.answer()
        except Exception:
            pass
        field = data[len("setedit_"):]
        step = next((s for s in ONBOARDING_STEPS if s["key"] == field), None)
        label = PROFILE_FIELD_LABELS.get(field, field)
        if step and step["type"] == "choice":
            keyboard = [
                [InlineKeyboardButton(opt, callback_data=f"setopt_{field}_{i}")]
                for i, opt in enumerate(step["options"])
            ]
            await query.message.reply_text(
                f"✏️ {label}:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            context.user_data["state"] = STATE_SETTINGS_EDIT
            context.user_data["field"] = field
            await query.message.reply_text(f"✏️ Введи новое значение для «{label}»:")
        return

    if data.startswith("setopt_"):
        try:
            await query.answer()
        except Exception:
            pass

        # Remove "setopt_" prefix then split on the LAST underscore so that
        # fields containing underscores (e.g. "native_lang", "other_langs")
        # are parsed correctly.
        remainder = data[7:]  # strip "setopt_"
        field, sep, opt_idx_str = remainder.rpartition("_")
        if not sep or not field or not opt_idx_str.isdigit():
            await query.answer("Некорректные данные", show_alert=True)
            return

        step = next((s for s in ONBOARDING_STEPS if s["key"] == field and s.get("type") == "choice"), None)
        if not step:
            await query.answer("Некорректные данные", show_alert=True)
            return

        opt_idx = int(opt_idx_str)
        options = step.get("options") or []
        if not (0 <= opt_idx < len(options)):
            await query.answer("Некорректные данные", show_alert=True)
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        value = options[opt_idx]
        await _update_profile_field(user_id, field, value)
        label = PROFILE_FIELD_LABELS.get(field, field)
        await query.message.reply_text(f"✅ Поле «{label}» обновлено: {value}")
        return

    if data == "settings_reset_ask":
        try:
            await query.answer()
        except Exception:
            pass
        keyboard = [[
            InlineKeyboardButton("🗑 Да, сбросить", callback_data="settings_reset_confirm"),
            InlineKeyboardButton("❌ Отмена",        callback_data="settings_back"),
        ]]
        await query.message.reply_text(
            "⚠️ Профиль будет удалён и анкету придётся пройти заново.\nПродолжить?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "settings_reset_confirm":
        try:
            await query.answer()
        except Exception:
            pass
        await query.edit_message_reply_markup(reply_markup=None)
        await _reset_profile(user_id)
        context.user_data.clear()
        await query.message.reply_text(
            "✅ Профиль сброшен. Нажми /start чтобы пройти анкету заново."
        )
        return

    if data == "settings_back":
        try:
            await query.answer()
        except Exception:
            pass
        await query.message.reply_text(
            "📋 Главное меню:",
            reply_markup=InlineKeyboardMarkup(MAIN_MENU_KEYBOARD),
        )
        return

    # ── Reset stats (from settings menu) ──
    if data == "reset_ask":
        try:
            await query.answer()
        except Exception:
            pass
        keyboard = [[
            InlineKeyboardButton("🗑 Да, удалить всё", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Отмена",           callback_data="reset_cancel"),
        ]]
        await query.message.reply_text(
            "⚠️ <b>Сброс статистики</b>\n\n"
            "Это удалит <b>все твои данные</b>:\n"
            "- Все ответы на вопросы\n"
            "- Накопленную статистику по темам\n"
            "- Историю дней и серию\n\n"
            "Продолжить?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    # ── Reset confirmation ──
    if data == "reset_confirm":
        try:
            await query.answer()
        except Exception:
            pass
        await query.edit_message_reply_markup(reply_markup=None)
        try:
            count = await clear_history(user_id)
            await query.message.reply_text(
                f"🗑 <b>История очищена.</b>\n"
                f"Удалено ответов: {count}\n"
                f"Статистика по темам и история сессий также сброшены.\n\n"
                f"Квиз начнёт обучение заново с чистого листа.",
                parse_mode="HTML",
            )
        except Exception as e:
            await query.message.reply_text(
                f"❌ <b>Ошибка при очистке:</b>\n<code>{h(str(e))}</code>",
                parse_mode="HTML",
            )
        return

    if data == "reset_cancel":
        try:
            await query.answer()
        except Exception:
            pass
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Отмена. История не тронута.")
        return

    # ── Owner-only: resume paused quiz ──
    if data == "quiz_resume":
        try:
            await query.answer()
        except Exception:
            pass
        await query.edit_message_reply_markup(reply_markup=None)
        paused = await _load_paused_session(user_id)
        if paused:
            user_sessions[user_id] = paused
            answered = paused["current"]
            total = len(paused["questions"])
            await query.message.reply_text(
                f"⏸ Продолжаю незавершённый квиз ({answered} из {total} вопросов пройдено).",
            )
            await send_question(query.message, user_id)
        else:
            await _start_new_quiz(query.message, user_id)
        return

    # ── Owner-only: discard paused quiz and start fresh ──
    if data == "quiz_restart":
        try:
            await query.answer()
        except Exception:
            pass
        await query.edit_message_reply_markup(reply_markup=None)
        await _delete_paused_session(user_id)
        if user_id in user_sessions:
            del user_sessions[user_id]
        await _start_new_quiz(query.message, user_id)
        return

    # ── Quiz answer ──
    if not data.startswith("ans_"):
        try:
            await query.answer()
        except Exception:
            pass
        return

    if user_id not in user_sessions:
        # Try to restore a paused session from the DB (e.g. after bot restart or from another device).
        paused = await _load_paused_session(user_id)
        if paused:
            user_sessions[user_id] = paused
        else:
            try:
                await query.answer("Сессия истекла. Напиши /quiz чтобы начать заново.")
            except Exception:
                pass
            return

    session = user_sessions[user_id]
    if not session.get("awaiting"):
        try:
            await query.answer()
        except Exception:
            pass
        return

    try:
        selected = int(data.split("_")[1])
    except (IndexError, ValueError):
        try:
            await query.answer()
        except Exception:
            pass
        return
    if not (0 <= selected <= 3):
        try:
            await query.answer()
        except Exception:
            pass
        return

    # Acknowledge the callback query immediately — Telegram requires this within 10 seconds.
    # All subsequent work (edit, reply, ChatGPT API) can take much longer.
    try:
        await query.answer()
    except Exception:
        pass

    session["awaiting"] = False
    q = session["questions"][session["current"]]
    correct = selected == q["correctIndex"]

    session["answers"].append({
        "topic": q["topic"],
        "type":  q["type"],
        "correct": correct,
    })

    correct_letter = LETTERS[q["correctIndex"]]
    correct_text   = q["options"][q["correctIndex"]]

    if correct:
        result = (
            f"✅ <b>Верно!</b>\n\n"
            f"<b>{h(correct_letter)}. {h(correct_text)}</b>\n\n"
            f"💡 {h(q['explanation'])}"
        )
    else:
        sel_letter = LETTERS[selected]
        sel_text   = q["options"][selected]
        result = (
            f"❌ <b>Неверно.</b>\n\n"
            f"Твой ответ: {h(sel_letter)}. {h(sel_text)}\n"
            f"✅ Правильный ответ: <b>{h(correct_letter)}. {h(correct_text)}</b>\n\n"
            f"💡 {h(q['explanation'])}"
        )

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as e:
        # Duplicate callback deliveries can race and try to remove an already-removed keyboard.
        # Telegram returns "Message is not modified" in this case; ignore and continue.
        if "Message is not modified" not in str(e):
            raise
    await query.message.reply_text(result, parse_mode="HTML")

    session["current"] += 1
    if session["current"] >= len(session["questions"]):
        await finish_quiz(query.message, user_id)
    else:
        session["awaiting"] = True
        # Persist progress after each answer so the quiz can be resumed from any device.
        try:
            await _save_paused_session(user_id, session)
        except Exception as e:
            print(f"[paused_session] save error: {e}")
        await send_question(query.message, user_id)

async def finish_quiz(message, user_id):
    session = user_sessions[user_id]
    answers       = session["answers"]
    session_dates = session.get("session_dates", [])

    correct_count = sum(1 for a in answers if a["correct"])
    total = len(answers)
    pct   = round(correct_count / total * 100)

    # Per-topic results this session
    topic_res = {}
    for a in answers:
        t = a["topic"]
        topic_res.setdefault(t, {"correct": 0, "total": 0})
        topic_res[t]["total"] += 1
        if a["correct"]:
            topic_res[t]["correct"] += 1

    weak = sorted(
        [(t, round(s["correct"] / s["total"] * 100)) for t, s in topic_res.items()],
        key=lambda x: x[1]
    )[:3]

    streak_cur, streak_best = calc_streak(session_dates)
    today_str = datetime.now().strftime("%Y-%m-%d")
    # today is not yet in session_dates (saved after quiz) — add 1 only for first quiz of the day
    new_streak = streak_cur if (session_dates and session_dates[-1] == today_str) else streak_cur + 1

    if pct >= 95:
        emoji, label, stars = "🎉", "Блестяще!", "⭐⭐⭐⭐⭐"
    elif pct >= 80:
        emoji, label, stars = "🎉", "Отлично!", "⭐⭐⭐⭐"
    elif pct >= 60:
        emoji, label, stars = "👍", "Хороший результат!", "⭐⭐⭐"
    elif pct >= 40:
        emoji, label, stars = "💪", "Нужно повторить.", "⭐⭐"
    else:
        emoji, label, stars = "💪", "Нужно повторить.", "⭐"

    text = (
        f"{emoji} <b>{label}</b>  {stars}\n\n"
        f"📊 Результат: <b>{correct_count} из {total} ({pct}%)</b>\n"
        f"🔥 Серия дней: {new_streak} (рекорд: {max(streak_best, new_streak)})\n"
    )
    if weak:
        text += "\n⚠️ <b>Слабые темы сегодня:</b>\n"
        for t, p in weak:
            text += f"  • {h(t)}: {p}%\n"
    text += "\n▶️ Для нового квиза напиши /quiz"

    try:
        await save_result(user_id, answers)
    except Exception as e:
        print(f"Save error: {e}")
        await message.reply_text(
            f"⚠️ <b>Не удалось сохранить результаты:</b>\n<code>{h(str(e))}</code>\n\n{text}",
            parse_mode="HTML",
        )
        del user_sessions[user_id]
        try:
            await _delete_paused_session(user_id)
        except Exception:
            pass
        return

    del user_sessions[user_id]
    try:
        await _delete_paused_session(user_id)
    except Exception:
        pass
    await message.reply_text(text, parse_mode="HTML")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    keyboard = [[
        InlineKeyboardButton("🗑 Да, удалить всё", callback_data="reset_confirm"),
        InlineKeyboardButton("❌ Отмена",           callback_data="reset_cancel"),
    ]]
    await update.message.reply_text(
        "⚠️ <b>Сброс истории</b>\n\n"
        "Это удалит <b>все твои данные</b>:\n"
        "- Все ответы на вопросы\n"
        "- Накопленную статистику по темам\n"
        "- Историю дней и серию\n\n"
        "Продолжить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await show_stats(update.message, update.effective_user.id)

async def show_stats(message, user_id: int):
    try:
        stats, session_dates = await _load_compact_data(user_id)
    except Exception as e:
        await message.reply_text(f"❌ Ошибка загрузки статистики: {e}")
        return

    if not stats and not session_dates:
        await message.reply_text("📊 Статистика пока пустая. Пройди первый квиз через /quiz")
        return

    streak_cur, streak_best = calc_streak(session_dates)
    total_questions = sum(s["total"]   for s in stats.values())
    total_correct   = sum(s["correct"] for s in stats.values())
    total_sessions  = total_questions // 20  # each quiz is exactly 20 questions
    overall_pct     = round(total_correct / total_questions * 100) if total_questions else 0

    learning_days = len(session_dates)
    is_learning = learning_days < 3

    profile = await _load_profile(user_id) or {}
    exam_date_obj = profile.get("exam_date")
    exam_line = ""
    if exam_date_obj and isinstance(exam_date_obj, date):
        days_left = max((datetime.combine(exam_date_obj, datetime.min.time()) - datetime.now()).days, 0)
        if days_left > 0:
            exam_line = f"📅 До экзамена: <b>{days_left} дней</b>\n"

    learning_status = (
        f"🎓 <b>Идёт обучение</b> ({learning_days} из 3 дней) - бот собирает статистику\n"
        if is_learning else ""
    )

    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"{learning_status}"
        f"{exam_line}"
        f"🔥 Серия дней: {streak_cur} (рекорд: {streak_best})\n"
        f"📝 Всего сессий: {total_sessions}\n"
        f"❓ Всего вопросов: {total_questions}\n"
        f"✅ Общий результат: <b>{overall_pct}%</b>\n"
    )

    # All-time topic breakdown
    if stats:
        weak   = sorted([(t, round(s["correct"]/s["total"]*100))
                         for t, s in stats.items() if s["total"] >= 1
                         and round(s["correct"]/s["total"]*100) < 60],
                        key=lambda x: x[1])
        medium = sorted([(t, round(s["correct"]/s["total"]*100))
                         for t, s in stats.items() if s["total"] >= 1
                         and 60 <= round(s["correct"]/s["total"]*100) < 85],
                        key=lambda x: x[1])
        strong = sorted([(t, round(s["correct"]/s["total"]*100))
                         for t, s in stats.items() if s["total"] >= 1
                         and round(s["correct"]/s["total"]*100) >= 85],
                        key=lambda x: -x[1])
        if weak:
            text += "\n🔴 <b>Слабые темы (&lt;60%):</b>\n"
            for t, p in weak[:5]:
                n = stats[t]["total"]
                text += f"  • {h(t)}: {p}% ({n} вопр.)\n"
        if medium:
            text += "\n🟡 <b>В процессе (60-85%):</b>\n"
            for t, p in medium[:5]:
                n = stats[t]["total"]
                text += f"  • {h(t)}: {p}% ({n} вопр.)\n"
        if strong:
            text += "\n🟢 <b>Сильные темы (≥85%):</b>\n"
            for t, p in strong[:5]:
                n = stats[t]["total"]
                text += f"  • {h(t)}: {p}% ({n} вопр.)\n"

    # Topics never practiced yet
    unseen = [t for t in MASTER_TOPICS if t not in stats or stats[t]["total"] == 0]
    if unseen:
        text += f"\n⚪ <b>Ещё не изучались ({len(unseen)}):</b>\n"
        text += ", ".join(h(t) for t in unseen) + "\n"

    # Per question-type accuracy (loaded from full answers — infrequent call)
    try:
        history = await _load_history_for_stats(user_id)
        type_st = type_stats_all(history)
        if type_st:
            text += "\n📋 <b>По типам вопросов:</b>\n"
            for qt, s in sorted(type_st.items(), key=lambda x: x[1]["correct"]/max(x[1]["total"],1)):
                pct = round(s["correct"] / s["total"] * 100) if s["total"] else 0
                bar = "🔴" if pct < 60 else "🟡" if pct < 85 else "🟢"
                name = TYPE_NAMES_RU.get(qt, qt)
                text += f"  {bar} {name}: {pct}% ({s['total']} вопр.)\n"
    except Exception:
        pass  # type stats are bonus — don't fail show_stats if history load fails

    await message.reply_text(text, parse_mode="HTML")

# ─── Text message handler (onboarding + settings edit) ────────────────────────

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text input during onboarding or settings edit."""
    user = update.effective_user
    if not is_access_allowed(user):
        return

    state = context.user_data.get("state")
    user_id = user.id
    text = update.message.text.strip()

    if state == STATE_ONBOARDING:
        step_index = context.user_data.get("step", 0)
        if step_index >= len(ONBOARDING_STEPS):
            return
        step = ONBOARDING_STEPS[step_index]
        if step["type"] != "text":
            return  # waiting for inline button, not text
        context.user_data.setdefault("onboarding_data", {})[step["key"]] = text
        next_step = step_index + 1
        context.user_data["step"] = next_step
        if next_step >= len(ONBOARDING_STEPS):
            await _finish_onboarding(update.message, user_id, context)
        else:
            await _send_onboarding_step(update.message, next_step, context)
        return

    if state == STATE_SETTINGS_EDIT:
        field = context.user_data.get("field")
        if not field:
            return
        await _update_profile_field(user_id, field, text)
        label = PROFILE_FIELD_LABELS.get(field, field)
        context.user_data.pop("state", None)
        context.user_data.pop("field", None)
        await update.message.reply_text(f"✅ Поле «{label}» обновлено.")
        return


# ─── Settings ─────────────────────────────────────────────────────────────────

PROFILE_FIELD_LABELS = {
    "display_name": "Имя",       "age":        "Возраст",
    "city":         "Город проживания", "native_lang": "Родной язык",
    "other_langs":  "Другие языки", "occupation": "Работа/занятие",
    "family":       "Семья",     "hobbies":     "Хобби",
    "greek_goal":   "Место применения", "exam_date": "Дата экзамена",
}


def _format_profile(profile: dict) -> str:
    """Format profile data for display."""
    def _v(key, default="—"):
        val = profile.get(key)
        if val is None:
            return default
        if isinstance(val, date):
            return val.strftime("%d.%m.%Y")
        return str(val)

    return (
        "👤 <b>Твой профиль</b>\n\n"
        f"Имя: {h(_v('display_name'))}\n"
        f"Возраст: {h(_v('age'))}\n"
        f"Город проживания: {h(_v('city'))}\n"
        f"Родной язык: {h(_v('native_lang'))}\n"
        f"Другие языки: {h(_v('other_langs'))}\n"
        f"Работа/занятие: {h(_v('occupation'))}\n"
        f"Семья: {h(_v('family_status'))}\n"
        f"Хобби: {h(_v('hobbies'))}\n"
        f"Место применения: {h(_v('greek_goal'))}\n"
        f"Дата экзамена: {h(_v('exam_date'))}"
    )


async def settings_menu(message):
    """Show the settings menu (called from /settings command or menu button)."""
    keyboard = [
        [InlineKeyboardButton("👤 Мой профиль",          callback_data="settings_view")],
        [InlineKeyboardButton("✏️ Изменить данные",       callback_data="settings_edit_menu")],
        [InlineKeyboardButton("🗑 Сбросить статистику",   callback_data="reset_ask")],
        [InlineKeyboardButton("🗑 Сбросить профиль",      callback_data="settings_reset_ask")],
        [InlineKeyboardButton("◀️ Назад",                 callback_data="settings_back")],
    ]
    await message.reply_text("⚙️ Настройки", reply_markup=InlineKeyboardMarkup(keyboard))


# ─── Main ───────────────────────────────────────────────────────────────────────

_bot_start_time = datetime.now()
_conflict_count = 0

async def conflict_error_handler(update, context):
    """Handle Conflict errors that arise when two bot instances run simultaneously.

    Strategy:
    - NEW instance  (uptime < 30 s): back off and retry — the old container is
      still alive but Railway will kill it shortly via SIGTERM.
    - OLD instance  (uptime ≥ 30 s): we were running fine and a new deployment
      just stole our getUpdates slot.  Send ourselves SIGTERM so run_polling's
      built-in signal handler shuts us down cleanly and the new instance wins.
    """
    global _conflict_count
    if not isinstance(context.error, Conflict):
        _conflict_count = 0
        raise context.error

    _conflict_count += 1
    uptime_s = (datetime.now() - _bot_start_time).total_seconds()

    if uptime_s > 30:
        print(f"[WARN] Conflict after {uptime_s:.0f}s uptime — new deployment detected, "
              f"shutting down this instance to yield to the new one.")
        os.kill(os.getpid(), signal.SIGTERM)
        return

    wait = min(5 * _conflict_count, 30)
    print(f"[WARN] Conflict at startup (attempt {_conflict_count}), backing off {wait}s …")
    await asyncio.sleep(wait)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await settings_menu(update.message)


async def daily_quiz_reminder(app):
    """Background task: at 18:00 Athens time send quiz reminder to users who haven't played today."""
    athens_tz = ZoneInfo("Europe/Athens")
    while True:
        now_athens = datetime.now(athens_tz)
        # Next 18:00 Athens
        target_athens = now_athens.replace(hour=18, minute=0, second=0, microsecond=0)
        if now_athens >= target_athens:
            target_athens += timedelta(days=1)
        wait_secs = (target_athens - now_athens).total_seconds()
        await asyncio.sleep(wait_secs)

        today_athens = datetime.now(athens_tz).date()
        try:
            async with _acquire() as conn:
                users = await conn.fetch("""
                    SELECT u.telegram_id FROM users u
                    WHERE u.onboarding_complete = TRUE
                    AND u.telegram_id NOT IN (
                        SELECT DISTINCT qs.user_id FROM quiz_sessions qs
                        WHERE qs.session_date = $1
                    )
                """, today_athens)
            for user in users:
                try:
                    await app.bot.send_message(
                        chat_id=user["telegram_id"],
                        text=(
                            "🔔 Сегодня ещё не было квиза!\n\n"
                            "Нажми /quiz чтобы пройти - это займёт около минуты."
                        ),
                    )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    print(f"[reminder] failed for {user['telegram_id']}: {e}")
        except Exception as e:
            print(f"[reminder] DB error: {e}")


async def post_init(app):
    # Delete any stale webhook so polling can start without a Conflict right away.
    await app.bot.delete_webhook(drop_pending_updates=True)
    await init_db()
    asyncio.create_task(daily_quiz_reminder(app))
    await app.bot.set_my_commands([
        BotCommand("start",    "Главное меню"),
        BotCommand("quiz",     "Начать квиз"),
        BotCommand("stats",    "Моя статистика"),
        BotCommand("settings", "Настройки профиля"),
        BotCommand("menu",     "Главное меню"),
    ])

def main():
    app = Application.builder().token(TG_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("menu",     menu))
    app.add_handler(CommandHandler("quiz",     quiz_command))
    app.add_handler(CommandHandler("stats",    stats_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("reset",    reset_command))
    app.add_handler(CallbackQueryHandler(handle_answer))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_error_handler(conflict_error_handler)
    app.run_polling(drop_pending_updates=True, poll_interval=1)

if __name__ == "__main__":
    main()
