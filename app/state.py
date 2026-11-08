import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

from app.config import WIFE_TZ

logger = logging.getLogger("wife_bots")

try:
    LOCAL_TZ = ZoneInfo(WIFE_TZ)
except Exception:
    LOCAL_TZ = ZoneInfo("Europe/Moscow")


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


SYSTEM_STATE: Dict[str, Any] = {
    "chat_Trainer": None,
    "chat_Nutrition": None,
    "plan_last_json": "",
    "last_session_date": "",
    "morning_weigh_done_date": "",
    "onboarding_complete": "0",
    "pending_workout_bonus_kcal": "0",
    "pending_workout_bonus_date": "",
    "last_norms_weight": "",
    "last_weight_notify_kg": "",
}


async def load_state_from_sheet() -> None:
    from app.sheets import get_worksheet, get_ws_values

    ws = await get_worksheet("Состояние")
    if not ws:
        return
    rows = await get_ws_values(ws)
    for row in rows:
        if len(row) >= 2 and str(row[0]).strip():
            key, val = str(row[0]).strip(), str(row[1]).strip()
            if key in SYSTEM_STATE or key.startswith("chat_") or key.startswith("remind_"):
                SYSTEM_STATE[key] = val
    for k in ("chat_Trainer", "chat_Nutrition"):
        if SYSTEM_STATE.get(k):
            try:
                SYSTEM_STATE[k] = int(SYSTEM_STATE[k])
            except (TypeError, ValueError):
                pass


async def save_state_pairs(pairs: Dict[str, str]) -> bool:
    from app.sheets import (
        sheet_write_lock,
        get_worksheet,
        get_ws_values,
        _update_sheet_cell_unlocked,
        _append_row_unlocked,
    )

    # Must use _append_row_unlocked here: append_row_to_sheet re-acquires the same
    # asyncio.Lock and deadlocks (Lock is not reentrant).
    async with sheet_write_lock("Состояние"):
        ws = await get_worksheet("Состояние")
        if not ws:
            return False
        rows = await get_ws_values(ws)
        index: Dict[str, int] = {}
        for idx, row in enumerate(rows, start=1):
            if row and str(row[0]).strip():
                index[str(row[0]).strip()] = idx
        ok = True
        for key, value in pairs.items():
            row_idx = index.get(key)
            if row_idx:
                if not await _update_sheet_cell_unlocked("Состояние", row_idx, 2, value):
                    ok = False
            else:
                w_ok, err = await _append_row_unlocked("Состояние", [key, value])
                if not w_ok:
                    ok = False
                    logger.error("state append %s: %s", key, err)
                else:
                    index[key] = len(index) + 2  # best-effort; cache invalidated on append
        for k, v in pairs.items():
            SYSTEM_STATE[k] = v
        return ok


def chat_id_trainer() -> Optional[int]:
    v = SYSTEM_STATE.get("chat_Trainer")
    return int(v) if v is not None and str(v).isdigit() else None


def chat_id_nutrition() -> Optional[int]:
    v = SYSTEM_STATE.get("chat_Nutrition")
    return int(v) if v is not None and str(v).isdigit() else None
