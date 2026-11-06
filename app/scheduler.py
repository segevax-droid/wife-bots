import asyncio
import logging
import re
from typing import Optional, Tuple

from aiogram import Bot

from app.config import WIFE_MEAL_REMINDER_TIMES
from app.nutrition.norms import recalc_norms_for_user
from app.nutrition.weight import morning_weigh_prompt_kb
from app.sheets import get_settings_map
from app.state import SYSTEM_STATE, load_state_from_sheet, now_local, save_state_pairs
from app.trainer.reminders import maybe_send_workout_reminders

logger = logging.getLogger("wife_bots")


def _parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s or "")
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mm <= 59:
        return h, mm
    return None


async def _tick_morning_weigh(bot_nutrition: Bot) -> None:
    await load_state_from_sheet()
    cid = SYSTEM_STATE.get("chat_Nutrition")
    if not cid:
        return
    day = now_local().strftime("%Y-%m-%d")
    key = f"remind_weigh_{day}"
    if SYSTEM_STATE.get(key) == "1":
        return
    try:
        await bot_nutrition.send_message(int(cid), "Утро. Взвесься и пришли вес (число или фото весов).", reply_markup=morning_weigh_prompt_kb())
    except Exception as e:
        logger.warning("morning weigh: %s", e)
    await save_state_pairs({key: "1"})


async def _tick_meal_reminders(bot_nutrition: Bot, hh: int, mm: int) -> None:
    if not WIFE_MEAL_REMINDER_TIMES:
        return
    cid = SYSTEM_STATE.get("chat_Nutrition")
    if not cid:
        return
    for raw in WIFE_MEAL_REMINDER_TIMES:
        t = _parse_hhmm(raw)
        if not t:
            continue
        if t != (hh, mm):
            continue
        key = f"remind_meal_{now_local().strftime('%Y-%m-%d')}_{t[0]:02d}{t[1]:02d}"
        if SYSTEM_STATE.get(key) == "1":
            continue
        try:
            await bot_nutrition.send_message(int(cid), "Напоминание: запиши приём пищи.")
        except Exception:
            pass
        await save_state_pairs({key: "1"})


async def _daily_norms(bot_nutrition: Bot) -> None:
    await load_state_from_sheet()
    key = f"remind_norms_{now_local().strftime('%Y-%m-%d')}"
    if SYSTEM_STATE.get(key) == "1":
        return
    settings = await get_settings_map()
    msg = await recalc_norms_for_user(settings, reason="daily")
    cid = SYSTEM_STATE.get("chat_Nutrition")
    if cid and msg:
        try:
            await bot_nutrition.send_message(int(cid), msg)
        except Exception:
            pass
    await save_state_pairs({key: "1"})


async def background_loop(bot_trainer: Bot, bot_nutrition: Bot) -> None:
    while True:
        try:
            await load_state_from_sheet()
            nl = now_local()
            hh, mm = nl.hour, nl.minute
            if hh == 8 and mm == 0:
                await _tick_morning_weigh(bot_nutrition)
            await _tick_meal_reminders(bot_nutrition, hh, mm)
            if hh == 3 and mm == 0:
                await _daily_norms(bot_nutrition)
            await maybe_send_workout_reminders(bot_trainer)
        except Exception as e:
            logger.warning("scheduler: %s", e)
        await asyncio.sleep(30)
