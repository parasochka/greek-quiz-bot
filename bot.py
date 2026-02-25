import os
import json
import html
import random
import asyncio
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import anthropic

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS = json.loads(os.environ["GOOGLE_CREDS_JSON"])

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

def h(text):
    return html.escape(str(text))

# ─── Google Sheets ─────────────────────────────────────────────────────────────
#
# THREE worksheets:
#   History  — raw audit log: date, topic, type, correct
#   Stats    — per-topic all-time aggregates: topic, correct, total, last_seen
#              (loaded on every quiz start — always tiny, O(topics) rows)
#   Sessions — one row per unique quiz date, for streak calculation
#
# build_prompt() uses Stats + Sessions only → token cost is O(topics), never
# grows with raw history size. History is kept for auditing / future analysis.

def _open_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(GOOGLE_CREDS, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID)

def _get_or_create_ws(sh, name, rows, cols, header):
    try:
        return sh.worksheet(name)
    except Exception:
        ws = sh.add_worksheet(name, rows, cols)
        ws.append_row(header)
        return ws

def _load_compact_data():
    """
    Load Stats + Sessions — compact, fast, fixed size regardless of history length.
    Returns:
      stats        — {topic: {correct, total, last_seen}}
      session_dates — sorted list of YYYY-MM-DD strings
    """
    sh = _open_spreadsheet()

    stats_ws = _get_or_create_ws(sh, "Stats", 200, 4, ["topic", "correct", "total", "last_seen"])
    stats = {}
    for r in stats_ws.get_all_records():
        t = r.get("topic", "")
        if not t:
            continue
        stats[t] = {
            "correct": int(r.get("correct", 0)),
            "total":   int(r.get("total",   0)),
            "last_seen": r.get("last_seen", ""),
        }

    sess_ws = _get_or_create_ws(sh, "Sessions", 1000, 1, ["date"])
    session_dates = sorted(set(d for d in sess_ws.col_values(1)[1:] if d))

    return stats, session_dates

def _load_history_for_stats():
    """Load full History only for /stats display (infrequent). Not used on quiz start."""
    try:
        sh = _open_spreadsheet()
        ws = _get_or_create_ws(sh, "History", 10000, 4, ["date", "topic", "type", "correct"])
        return ws.get_all_records()
    except Exception as e:
        print(f"Load history error: {e}")
        return []

def _save_all(answers):
    """
    Persist one quiz session:
      1. Append raw rows to History
      2. Incrementally update Stats (rewrite sheet — one clear + one update call)
      3. Add today's date to Sessions (if not already present)
    """
    sh = _open_spreadsheet()
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. Raw history
    hist_ws = _get_or_create_ws(sh, "History", 10000, 4, ["date", "topic", "type", "correct"])
    hist_ws.append_rows([[today, a["topic"], a["type"], str(a["correct"])] for a in answers])

    # 2. Stats — load existing, merge today's answers, rewrite
    stats_ws = _get_or_create_ws(sh, "Stats", 200, 4, ["topic", "correct", "total", "last_seen"])
    existing = {}
    for r in stats_ws.get_all_records():
        t = r.get("topic", "")
        if t:
            existing[t] = {"correct": int(r.get("correct", 0)),
                           "total":   int(r.get("total",   0)),
                           "last_seen": r.get("last_seen", "")}
    for a in answers:
        t = a["topic"]
        if t not in existing:
            existing[t] = {"correct": 0, "total": 0, "last_seen": ""}
        existing[t]["total"] += 1
        if a["correct"]:
            existing[t]["correct"] += 1
        existing[t]["last_seen"] = today

    rows = [["topic", "correct", "total", "last_seen"]]
    rows += [[t, s["correct"], s["total"], s["last_seen"]] for t, s in existing.items()]
    stats_ws.clear()
    stats_ws.update("A1", rows)

    # 3. Sessions — add today if new
    sess_ws = _get_or_create_ws(sh, "Sessions", 1000, 1, ["date"])
    if today not in sess_ws.col_values(1)[1:]:
        sess_ws.append_row([today])

