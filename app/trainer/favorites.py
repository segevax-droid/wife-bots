import json
import logging
from typing import List, Set

from app.state import SYSTEM_STATE, save_state_pairs

logger = logging.getLogger("wife_bots")

MAX_FAVORITES = 15
FAV_STATE_KEY = "fav_exercise_ids"


def load_favorite_ids() -> Set[str]:
    raw = SYSTEM_STATE.get(FAV_STATE_KEY) or "[]"
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(x) for x in data if x}
    except Exception:
        pass
    return set()


def is_favorite(exercise_id: str) -> bool:
    return str(exercise_id) in load_favorite_ids()


async def toggle_favorite(exercise_id: str) -> tuple[bool, str]:
    """Returns (now_favorite, user_message)."""
    eid = str(exercise_id).strip()
    if not eid:
        return False, "Нет id упражнения."
    fav = load_favorite_ids()
    if eid in fav:
        fav.discard(eid)
        await save_state_pairs({FAV_STATE_KEY: json.dumps(sorted(fav), ensure_ascii=False)})
        return False, "Убрала из любимых."
    if len(fav) >= MAX_FAVORITES:
        return (
            True,
            f"В любимых уже {MAX_FAVORITES} — сними звезду с другого (меню «Любимые упражнения»).",
        )
    fav.add(eid)
    await save_state_pairs({FAV_STATE_KEY: json.dumps(sorted(fav), ensure_ascii=False)})
    return True, "Добавила в любимые — буду чуть чаще подставлять в план."


async def remove_favorite(exercise_id: str) -> None:
    fav = load_favorite_ids()
    fav.discard(str(exercise_id))
    await save_state_pairs({FAV_STATE_KEY: json.dumps(sorted(fav), ensure_ascii=False)})


def favorite_ids_list() -> List[str]:
    return sorted(load_favorite_ids())
