import json
import re
import uuid
from typing import Any, Dict, List, Optional

from app.gemini import call_gemini
from app.trainer.exercise_db import ExerciseDB


def _map_exercises(data: dict, db: ExerciseDB) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ex in data.get("exercises", []):
        name = str(ex.get("name", "")).strip()
        if not name:
            continue
        matched = db.match(name)
        if matched:
            eid, ename = matched["id"], matched["name"]
        else:
            sim = db.top_similar(name, 1)
            if not sim:
                continue
            eid, ename = sim[0]["id"], sim[0]["name"]
        sets = int(ex.get("sets", 3) or 3)
        reps = int(ex.get("reps_target", 10) or 10)
        w = ex.get("weight_kg")
        try:
            w = float(w) if w is not None else None
        except (TypeError, ValueError):
            w = None
        out.append(
            {
                "exercise_id": eid,
                "name": ename,
                "original": name,
                "sets": max(1, min(sets, 12)),
                "reps_target": max(1, min(reps, 50)),
                "weight_kg": w,
            }
        )
    return out


def _parse_json_obj(raw: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


async def parse_workout_plan(text: str, db: ExerciseDB) -> Dict[str, Any]:
    prompt = f'''Преобразуй план тренировки в JSON. Только JSON без markdown.
Формат: {{"exercises":[{{"name":"...", "sets":3, "reps_target":10, "weight_kg":null}}]}}
Имена упражнений на русском как в тексте. weight_kg число или null.
Текст плана:
{text}
'''
    raw = await call_gemini(prompt)
    if raw.startswith("⚠️") or raw.startswith("⏳"):
        return {"exercises": [], "error": raw}
    data = _parse_json_obj(raw)
    if not data:
        return {"exercises": [], "error": "parse"}
    return {"exercises": _map_exercises(data, db)}


async def generate_workout_plan(db: ExerciseDB, hint: str = "") -> Dict[str, Any]:
    """AI builds a full-body plan; names must come from the provided catalog only."""
    catalog = []
    for ex in list(db.by_id.values())[:80]:
        catalog.append({"id": ex["id"], "name": ex["name"], "group": ex.get("group", "")})
    prompt = f'''Ты тренер. Составь план на 1 тренировку (5–8 упражнений) из каталога.
Выбирай только name из каталога. Только JSON без markdown:
{{"exercises":[{{"name":"...","sets":3,"reps_target":10,"weight_kg":null}}]}}
Подсказка пользователя: {hint or "общее развитие силы, без травм"}
Каталог: {json.dumps(catalog, ensure_ascii=False)[:6000]}
'''
    raw = await call_gemini(prompt)
    if raw.startswith("⚠️") or raw.startswith("⏳"):
        return {"exercises": [], "error": raw}
    data = _parse_json_obj(raw)
    if not data:
        return {"exercises": [], "error": "parse"}
    return {"exercises": _map_exercises(data, db)}


def plan_to_state(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "plan_id": str(uuid.uuid4())[:8],
        "exercises": plan.get("exercises", []),
    }
