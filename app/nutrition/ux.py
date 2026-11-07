import random
from typing import Dict, Optional

from app.nutrition.norms import progress_bar


def meal_logged_praise(kcal: float, goal: str) -> str:
    if kcal >= 600:
        base = "Сытный приём — запомнила 💛"
    elif kcal <= 120:
        base = "Лёгкий перекус — аккуратно записала ✨"
    else:
        base = random.choice(
            [
                "Записала — ты на шаг ближе к балансу 🍽",
                "Принято! Копилка обновлена 💫",
                "Есть! Считаю дальше вместе с тобой 🌿",
            ]
        )
    if (goal or "").strip() == "cut":
        return base + "\n<i>Белок в приёме — твой друг при похудении.</i>"
    return base


def bank_message(
    p: float,
    f_: float,
    c: float,
    k: float,
    tgt: Dict[str, float],
) -> str:
    ratio = k / tgt["kcal"] if tgt["kcal"] > 0 else 0
    if ratio < 0.35:
        mood = "Утро или лёгкий день — впереди ещё много вкусного 🌅"
    elif ratio < 0.85:
        mood = "Ровный темп — так держать 💛"
    elif ratio <= 1.05:
        mood = "Почти в цели на сегодня — ты молодец ✨"
    else:
        mood = "Чуть выше плана — не ругай себя, завтра новый день 🤍"
    return (
        f"💰 <b>Копилка дня</b>\n"
        f"<i>{mood}</i>\n\n"
        f"Калории {progress_bar(k, tgt['kcal'])}  <b>{k:.0f}</b> / {tgt['kcal']:.0f}\n"
        f"Белки    {progress_bar(p, tgt['p'])}  <b>{p:.0f}</b> / {tgt['p']:.0f} г\n"
        f"Жиры     {progress_bar(f_, tgt['f'])}  <b>{f_:.0f}</b> / {tgt['f']:.0f} г\n"
        f"Углеводы {progress_bar(c, tgt['c'])}  <b>{c:.0f}</b> / {tgt['c']:.0f} г"
    )


def weight_saved_note(delta_msg: str = "") -> str:
    return "Отлично, вес записала — график растёт вместе с тобой 📈" + delta_msg
