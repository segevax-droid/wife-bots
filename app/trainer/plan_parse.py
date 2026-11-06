import json
import re
import uuid
from typing import Any, Dict, List, Optional

from app.gemini import call_gemini
from app.trainer.exercise_db import ExerciseDB


async def parse_workout_plan(text: str, db: ExerciseDB) -> Dict[str, Any]:
    prompt = f'''Преобразуй план тренировки в JSON. Только JSON без markdown.
Формат: {{"exercises":[{{"name":"...", "sets":3, "reps_target":10, "weight_kg":null}}]}}
Имена упражнений на русском как в тексте. weight_kg число или null.
Текст плана:
{text}
'''
    raw = await call_gemini(prompt)
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except Exception:
        return {"exercises": [], "error": "parse"}
    out: List[Dict[str, Any]] = []
    for ex in data.get("exercises", []):
        name = str(ex.get("name", "")).strip()
        if not name:
            continue
        matched = db.match(name)
        if matched:
            eid = matched["id"]
            ename = matched["name"]
        else:
            sim = db.top_similar(name, 1)
            if not sim:
                continue
            eid = sim[0]["id"]
            ename = sim[0]["name"]
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
    return {"exercises": out}


def plan_to_state(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "plan_id": str(uuid.uuid4())[:8],
        "exercises": plan.get("exercises", []),
    }
