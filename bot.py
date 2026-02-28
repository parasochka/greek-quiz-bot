import os
import signal
import time
import html
import asyncio
import contextlib
import traceback
import asyncpg
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest, Conflict

from config import (
    ALLOWED_USERNAMES,
    DATABASE_URL,
    LETTERS,
    MAIN_MENU_KEYBOARD,
    ONBOARDING_STEPS,
    OWNER_USERNAME,
    PAUSED_SESSION_TTL_HOURS,
    QUIZ_GENERATION_TIMEOUT_SEC,
    QUIZ_QUESTION_COUNT,
    STATE_ONBOARDING,
    STATE_SETTINGS_EDIT,
    TG_TOKEN,
    WELCOME_TEXT,
)
from topics import MASTER_TOPICS, build_topic_sequence
from quiz_generation import TYPE_LABELS, generate_questions


db_pool = None


@contextlib.asynccontextmanager
async def _acquire():
    async with db_pool.acquire() as conn:
        yield conn


def is_access_allowed(user) -> bool:
    """Currently only allowed users have access. Future: check subscription_status."""
    return user.username in ALLOWED_USERNAMES


def is_owner(user) -> bool:
    return bool(user and user.username == OWNER_USERNAME)


def get_main_menu_keyboard(user) -> list[list[InlineKeyboardButton]]:
    keyboard = [row[:] for row in MAIN_MENU_KEYBOARD]
    if is_owner(user):
        keyboard.append([InlineKeyboardButton("🛠 Админка", callback_data="menu_admin")])
    return keyboard


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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_events (
                id         SERIAL PRIMARY KEY,
                level      VARCHAR(16) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                message    TEXT NOT NULL,
                details    TEXT,
                user_id    BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    Wipe every per-user statistics table while keeping user account/profile info.
    Returns number of answers deleted.
    """
    async with _acquire() as conn:
        async with conn.transaction():
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM answers WHERE user_id=$1", user_id,
            )

            # Preserve profile/account info and clear every other table that stores
            # rows by user_id. This keeps admin reset future-proof when new
            # statistics tables are added.
            tables = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND column_name = 'user_id'
                  AND table_name NOT IN ('users', 'user_profiles')
                ORDER BY table_name
                """
            )
            for row in tables:
                table_name = row["table_name"]
                safe_name = table_name.replace('"', '""')
                await conn.execute(
                    f'DELETE FROM "{safe_name}" WHERE user_id=$1',
                    user_id,
                )
            return count


async def save_result(user_id: int, answers: list):
    await _save_all(user_id, answers)


async def clear_history(user_id: int):
    return await _clear_all(user_id)


