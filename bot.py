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

def h(text):
    """Escape text for HTML parse mode."""
    return html.escape(str(text))

# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(GOOGLE_CREDS, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    try:
        return sh.worksheet("History")
    except Exception:
        ws = sh.add_worksheet("History", 2000, 10)
        ws.append_row(["date", "topic", "type", "correct"])
        return ws

def load_history():
    try:
        ws = get_sheet()
        return ws.get_all_records()
    except Exception as e:
        print(f"Load history error: {e}")
        return []

async def save_result(answers):
    """Save quiz results to Google Sheets. Runs sync gspread in thread executor."""
    def _do_save():
        ws = get_sheet()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [[today, a["topic"], a["type"], str(a["correct"])] for a in answers]
        ws.append_rows(rows)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_save)

# ─── Stats helpers ─────────────────────────────────────────────────────────────

def calc_streak(history):
    if not history:
        return 0, 0
    dates = sorted(set(r["date"] for r in history if r.get("date")))
    if not dates:
        return 0, 0
    best = cur = 1
    for i in range(1, len(dates)):
        diff = (datetime.strptime(dates[i], "%Y-%m-%d") -
                datetime.strptime(dates[i-1], "%Y-%m-%d")).days
        if diff == 1:
            cur += 1
            best = max(best, cur)
        elif diff > 1:
            cur = 1
    today = datetime.now().strftime("%Y-%m-%d")
    last = dates[-1]
    diff = (datetime.strptime(today, "%Y-%m-%d") -
            datetime.strptime(last, "%Y-%m-%d")).days
    current = cur if diff <= 1 else 0
    return current, best

