import os
import json
import html
import random
import asyncio
import difflib
import contextlib
import asyncpg
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import Conflict
import anthropic

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"ERROR: Required environment variable '{name}' is not set.\n"
            f"Add it to your deployment settings (Railway → Variables)."
        )
    return value

ANTHROPIC_KEY = _require_env("ANTHROPIC_API_KEY")
TG_TOKEN = _require_env("TELEGRAM_TOKEN")
DATABASE_URL = _require_env("DATABASE_URL").replace("postgres://", "postgresql://", 1)

db_pool = None


@contextlib.asynccontextmanager
async def _acquire():
    async with db_pool.acquire() as conn:
        yield conn


LETTERS = ["А", "Б", "В", "Г"]

ALLOWED_USERNAME = "aparasochka"

# Canonical topic names — used to detect unseen topics and enforce consistent Stats keys.
# Claude is instructed to use EXACTLY these strings in the "topic" field of each question.
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

    Claude occasionally mixes in visually similar Greek characters (e.g. ο, ι, Και)
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
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS quiz_sessions (
                id              SERIAL PRIMARY KEY,
                user_id         BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                session_date    DATE NOT NULL,
                completed_at    TIMESTAMPTZ DEFAULT NOW(),
                correct_answers INT,
                total_questions INT DEFAULT 20
            );
            CREATE TABLE IF NOT EXISTS answers (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                session_id    INT REFERENCES quiz_sessions(id) ON DELETE CASCADE,
                answered_at   TIMESTAMPTZ DEFAULT NOW(),
                topic         VARCHAR(100) NOT NULL,
                question_type VARCHAR(20)  NOT NULL,
                correct       BOOLEAN      NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_stats (
                user_id   BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                topic     VARCHAR(100) NOT NULL,
                correct   INT  DEFAULT 0,
                total     INT  DEFAULT 0,
                last_seen DATE,
                PRIMARY KEY (user_id, topic)
            );
        """)


async def register_user(user):
    async with _acquire() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, username, first_name) "
            "VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
            user.id, user.username, user.first_name,
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


async def _clear_all(user_id: int):
    """
    Wipe answers, quiz_sessions, topic_stats for this user. Returns number of answers deleted.
    """
    async with _acquire() as conn:
        async with conn.transaction():
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM answers WHERE user_id=$1", user_id,
            )
            await conn.execute("DELETE FROM answers WHERE user_id=$1", user_id)
            await conn.execute("DELETE FROM quiz_sessions WHERE user_id=$1", user_id)
            await conn.execute("DELETE FROM topic_stats WHERE user_id=$1", user_id)
            return count


async def save_result(user_id: int, answers: list):
    await _save_all(user_id, answers)


async def clear_history(user_id: int):
    return await _clear_all(user_id)

# ─── Stats helpers ─────────────────────────────────────────────────────────────

def calc_streak(session_dates):
    """session_dates: sorted list of YYYY-MM-DD strings."""
    if not session_dates:
        return 0, 0
    best = cur = 1
    for i in range(1, len(session_dates)):
        diff = (datetime.strptime(session_dates[i], "%Y-%m-%d") -
                datetime.strptime(session_dates[i-1], "%Y-%m-%d")).days
        if diff == 1:
            cur += 1
            best = max(best, cur)
        elif diff > 1:
            cur = 1
    today = datetime.now().strftime("%Y-%m-%d")
    diff = (datetime.strptime(today, "%Y-%m-%d") -
            datetime.strptime(session_dates[-1], "%Y-%m-%d")).days
    current = cur if diff <= 1 else 0
    return current, best

def days_since_last_session(session_dates):
    if not session_dates:
        return 99
    return (datetime.now() - datetime.strptime(session_dates[-1], "%Y-%m-%d")).days

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

# ─── Claude prompt ─────────────────────────────────────────────────────────────

STATIC_SYSTEM_PROMPT = """Ты генератор вопросов для ежедневного квиза по греческому языку уровней A1-A2.

Ученик: Артем Парасочка, 36 лет (31.12.1989), живёт в Лимассоле, Кипр — 5 лет. Из России.
Родной язык: русский. Английский: хорошо.
Работа: онлайн-маркетинг / IT, каждый день едет в офис на машине.
Семья: жена Ольга, дети-двойняшки Роберт и Лили (1.5 года).
Греческий использует: с соседями, в магазинах, кафе. По выходным гуляет на набережной Молос, рынке Агора, в парке и центре Лимассола.
Цель: сдать официальный экзамен A2 по современному стандартному греческому языку на Кипре 19 мая 2026.

КРИТИЧЕСКИ ВАЖНО:
- Только стандартный современный греческий язык (νέα ελληνική γλώσσα).
- Никакого кипрского диалекта, кипрских слов, кипрского произношения.
- Артем не использует греческую клавиатуру. Все вопросы только с вариантами ответа, без ввода текста.
- КАЖДЫЙ вопрос обязан быть встроен в мини-ситуацию из жизни Артёма. Текст вопроса начинай с короткого сценария (1-2 предложения), потом задавай языковую задачу. Ситуации: еду в офис, разговор с соседом, покупки в Агора, прогулка с детьми у моря, врач/аптека, кафе/ресторан в центре Лимассола, разговор с Ольгой дома.
  Плохо: «Как сказать по-гречески: "31 декабря"?»
  Хорошо: «Ты договариваешься с коллегой о корпоративе. Как сказать: "Вечеринка будет 31 декабря"?»
  Плохо: «Вставь артикль: ___ γυναίκα είναι όμορφη.»
  Хорошо: «Ты рассказываешь соседу о жене. Вставь нужный артикль: "___ γυναίκα μου είναι πολύ ωραία."»

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
Неправильные варианты — правдоподобные: похожие формы, близкие слова, частые ошибки."""


def build_prompt(stats, session_dates):
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

    # Unseen topics — explicitly listed so Claude knows exactly what hasn't been practiced
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

    exam_date = datetime(2026, 5, 19)
    days_left = max((exam_date - datetime.now()).days, 0)
    pre_exam_note = ""
    if days_left <= 30:
        pre_exam_note = (
            "ПРЕДЭКЗАМЕНАЦИОННЫЙ РЕЖИМ: из 20 вопросов ровно 6 должны быть в формате "
            "короткий текст или диалог на греческом (3-5 строк) + вопрос на понимание прочитанного. "
            "Эти 6 вопросов входят в общий лимит 20, не сверх него.\n"
        )

    return (
        f"До экзамена: {days_left} дней.\n"
        f"{learning_note}"
        f"{review_note}"
        f"{pre_exam_note}"
        f"Статистика ученика по темам (накопленная за всё время):\n"
        f"{hist_summary}"
    )


def generate_questions(stats, session_dates):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    dynamic_prompt = build_prompt(stats, session_dates)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=STATIC_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": dynamic_prompt}],
    )
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        start = raw.index("[")
        end = raw.rindex("]")
        questions = json.loads(raw[start:end+1])
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Не удалось распарсить ответ Claude: {e}\nСырой ответ: {raw[:300]}")

    # Validate correctIndex before shuffle — catches silent scoring bugs
    for i, q in enumerate(questions):
        if not (0 <= q.get("correctIndex", -1) < len(q.get("options", []))):
            raise ValueError(f"Question {i}: correctIndex={q.get('correctIndex')} out of range")

    # Normalise topic names — guard against mixed Greek/Cyrillic characters
    for q in questions:
        q["topic"] = normalize_topic(q["topic"])

    # Server-side shuffle — correct answer is never stuck at position 0
    for q in questions:
        correct_text = q["options"][q["correctIndex"]]
        random.shuffle(q["options"])
        q["correctIndex"] = q["options"].index(correct_text)

    return questions

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ALLOWED_USERNAME:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await register_user(update.effective_user)
    keyboard = [
        [InlineKeyboardButton("🎯 Начать квиз",    callback_data="menu_quiz")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("ℹ️ О боте",          callback_data="menu_about")],
    ]
    await update.message.reply_text(
        "👋 Привет! Я твой тренер по греческому языку.\n\n"
        "Каждый день я генерирую новый квиз из 20 вопросов, "
        "адаптированный под твой уровень и историю ответов.\n\n"
        "🎯 Цель: подготовка к экзамену A2 по современному греческому языку.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ALLOWED_USERNAME:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    keyboard = [
        [InlineKeyboardButton("🎯 Начать квиз",    callback_data="menu_quiz")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("ℹ️ О боте",          callback_data="menu_about")],
    ]
    await update.message.reply_text("📋 Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ALLOWED_USERNAME:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await start_quiz(update.message, update.effective_user.id)

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    if query.data == "menu_quiz":
        await query.message.reply_text("⏳ Запускаю квиз...")
        await start_quiz(query.message, query.from_user.id)

    elif query.data == "menu_stats":
        await show_stats(query.message, query.from_user.id)

    elif query.data == "menu_about":
        await query.message.reply_text(
            "📖 <b>О боте</b>\n\n"
            "Помогает готовиться к экзамену A2 по современному греческому языку.\n\n"
            "<b>Как работает:</b>\n"
            "• Квиз из 20 вопросов каждый день\n"
            "• Первые 3 дня — режим обучения: бот равномерно охватывает все темы, чтобы собрать базовую статистику\n"
            "• С 4-го дня — адаптивный режим: слабые темы повторяются чаще, сильные — реже\n"
            "• После каждого ответа — объяснение правила\n\n"
            "<b>Команды:</b>\n"
            "/quiz — начать квиз\n"
            "/stats — статистика\n"
            "/reset — сбросить историю\n"
            "/menu — главное меню",
            parse_mode="HTML",
        )

async def start_quiz(message, user_id):
    msg = await message.reply_text("⏳ Готовлю квиз... Это займет около 15 секунд.")
    try:
        stats, session_dates = await _load_compact_data(user_id)

        loop = asyncio.get_running_loop()
        last_exc = None
        questions = None
        for attempt in range(3):
            try:
                questions = await loop.run_in_executor(None, generate_questions, stats, session_dates)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s
        if questions is None:
            raise last_exc

        user_sessions[user_id] = {
            "questions": questions,
            "current": 0,
            "answers": [],
            "awaiting": True,
            "session_dates": session_dates,
        }
        await msg.delete()
        await send_question(message, user_id)
    except Exception as e:
        await msg.edit_text(f"❌ Не удалось загрузить квиз: {e}\n\nПопробуй ещё раз через /quiz")

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
    if query.from_user.username != ALLOWED_USERNAME:
        await query.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    user_id = query.from_user.id
    data = query.data

    # ── Menu ──
    if data.startswith("menu_"):
        await handle_menu(update, context)
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

    # ── Quiz answer ──
    if not data.startswith("ans_"):
        try:
            await query.answer()
        except Exception:
            pass
        return

    if user_id not in user_sessions:
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
    # All subsequent work (edit, reply, Claude API) can take much longer.
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

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(result, parse_mode="HTML")

    session["current"] += 1
    if session["current"] >= len(session["questions"]):
        await finish_quiz(query.message, user_id)
    else:
        session["awaiting"] = True
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
        return

    del user_sessions[user_id]
    await message.reply_text(text, parse_mode="HTML")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ALLOWED_USERNAME:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    keyboard = [[
        InlineKeyboardButton("🗑 Да, удалить всё", callback_data="reset_confirm"),
        InlineKeyboardButton("❌ Отмена",           callback_data="reset_cancel"),
    ]]
    await update.message.reply_text(
        "⚠️ <b>Сброс истории</b>\n\n"
        "Это удалит <b>все твои данные</b>:\n"
        "• Все ответы на вопросы\n"
        "• Накопленную статистику по темам\n"
        "• Историю дней и серию\n\n"
        "Продолжить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ALLOWED_USERNAME:
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

    exam_date  = datetime(2026, 5, 19)
    days_left  = max((exam_date - datetime.now()).days, 0)

    learning_status = (
        f"🎓 <b>Идёт обучение</b> ({learning_days} из 3 дней) — бот собирает статистику\n"
        if is_learning else ""
    )

    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"{learning_status}"
        f"📅 До экзамена: <b>{days_left} дней</b>\n"
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

# ─── Main ───────────────────────────────────────────────────────────────────────

async def conflict_error_handler(update, context):
    """Suppress Conflict errors that appear briefly when a new deploy starts
    while the previous container is still shutting down. python-telegram-bot
    retries automatically; we just want a clean warning instead of a traceback."""
    if isinstance(context.error, Conflict):
        print("[WARN] Conflict: another bot instance still running, will retry automatically.")
        return
    raise context.error

async def post_init(app):
    await init_db()
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("quiz",  "Начать квиз"),
        BotCommand("stats", "Моя статистика"),
        BotCommand("reset", "Сбросить историю"),
        BotCommand("menu",  "Главное меню"),
    ])

def main():
    app = Application.builder().token(TG_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu",  menu))
    app.add_handler(CommandHandler("quiz",  quiz_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CallbackQueryHandler(handle_answer))
    app.add_error_handler(conflict_error_handler)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
