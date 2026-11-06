from datetime import date, datetime, timedelta
from typing import Optional

from aiogram import Bot

from app.state import SYSTEM_STATE, load_state_from_sheet, now_local, save_state_pairs
from app.trainer.kb import reminder_train_kb


def _parse_iso(d: str) -> Optional[date]:
    try:
        return date.fromisoformat((d or "").strip()[:10])
    except Exception:
        return None


async def maybe_send_workout_reminders(bot_trainer: Bot) -> None:
    await load_state_from_sheet()
    cid = SYSTEM_STATE.get("chat_Trainer")
    if not cid:
        return
    last = _parse_iso(str(SYSTEM_STATE.get("last_session_date", "")))
    if not last:
        return
    today = now_local().date()
    if (today - last).days < 4:
        return
    nl = now_local()
    slot = None
    if nl.hour == 9 and nl.minute < 2:
        slot = "am"
    elif nl.hour == 19 and nl.minute < 2:
        slot = "pm"
    if not slot:
        return
    key = f"remind_train_{today.isoformat()}_{slot}"
    if SYSTEM_STATE.get(key) == "1":
        return
    text = "Давно не было тренировки. Выбери действие:"
    try:
        await bot_trainer.send_message(int(cid), text, reply_markup=reminder_train_kb())
    except Exception:
        pass
    await save_state_pairs({key: "1"})


async def handle_light_home(bot: Bot, chat_id: int) -> None:
    text = (
        "Лёгкая дома 10–15 мин:\n"
        "1) Разминка суставов 3 мин\n"
        "2) Присед/отжимание/планка по 2×15\n"
        "3) Заминка дыхание 2 мин"
    )
    await bot.send_message(chat_id, text)