async def _admin_list_users_with_quiz_counts():
    async with _acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.telegram_id, u.username, u.first_name, COUNT(qs.id) AS quiz_count
            FROM users u
            LEFT JOIN quiz_sessions qs ON qs.user_id = u.telegram_id
            GROUP BY u.telegram_id, u.username, u.first_name
            ORDER BY quiz_count DESC, u.first_name NULLS LAST, u.username NULLS LAST
            """
        )
    return rows


async def log_admin_event(level: str, event_type: str, message: str, details: str = "", user_id: int | None = None):
    payload = f"[{level}/{event_type}] {message}"
    if details:
        payload += f" | {details[:300]}"
    print(payload)

    if db_pool is None:
        return

    try:
        async with _acquire() as conn:
            await conn.execute(
                """
                INSERT INTO admin_events (level, event_type, message, details, user_id)
                VALUES ($1, $2, $3, $4, $5)
                """,
                level[:16], event_type[:64], message[:1000], (details or "")[:4000], user_id,
            )
    except Exception as e:
        print(f"[WARN/admin_events] failed to persist event: {e}")


async def _admin_health_snapshot():
    async with _acquire() as conn:
        users_total = await conn.fetchval("SELECT COUNT(*) FROM users")
        users_onboarded = await conn.fetchval("SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE")
        quizzes_today = await conn.fetchval("SELECT COUNT(*) FROM quiz_sessions WHERE session_date = CURRENT_DATE")
        active_paused = await conn.fetchval("SELECT COUNT(*) FROM paused_sessions WHERE expires_at > NOW()")
        last_quiz_at = await conn.fetchval("SELECT MAX(completed_at) FROM quiz_sessions")
    return {
        "users_total": users_total or 0,
        "users_onboarded": users_onboarded or 0,
        "quizzes_today": quizzes_today or 0,
        "active_paused": active_paused or 0,
        "last_quiz_at": last_quiz_at,
    }


async def _admin_recent_events(limit: int = 8):
    async with _acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT level, event_type, message, details, user_id, created_at
            FROM admin_events
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return rows


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

    def _decode_jsonb(value, fallback):
        """Handle JSONB decoded as text (default asyncpg) or native Python objects (custom codec)."""
        if value is None:
            return fallback
        if isinstance(value, (list, dict)):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return fallback
        return fallback

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
        "questions":     _decode_jsonb(row["questions"], []),
        "current":       row["current_idx"],
        "answers":       _decode_jsonb(row["answers"], []),
        "awaiting":      True,
        "session_dates": _decode_jsonb(row["session_dates"], []),
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


# ─── Session storage ───────────────────────────────────────────────────────────

user_sessions = {}
user_answer_locks = {}


def _get_user_answer_lock(user_id: int) -> asyncio.Lock:
    """Serialize answer callbacks per user to avoid race conditions on rapid taps."""
    lock = user_answer_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        user_answer_locks[user_id] = lock
    return lock

# ─── Handlers ──────────────────────────────────────────────────────────────────

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
        reply_markup=InlineKeyboardMarkup(get_main_menu_keyboard(message.from_user)),
    )


async def send_about_message(message):
    await message.reply_text(
        "📖 <b>О Greekly</b>\n\n"
        "Greekly помогает учить современный греческий язык (уровень A2).\n\n"
        "<b>Как работает:</b>\n"
        "• Квизы из 20 вопросов - сколько хочешь в день\n"
        "• Все вопросы генерирует AI на основе твоего профиля\n"
        "• Первые 3 дня - режим обучения: Greekly охватывает все темы\n"
        "• С 4-го дня - адаптивный режим: слабые темы чаще, сильные реже\n"
        "• После каждого ответа - объяснение правила\n\n"
        "<b>Команды:</b>\n"
        "/start - главное меню\n"
        "/quiz - начать квиз\n"
        "/stats - статистика\n"
        "/settings - настройки профиля\n"
        "/about - о Greekly\n"
        "/admin - админка\n\n"
        "⚠️ Вопросы генерирует AI - возможны неточности.\n\n"
        "Автор: @aparasochka",
        parse_mode="HTML",
    )


async def show_admin_menu(message):
    keyboard = [
        [InlineKeyboardButton("📈 Статистика пользователей", callback_data="admin_user_stats")],
        [InlineKeyboardButton("🚨 Логи и здоровье", callback_data="admin_logs")],
    ]
    await message.reply_text("🛠 <b>Админка</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def show_admin_user_stats(message):
    rows = await _admin_list_users_with_quiz_counts()
    if not rows:
        await message.reply_text("Пока нет пользователей.")
        return

    keyboard = []
    for row in rows:
        title = row["first_name"] or (f"@{row['username']}" if row["username"] else str(row["telegram_id"]))
        username = f" (@{row['username']})" if row["username"] else ""
        label = f"{title}{username} — {row['quiz_count']}"
        keyboard.append([InlineKeyboardButton(label[:64], callback_data=f"admin_reset_{row['telegram_id']}")])

    await message.reply_text(
        "📈 <b>Статистика пользователей</b>\n"
        "Нажми на пользователя, чтобы полностью сбросить его память (квизы/статистику).",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_admin_logs(message):
    health = await _admin_health_snapshot()
    events = await _admin_recent_events()

    lines = [
        "🚨 <b>Критичные логи и здоровье</b>",
        "",
        f"👥 Пользователей: <b>{health['users_total']}</b> (с анкетой: {health['users_onboarded']})",
        f"📘 Квизов сегодня: <b>{health['quizzes_today']}</b>",
        f"⏸ Активных пауз квиза: <b>{health['active_paused']}</b>",
        f"🕒 Последний завершённый квиз: <b>{health['last_quiz_at'] or '—'}</b>",
        "",
        "<b>Последние события:</b>",
    ]

    if not events:
        lines.append("— пока нет событий —")
    else:
        for row in events:
            ts = row["created_at"].strftime("%d.%m %H:%M")
            uid = f" uid={row['user_id']}" if row["user_id"] else ""
            details = (row["details"] or "").strip()
            details_short = f" | {h(details[:120])}" if details else ""
            lines.append(f"• [{ts}] <b>{h(row['level'])}</b> {h(row['event_type'])}{uid}: {h(row['message'])}{details_short}")

    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await register_user(update.effective_user)
    context.user_data.clear()

    if await _is_onboarding_complete(update.effective_user.id):
        await update.message.reply_text(
            "📋 Главное меню:",
            reply_markup=InlineKeyboardMarkup(get_main_menu_keyboard(update.effective_user)),
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
        reply_markup=InlineKeyboardMarkup(get_main_menu_keyboard(update.effective_user)),
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
    await start_quiz(update.message, update.effective_user.id)

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
        await start_quiz(query.message, query.from_user.id)

    elif query.data == "menu_stats":
        await show_stats(query.message, query.from_user.id)

    elif query.data == "menu_settings":
        await settings_menu(query.message)

    elif query.data == "menu_about":
        await send_about_message(query.message)

    elif query.data == "menu_admin":
        if not is_owner(query.from_user):
            await query.message.reply_text("⛔ Доступ запрещён.")
            return
        await show_admin_menu(query.message)

async def start_quiz(message, user_id):
    # Restore a paused session if one exists (survives bot restarts and device switches).
    paused = await _load_paused_session(user_id)
    if paused:
        answered = paused["current"]
        total = len(paused["questions"])
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

    await _start_new_quiz(message, user_id)


async def _start_new_quiz(message, user_id):
    """Generate fresh questions and start a new quiz, discarding any paused state."""
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
    session = user_sessions.get(user_id)
    if session is None:
        await message.reply_text("⚠️ Сессия не найдена. Начни квиз заново через /quiz")
        return
    q = session["questions"][session["current"]]
    num = session["current"] + 1
    total = len(session["questions"])

    type_label = TYPE_LABELS.get(q.get("type", ""), "❓ Вопрос")
    question_idx = session["current"]
    keyboard = [
        [InlineKeyboardButton(f"{LETTERS[i]}. {opt}", callback_data=f"ans_{question_idx}_{i}")]
        for i, opt in enumerate(q["options"])
    ]
    await message.reply_text(
        f"<b>Вопрос {num} из {total}</b>  •  {type_label}\n"
        f"📌 <i>Тема: {h(q['topic'])}</i>\n\n"
        f"❓ {h(q['question'])}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

PROFILE_FIELD_LABELS = {
    "display_name": "Имя",       "age":        "Возраст",
    "city":         "Город проживания", "native_lang": "Родной язык",
    "other_langs":  "Другие языки", "occupation": "Работа/занятие",
    "family":       "Семья",     "hobbies":     "Хобби",
    "greek_goal":   "Место применения", "exam_date": "Дата экзамена",
}


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_access_allowed(query.from_user):
        await query.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    user_id = query.from_user.id
    data = query.data

    async def _clear_reply_markup() -> None:
        """Safely remove inline keyboard from callback message.

        Telegram may deliver duplicate callbacks for the same button press.
        In races, the second edit gets "Message is not modified" — ignore it.
        """
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

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
        await _clear_reply_markup()
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
            reply_markup=InlineKeyboardMarkup(get_main_menu_keyboard(query.from_user)),
        )
        return

    if data == "admin_user_stats":
        try:
            await query.answer()
        except Exception:
            pass
        if not is_owner(query.from_user):
            await query.message.reply_text("⛔ Доступ запрещён.")
            return
        await show_admin_user_stats(query.message)
        return

    if data == "admin_logs":
        try:
            await query.answer()
        except Exception:
            pass
        if not is_owner(query.from_user):
            await query.message.reply_text("⛔ Доступ запрещён.")
            return
        await show_admin_logs(query.message)
        return

    if data.startswith("admin_reset_confirm_"):
        try:
            await query.answer()
        except Exception:
            pass
        if not is_owner(query.from_user):
            await query.message.reply_text("⛔ Доступ запрещён.")
            return
        try:
            target_user_id = int(data.split("_")[-1])
        except ValueError:
            await query.message.reply_text("Некорректный идентификатор пользователя.")
            return

        count = await clear_history(target_user_id)
        user_sessions.pop(target_user_id, None)
        await query.message.reply_text(
            f"🧹 Статистика пользователя {target_user_id} полностью очищена. "
            f"Удалено ответов: {count}."
        )
        await show_admin_user_stats(query.message)
        return

    if data.startswith("admin_reset_"):
        try:
            await query.answer()
        except Exception:
            pass
        if not is_owner(query.from_user):
            await query.message.reply_text("⛔ Доступ запрещён.")
            return
        try:
            target_user_id = int(data.split("_")[-1])
        except ValueError:
            await query.message.reply_text("Некорректный идентификатор пользователя.")
            return

        keyboard = [[
            InlineKeyboardButton(
                "🗑 Да, очистить полностью",
                callback_data=f"admin_reset_confirm_{target_user_id}",
            ),
            InlineKeyboardButton("❌ Отмена", callback_data="admin_user_stats"),
        ]]
        await query.message.reply_text(
            "⚠️ <b>Подтверждение очистки статистики</b>\n\n"
            f"Пользователь: <code>{target_user_id}</code>\n"
            "Будет удалена вся учебная статистика (квизы, ответы, память, паузы и др.), "
            "но данные профиля и аккаунта останутся.\n\n"
            "Продолжить?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
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
        await _clear_reply_markup()
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
        await _clear_reply_markup()
        await query.message.reply_text("✅ Отмена. История не тронута.")
        return

    if data == "quiz_resume":
        try:
            await query.answer()
        except Exception:
            pass
        await _clear_reply_markup()
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

    if data == "quiz_restart":
        try:
            await query.answer()
        except Exception:
            pass
        await _clear_reply_markup()
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

    lock = _get_user_answer_lock(user_id)
    async with lock:
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
                await query.answer("Ответ уже принят.")
            except Exception:
                pass
            return

        parts = data.split("_")
        try:
            if len(parts) == 3:
                q_idx = int(parts[1])
                selected = int(parts[2])
            else:
                # Backward compatibility with old callback format ans_<option>.
                q_idx = session["current"]
                selected = int(parts[1])
        except (IndexError, ValueError):
            try:
                await query.answer()
            except Exception:
                pass
            return

        if q_idx != session["current"]:
            try:
                await query.answer("Этот вопрос уже закрыт.")
            except Exception:
                pass
            await _clear_reply_markup()
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

        await _clear_reply_markup()
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
    if total == 0:
        await message.reply_text("⚠️ Квиз завершён, но ответов не найдено.")
        del user_sessions[user_id]
        return
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
        async with _acquire() as conn:
            total_sessions = await conn.fetchval(
                "SELECT COUNT(*) FROM quiz_sessions WHERE user_id=$1", user_id
            ) or 0
    except Exception as e:
        await message.reply_text(f"❌ Ошибка загрузки статистики: {e}")
        return

    if not stats and not session_dates:
        await message.reply_text("📊 Статистика пока пустая. Пройди первый квиз через /quiz")
        return

    streak_cur, streak_best = calc_streak(session_dates)
    total_questions = sum(s["total"]   for s in stats.values())
    total_correct   = sum(s["correct"] for s in stats.values())
    overall_pct     = round(total_correct / total_questions * 100) if total_questions else 0

    learning_days = len(session_dates)
    is_learning = learning_days < 3

    profile = await _load_profile(user_id) or {}
    exam_date_obj = profile.get("exam_date")
    exam_line = ""
    if exam_date_obj and isinstance(exam_date_obj, date):
        days_left = max((exam_date_obj - date.today()).days, 0)
        if days_left > 0:
            exam_line = f"📅 До экзамена: <b>{days_left} дней</b>\n"

    learning_status = (
        f"🎓 <b>Идёт обучение</b> ({learning_days} из 3 дней) - Greekly собирает статистику\n"
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

async def global_error_handler(update, context):
    """Capture conflicts + unhandled exceptions and persist admin-facing logs."""
    global _conflict_count

    if isinstance(context.error, Conflict):
        _conflict_count += 1
        uptime_s = (datetime.now() - _bot_start_time).total_seconds()

        if uptime_s > 30:
            msg = (
                f"Conflict after {uptime_s:.0f}s uptime — new deployment detected, "
                "shutting down this instance."
            )
            await log_admin_event("WARN", "telegram_conflict", msg)
            os.kill(os.getpid(), signal.SIGTERM)
            return

        wait = min(5 * _conflict_count, 30)
        await log_admin_event("WARN", "telegram_conflict", f"Conflict at startup, backoff {wait}s")
        await asyncio.sleep(wait)
        return

    _conflict_count = 0
    update_type = type(update).__name__ if update is not None else "none"
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    await log_admin_event(
        "ERROR",
        "handler_exception",
        f"Unhandled exception in update={update_type}: {context.error}",
        details=tb,
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await settings_menu(update.message)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await send_about_message(update.message)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_access_allowed(update.effective_user) or not is_owner(update.effective_user):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await show_admin_menu(update.message)


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
                    await log_admin_event(
                        "WARN", "reminder_send_failed", f"Failed reminder delivery: {e}", user_id=user["telegram_id"],
                    )
        except Exception as e:
            await log_admin_event("ERROR", "reminder_db_error", f"Reminder DB error: {e}")


async def post_init(app):
    # Delete any stale webhook so polling can start without a Conflict right away.
    await app.bot.delete_webhook(drop_pending_updates=True)
    await init_db()
    await log_admin_event("INFO", "startup", "Bot started and DB initialized")
    asyncio.create_task(daily_quiz_reminder(app))
    await app.bot.set_my_commands([
        BotCommand("start",    "Главное меню"),
        BotCommand("quiz",     "Начать квиз"),
        BotCommand("stats",    "Моя статистика"),
        BotCommand("settings", "Настройки профиля"),
        BotCommand("about",    "О Greekly"),
        BotCommand("admin",    "Админка"),
    ])

def main():
    app = Application.builder().token(TG_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("quiz",     quiz_command))
    app.add_handler(CommandHandler("stats",    stats_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("about",    about_command))
    app.add_handler(CommandHandler("admin",    admin_command))
    app.add_handler(CommandHandler("menu",     menu))
    app.add_handler(CommandHandler("reset",    reset_command))
    app.add_handler(CallbackQueryHandler(handle_answer))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_error_handler(global_error_handler)
    app.run_polling(drop_pending_updates=True, poll_interval=1)

if __name__ == "__main__":
    main()