def topic_stats_last_n_days(history, n=7):
    cutoff = (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")
    recent = [r for r in history if r.get("date", "") >= cutoff]
    stats = {}
    for r in recent:
        t = r.get("topic", "")
        if not t:
            continue
        if t not in stats:
            stats[t] = {"correct": 0, "total": 0}
        stats[t]["total"] += 1
        if str(r.get("correct", "")) == "True":
            stats[t]["correct"] += 1
    return stats

def days_since_last_session(history):
    if not history:
        return 99
    dates = [r["date"] for r in history if r.get("date")]
    if not dates:
        return 99
    last = max(dates)
    return (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days

# ─── Claude prompt ─────────────────────────────────────────────────────────────

def build_prompt(history):
    stats = topic_stats_last_n_days(history, 7)
    days_away = days_since_last_session(history)

    hist_lines = []
    for t, s in stats.items():
        pct = round(s["correct"] / s["total"] * 100) if s["total"] else 0
        hist_lines.append(f"- {t}: {pct}% ({s['total']} вопросов)")
    hist_summary = "\n".join(hist_lines) if hist_lines else "История пуста."

    review_note = ""
    if days_away >= 2:
        review_note = (
            "ВАЖНО: ученик не занимался более 2 дней. "
            "Первые 8 вопросов строго из уже пройденного материала (повторение). "
            "Только после них переходи к новому."
        )

    # days until mid-May 2025
    exam_date = datetime(2025, 5, 15)
    days_left = max((exam_date - datetime.now()).days, 0)
    pre_exam_note = ""
    if days_left <= 30:
        pre_exam_note = (
            "ПРЕДЭКЗАМЕНАЦИОННЫЙ РЕЖИМ: добавь 6 вопросов в формате "
            "короткий текст или диалог на греческом (3-5 строк) + вопрос на понимание прочитанного."
        )

    return f"""Ты генератор вопросов для ежедневного квиза по греческому языку уровней A1-A2.
Ученик: Артем, 36 лет, Лимассол. Родной язык: русский. Английский: хорошо.
Цель: сдать официальный экзамен A2 по современному стандартному греческому языку на Кипре в середине мая 2025.
До экзамена: {days_left} дней.

КРИТИЧЕСКИ ВАЖНО:
- Только стандартный современный греческий язык (νέα ελληνική γλώσσα).
- Никакого кипрского диалекта, кипрских слов, кипрского произношения.
- Артем не использует греческую клавиатуру. Все вопросы только с вариантами ответа, без ввода текста.

{review_note}
{pre_exam_note}

История ученика за последние 7 дней:
{hist_summary}

Приоритеты при подборе тем:
- Темы с результатом ниже 60 процентов = 40 процентов вопросов (повторение слабых мест)
- Темы с результатом выше 85 процентов = 15 процентов вопросов
- Остальное = новый материал по программе A1-A2

Полный перечень тем для покрытия (все темы должны встречаться со временем):
ГЛАГОЛЫ: είμαι, έχω, θέλω, κάνω, πάω, μπορώ, ξέρω, βλέπω, τρώω, πίνω, μιλάω, λέω, μένω, δουλεύω, αγοράζω, πληρώνω, παίρνω, δίνω, ανοίγω, κλείνω, αρχίζω, τελειώνω
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

Типы вопросов - строго вперемешку, примерно поровну:
1. ru_to_gr - перевод с русского на греческий: "Как сказать по-гречески: «Я хочу кофе»?" - 4 варианта на греческом
2. gr_to_ru - перевод с греческого на русский: "Что означает фраза «Πού είναι η στάση;»?" - 4 варианта на русском
3. choose_form - выбор правильной формы: "Вижу ___ (красивая женщина)" - 4 варианта на греческом с разными артиклями, падежами или окончаниями
4. fill_blank - вставить слово в предложение: "Εγώ ___ στην Αθήνα." - 4 варианта на греческом

Сгенерируй СТРОГО 20 вопросов. Верни ТОЛЬКО валидный JSON без markdown, без пояснений вне JSON.

Каждый объект в массиве:
{{
  "question": "текст вопроса на русском языке",
  "options": ["вариант1", "вариант2", "вариант3", "вариант4"],
  "correctIndex": 2,
  "explanation": "пояснение почему этот вариант правильный - полными словами без сокращений, 1-2 предложения на русском языке",
  "topic": "название темы",
  "type": "ru_to_gr | gr_to_ru | choose_form | fill_blank"
}}

Требования к пояснениям:
- Пиши полными словами, без грамматических сокращений (не 'им.п.' а 'именительный падеж', не 'ед.ч.' а 'единственное число', не 'муж.р.' а 'мужской род').
- Объясни конкретное правило которое применяется в этом вопросе.
- 1-2 предложения, не больше.

Варианты ответа должны быть перемешаны случайным образом — correctIndex указывает реальную позицию правильного варианта в массиве.
Неправильные варианты должны быть правдоподобными - похожие формы, близкие слова, частые ошибки."""

def generate_questions(history):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = build_prompt(history)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system="Ты генератор JSON-вопросов для квиза. Отвечай ТОЛЬКО валидным JSON без markdown и пояснений.",
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    start = raw.index("[")
    end = raw.rindex("]")
    questions = json.loads(raw[start:end+1])

    # Server-side shuffle: guarantees correct answer is NOT always in position 0,
    # regardless of what Claude returned.
    for q in questions:
        correct_text = q["options"][q["correctIndex"]]
        random.shuffle(q["options"])
        q["correctIndex"] = q["options"].index(correct_text)

    return questions

# ─── Session storage ───────────────────────────────────────────────────────────

user_sessions = {}

# ─── Handlers ──────────────────────────────────────────────────────────────────

TYPE_LABELS = {
    "ru_to_gr": "🇷🇺 → 🇬🇷 Перевод",
    "gr_to_ru": "🇬🇷 → 🇷🇺 Перевод",
    "choose_form": "📝 Выбор формы",
    "fill_blank": "✏️ Заполни пропуск",
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎯 Начать квиз", callback_data="menu_quiz")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("ℹ️ О боте", callback_data="menu_about")],
    ]
    text = (
        "👋 Привет! Я твой тренер по греческому языку.\n\n"
        "Каждый день я генерирую новый квиз из 20 вопросов, "
        "адаптированный под твой уровень и историю ответов.\n\n"
        "🎯 Цель: подготовка к экзамену A2 по современному греческому языку."
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎯 Начать квиз", callback_data="menu_quiz")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("ℹ️ О боте", callback_data="menu_about")],
    ]
    await update.message.reply_text("📋 Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_quiz(update.message, update.effective_user.id)

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "menu_quiz":
        await query.message.reply_text("⏳ Запускаю квиз...")
        await start_quiz(query.message, query.from_user.id)

    elif action == "menu_stats":
        await show_stats(query.message, query.from_user.id)

    elif action == "menu_about":
        text = (
            "📖 <b>О боте</b>\n\n"
            "Этот бот помогает готовиться к экзамену A2 по современному греческому языку.\n\n"
            "<b>Как работает:</b>\n"
            "• Каждый день генерируется новый квиз из 20 вопросов\n"
            "• Вопросы подбираются на основе твоей истории ответов\n"
            "• Слабые темы повторяются чаще\n"
            "• После каждого ответа объясняется правило\n\n"
            "<b>Команды:</b>\n"
            "/quiz — начать квиз\n"
            "/stats — статистика\n"
            "/menu — главное меню"
        )
        await query.message.reply_text(text, parse_mode="HTML")

async def start_quiz(message, user_id):
    msg = await message.reply_text("⏳ Готовлю квиз... Это займет около 15 секунд.")
    try:
        history = load_history()
        questions = generate_questions(history)
        user_sessions[user_id] = {
            "questions": questions,
            "current": 0,
            "answers": [],
            "awaiting": True,
            "history": history,
        }
        await msg.delete()
        await send_question(message, user_id)
    except Exception as e:
        await msg.edit_text(
            f"❌ Не удалось загрузить квиз: {e}\n\nПопробуй ещё раз через /quiz"
        )

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

    text = (
        f"<b>Вопрос {num} из {total}</b>  •  {type_label}\n"
        f"📌 <i>Тема: {h(q['topic'])}</i>\n\n"
        f"❓ {h(q['question'])}"
    )
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if query.data.startswith("menu_"):
        await handle_menu(update, context)
        return

    if not query.data.startswith("ans_"):
        await query.answer()
        return

    if user_id not in user_sessions:
        await query.answer("Сессия истекла. Напиши /quiz чтобы начать заново.")
        return

    session = user_sessions[user_id]
    if not session.get("awaiting"):
        await query.answer()
        return

    session["awaiting"] = False
    selected = int(query.data.split("_")[1])
    q = session["questions"][session["current"]]
    correct = selected == q["correctIndex"]

    session["answers"].append({
        "topic": q["topic"],
        "type": q["type"],
        "correct": correct,
    })

    correct_letter = LETTERS[q["correctIndex"]]
    correct_text = q["options"][q["correctIndex"]]

    if correct:
        result = (
            f"✅ <b>Верно!</b>\n\n"
            f"<b>{h(correct_letter)}. {h(correct_text)}</b>\n\n"
            f"💡 {h(q['explanation'])}"
        )
    else:
        selected_letter = LETTERS[selected]
        selected_text = q["options"][selected]
        result = (
            f"❌ <b>Неверно.</b>\n\n"
            f"Твой ответ: {h(selected_letter)}. {h(selected_text)}\n"
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

    await query.answer()

async def finish_quiz(message, user_id):
    session = user_sessions[user_id]
    answers = session["answers"]
    history = session.get("history", [])

    correct_count = sum(1 for a in answers if a["correct"])
    total = len(answers)
    pct = round(correct_count / total * 100)

    topic_stats = {}
    for a in answers:
        t = a["topic"]
        if t not in topic_stats:
            topic_stats[t] = {"correct": 0, "total": 0}
        topic_stats[t]["total"] += 1
        if a["correct"]:
            topic_stats[t]["correct"] += 1

    weak = sorted(
        [(t, round(s["correct"] / s["total"] * 100)) for t, s in topic_stats.items()],
        key=lambda x: x[1]
    )[:3]

    streak_cur, streak_best = calc_streak(history)
    new_streak = streak_cur + 1

    if pct >= 80:
        emoji, label = "🎉", "Отлично!"
        stars = "⭐⭐⭐⭐⭐" if pct >= 95 else "⭐⭐⭐⭐"
    elif pct >= 60:
        emoji, label = "👍", "Хороший результат!"
        stars = "⭐⭐⭐"
    else:
        emoji, label = "💪", "Нужно повторить."
        stars = "⭐⭐" if pct >= 40 else "⭐"

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

    # Save results — errors are surfaced to user instead of being silently swallowed
    try:
        await save_result(answers)
    except Exception as e:
        print(f"Save error: {e}")
        await message.reply_text(
            f"⚠️ <b>Не удалось сохранить результаты в Google Sheets:</b>\n<code>{h(str(e))}</code>\n\n{text}",
            parse_mode="HTML"
        )
        del user_sessions[user_id]
        return

    del user_sessions[user_id]
    await message.reply_text(text, parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update.message, update.effective_user.id)

async def show_stats(message, user_id):
    history = load_history()
    if not history:
        await message.reply_text(
            "📊 Статистика пока пустая. Пройди первый квиз через /quiz"
        )
        return

    streak_cur, streak_best = calc_streak(history)
    total_sessions = len(set(r["date"] for r in history if r.get("date")))
    total_questions = len(history)
    total_correct = sum(1 for r in history if str(r.get("correct", "")) == "True")
    overall_pct = round(total_correct / total_questions * 100) if total_questions else 0

    stats_7 = topic_stats_last_n_days(history, 7)
    weak_topics = sorted(
        [(t, round(s["correct"] / s["total"] * 100)) for t, s in stats_7.items() if s["total"] >= 3],
        key=lambda x: x[1]
    )[:5]
    strong_topics = sorted(
        [(t, round(s["correct"] / s["total"] * 100)) for t, s in stats_7.items() if s["total"] >= 3],
        key=lambda x: -x[1]
    )[:3]

    exam_date = datetime(2025, 5, 15)
    days_left = max((exam_date - datetime.now()).days, 0)

    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"📅 До экзамена: <b>{days_left} дней</b>\n"
        f"🔥 Серия дней: {streak_cur} (рекорд: {streak_best})\n"
        f"📝 Всего сессий: {total_sessions}\n"
        f"❓ Всего вопросов: {total_questions}\n"
        f"✅ Общий результат: <b>{overall_pct}%</b>\n"
    )
    if weak_topics:
        text += "\n⚠️ <b>Слабые темы (последние 7 дней):</b>\n"
        for t, p in weak_topics:
            text += f"  • {h(t)}: {p}%\n"
    if strong_topics:
        text += "\n💪 <b>Сильные темы:</b>\n"
        for t, p in strong_topics:
            text += f"  • {h(t)}: {p}%\n"

    await message.reply_text(text, parse_mode="HTML")

# ─── Main ───────────────────────────────────────────────────────────────────────

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("quiz", "Начать квиз"),
        BotCommand("stats", "Моя статистика"),
        BotCommand("menu", "Главное меню"),
    ])

def main():
    app = Application.builder().token(TG_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(handle_answer))
    app.run_polling()

if __name__ == "__main__":
    main()
