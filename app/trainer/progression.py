"""Прогрессия 1–10 (тяжесть подхода/упражнения): правила для следующей тренировки."""

from typing import Any, Dict, Optional, Tuple

from app.config import DIFF_EASY_MAX, DIFF_HARD_MAX, DIFF_MODERATE_MAX

# 1–4: легко → +2.5% вес ИЛИ +1–2 повтора на сет
# 5–7: тот же вес +1 повтор
# 8–9: вес без добавления повторов (подразумевается +2.5% если есть вес)
# 10 или боль: deload -10% или замена варианта из базы

DELTA_WEIGHT_PCT = 0.025
DELOAD_PCT = 0.10


def next_load_hint(
    avg_difficulty: float,
    pain: bool,
    has_weight: bool,
    last_weight: Optional[float],
    last_reps: Optional[int],
) -> Tuple[str, Dict[str, Any]]:
    if pain or avg_difficulty >= 10:
        return (
            "deload_or_swap",
            {
                "why": "Боль или максимальная тяжесть (10): разгрузка -10% или аналог из базы.",
                "weight_mult": 1.0 - DELOAD_PCT,
            },
        )
    d = avg_difficulty
    if d <= DIFF_EASY_MAX:
        return (
            "easy",
            {
                "why": f"Лёгкая зона (≤{DIFF_EASY_MAX}): +{DELTA_WEIGHT_PCT*100:.1f}% к весу или +1–2 повтора.",
                "weight_mult": 1.0 + DELTA_WEIGHT_PCT if has_weight else 1.0,
                "reps_add": 1,
            },
        )
    if d <= DIFF_MODERATE_MAX:
        return (
            "moderate",
            {
                "why": f"Средняя зона ({DIFF_EASY_MAX+1}–{DIFF_MODERATE_MAX}): тот же вес, +1 повтор.",
                "weight_mult": 1.0,
                "reps_add": 1,
            },
        )
    if d <= DIFF_HARD_MAX:
        return (
            "hard",
            {
                "why": f"Тяжёлая зона ({DIFF_MODERATE_MAX+1}–{DIFF_HARD_MAX}): прирост веса ~{DELTA_WEIGHT_PCT*100:.1f}%, повторы стабильно.",
                "weight_mult": 1.0 + DELTA_WEIGHT_PCT if has_weight else 1.0,
                "reps_add": 0,
            },
        )
    return (
        "very_hard",
        {
            "why": f"Очень тяжело (9): следующий раз добавить вес осторожно или удержать объём.",
            "weight_mult": 1.0 + (DELTA_WEIGHT_PCT / 2) if has_weight else 1.0,
            "reps_add": 0,
        },
    )
