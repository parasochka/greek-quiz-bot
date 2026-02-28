from datetime import date, timedelta

from topics import build_topic_sequence


def _stats(correct, total, last_seen):
    return {"correct": correct, "total": total, "last_seen": last_seen}


def test_review_first_block_for_returning_user():
    today = date.today()
    session_dates = [
        str(today - timedelta(days=10)),
        str(today - timedelta(days=7)),
        str(today - timedelta(days=3)),
    ]
    stats = {
        "Глаголы": _stats(2, 5, str(today - timedelta(days=3))),
        "Артикли": _stats(3, 6, str(today - timedelta(days=4))),
        "Отрицание": _stats(5, 8, str(today - timedelta(days=6))),
    }

    sequence = build_topic_sequence(stats, session_dates, topic_memory={}, total_questions=20)

    seen_topics = set(stats)
    assert len(sequence) == 20
    assert all(topic in seen_topics for topic in sequence[:8])


def test_memory_priority_outweighs_equal_accuracy():
    today = date.today()
    session_dates = [
        str(today - timedelta(days=8)),
        str(today - timedelta(days=5)),
        str(today - timedelta(days=1)),
    ]
    stats = {
        "Глаголы": _stats(1, 4, str(today - timedelta(days=1))),
        "Артикли": _stats(1, 4, str(today - timedelta(days=1))),
    }
    topic_memory = {
        "Глаголы": {
            "mastery": 0.15,
            "stability": 2.0,
            "due_at": str(today - timedelta(days=4)),
            "last_seen": str(today - timedelta(days=10)),
            "review_count": 10,
            "lapses": 5,
        },
        "Артикли": {
            "mastery": 0.80,
            "stability": 10.0,
            "due_at": str(today + timedelta(days=3)),
            "last_seen": str(today - timedelta(days=1)),
            "review_count": 10,
            "lapses": 0,
        },
    }

    sequence = build_topic_sequence(stats, session_dates, topic_memory=topic_memory, total_questions=6)

    assert sequence[0] == "Глаголы"
