import difflib
import random
from datetime import date


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
    """Map API-returned topic to the nearest canonical MASTER_TOPICS name."""
    if topic in MASTER_TOPICS:
        return topic
    matches = difflib.get_close_matches(topic, MASTER_TOPICS, n=1, cutoff=0.6)
    return matches[0] if matches else topic


def build_topic_sequence(
    stats: dict,
    session_dates: list,
    topic_memory: dict,
    total_questions: int,
) -> list[str]:
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

    # Final safety net for both modes: fill remaining slots from all topics
    if len(sequence) < total_questions:
        fill_from_pool(MASTER_TOPICS, total_questions - len(sequence), weakest_first=True)

    random.shuffle(sequence)
    return sequence[:total_questions]