def _clear_all():
    """
    Wipe History, Stats, Sessions — keep headers. Returns number of history rows deleted.
    """
    sh = _open_spreadsheet()
    count = 0

    # History
    try:
        ws = sh.worksheet("History")
        vals = ws.get_all_values()
        count = max(0, len(vals) - 1)
        if count > 0:
            ws.delete_rows(2, len(vals))
    except Exception:
        pass

    # Stats and Sessions — clear and restore header
    for name, header in [
        ("Stats",    ["topic", "correct", "total", "last_seen"]),
        ("Sessions", ["date"]),
    ]:
        try:
            ws = sh.worksheet(name)
            ws.clear()
            ws.append_row(header)
        except Exception:
            pass

    return count

async def save_result(answers):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _save_all, answers)

async def clear_history():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _clear_all)

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
        if str(r.get("correct", "")) == "True":
            stats[qt]["correct"] += 1
    return stats

# ─── Claude prompt ─────────────────────────────────────────────────────────────
#
# Static content is cached via Anthropic prompt caching (cache_control: ephemeral).
# Only the dynamic part (per-session stats + conditional notes) is sent uncached.
# This cuts input token cost by ~80% on every quiz generation after the first call.

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
- Используй контекст из реальной жизни: поездка в офис на машине, разговор с соседом, покупки в Агора, прогулка у моря с детьми, врач, кафе в центре Лимассола.

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

Полный перечень тем (все темы должны встречаться со временем):
ГЛАГОЛЫ: είμαι, έχω, θέλω, κάνω, πάω, μπορώ, ξέρω, βλέπω, τρώω, πίνω, μιλάω, λέω, μένω, δουλεύω, αγοράζω, πληρώνω, παίρνω, δίνω, ανοίγω, κλείνω, αρχίζω, τελειώνω
ПРОШЕДШЕЕ ВРЕМЯ: αόριστος — πήγα, είπα, έκανα, ήθελα, είχα, ήμουν, αγόρασα, πλήρωσα, πήρα, είδα и др.
БУДУЩЕЕ ВРЕМЯ: θα + глагол — θα πάω, θα κάνω, θα αγοράσω, θα μιλήσω и др.
ОТРИЦАНИЕ: δεν, μην
МЕСТОИМЕНИЯ: личные (εγώ/εσύ/αυτός/αυτή/αυτό/εμείς/εσείς/αυτοί), слабые и сильные формы, притяжательные
АРТИКЛИ: определенный и неопределенный, все роды, все падежи
СУЩЕСТВИТЕЛЬНЫЕ: три рода, именительный/винительный/родительный падежи, единственное и множественное число
ПРИЛАГАТЕЛЬНЫЕ: согласование с существительным по роду, числу, падежу
УКАЗАТЕЛЬНЫЕ МЕСТОИМЕНИЯ: αυτός/αυτή/αυτό, εκείνος/εκείνη/εκείνο
ЧИСЛА: от 0 до 1000, изменение по роду (1/3/4), даты, время
ВОПРОСИТЕЛЬНЫЕ СЛОВА: πού, πότε, τι, ποιος, πώς, πόσο, γιατί, από πού
ПРЕДЛОГИ И СОЮЗЫ: σε, από, με, για, και, αλλά, ή, γιατί, όταν
БЫТОВЫЕ СИТУАЦИИ: приветствие и знакомство, кафе и ресторан, магазин и рынок, транспорт и направления, врач и аптека, гостиница, почта, банк
ВРЕМЯ И ДАТА: дни недели, месяцы, времена года, который час, когда
СЕМЬЯ: μαμά, μπαμπάς, παιδί, διδύμια, γυναίκα, άντρας, αδερφός, αδερφή, παππούς, γιαγιά, οικογένεια, παντρεμένος
ЧАСТИ ТЕЛА: κεφάλι, χέρι, πόδι, στομάχι, πλάτη, μάτι, αυτί, μύτη, στόμα, δόντι, λαιμός
ПОГОДА: ήλιος, βροχή, ζέστη, κρύο, αέρας, θερμοκρασία, συννεφιά, θάλασσα, καιρός
ДОМ/КВАРТИРА: σπίτι, δωμάτιο, κουζίνα, μπάνιο, σαλόνι, μπαλκόνι, ενοίκιο, γείτονας, διαμέρισμα
ЕДА/ПРОДУКТЫ: ψωμί, κρέας, λαχανικά, φρούτα, γάλα, τυρί, ψάρι, νερό, καφές, σούπερ μάρκετ, αγορά
ОДЕЖДА: ρούχα, παπούτσια, φόρεμα, παντελόνι, μπλούζα, μέγεθος, χρώμα, φοράω
НАРЕЧИЯ: πάντα, ποτέ, συχνά, μερικές φορές, ήδη, ακόμα, σύντομα, αμέσως, μαζί, μόνος, πολύ, λίγο

Типы вопросов — строго вперемешку, примерно поровну:
1. ru_to_gr — перевод с русского на греческий: "Как сказать по-гречески: «Я хочу кофе»?" — 4 варианта на греческом
2. gr_to_ru — перевод с греческого на русский: "Что означает фраза «Πού είναι η στάση;»?" — 4 варианта на русском
3. choose_form — выбор правильной формы: "Вижу ___ (красивая женщина)" — 4 варианта на греческом с разными артиклями, падежами или окончаниями
4. fill_blank — вставить слово в предложение: "Εγώ ___ στην Αθήνα." — 4 варианта на греческом

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
    The static part lives in STATIC_SYSTEM_PROMPT and is sent with cache_control.

    stats        — {topic: {correct, total, last_seen}}  (from Stats sheet, compact)
    session_dates — sorted list of date strings          (from Sessions sheet, compact)

    Dynamic prompt size is O(number_of_topics) — never grows with raw history length.
    """
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

    review_note = ""
    if days_away >= 2:
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
        system=[
            {
                "type": "text",
                "text": STATIC_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
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
        await show_stats(query.message)

    elif query.data == "menu_about":
        await query.message.reply_text(
            "📖 <b>О боте</b>\n\n"
            "Помогает готовиться к экзамену A2 по современному греческому языку.\n\n"
            "<b>Как работает:</b>\n"
            "• Квиз из 20 вопросов каждый день\n"
            "• Вопросы адаптируются под твою историю\n"
            "• Слабые темы повторяются чаще\n"
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
        loop = asyncio.get_running_loop()
        stats, session_dates = await loop.run_in_executor(None, _load_compact_data)

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
            count = await clear_history()
            await query.message.reply_text(
                f"🗑 <b>История очищена.</b>\n"
                f"Удалено строк из History: {count}\n"
                f"Stats и Sessions также сброшены.\n\n"
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
        await save_result(answers)
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
        "Это удалит <b>все записи</b> из таблицы Google Sheets:\n"
        "• History — полный лог ответов\n"
        "• Stats — накопленная статистика по темам\n"
        "• Sessions — история дней и серия\n\n"
        "Продолжить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ALLOWED_USERNAME:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await show_stats(update.message)

async def show_stats(message):
    loop = asyncio.get_running_loop()

    try:
        stats, session_dates = await loop.run_in_executor(None, _load_compact_data)
    except Exception as e:
        await message.reply_text(f"❌ Ошибка загрузки статистики: {e}")
        return

    if not stats and not session_dates:
        await message.reply_text("📊 Статистика пока пустая. Пройди первый квиз через /quiz")
        return

    streak_cur, streak_best = calc_streak(session_dates)
    total_sessions  = len(session_dates)
    total_questions = sum(s["total"]   for s in stats.values())
    total_correct   = sum(s["correct"] for s in stats.values())
    overall_pct     = round(total_correct / total_questions * 100) if total_questions else 0

    exam_date  = datetime(2026, 5, 19)
    days_left  = max((exam_date - datetime.now()).days, 0)

    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
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

    # Per question-type accuracy (loaded from full History — infrequent call)
    try:
        history = await loop.run_in_executor(None, _load_history_for_stats)
        type_st = type_stats_all(history)
        if type_st:
            text += "\n📋 <b>По типам вопросов:</b>\n"
            for qt, s in sorted(type_st.items(), key=lambda x: x[1]["correct"]/max(x[1]["total"],1)):
                pct = round(s["correct"] / s["total"] * 100) if s["total"] else 0
                bar = "🔴" if pct < 60 else "🟡" if pct < 85 else "🟢"
                name = TYPE_NAMES_RU.get(qt, qt)
                text += f"  {bar} {name}: {pct}% ({s['total']} вопр.)\n"
    except Exception:
        pass  # type stats are bonus — don't fail show_stats if History load fails

    await message.reply_text(text, parse_mode="HTML")

# ─── Main ───────────────────────────────────────────────────────────────────────

async def post_init(app):
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
    app.run_polling()

if __name__ == "__main__":
    main()
