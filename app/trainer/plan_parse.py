import json
import re
import uuid
from typing import Any, Dict, List, Optional

from app.gemini import call_gemini
from app.trainer.exercise_db import ExerciseDB
from app.trainer.program import (
    build_cycle,
    resolve_today_from_cycle,
)


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


async def _coach_fields_for_plan(mapped: List[Dict[str, Any]], source: str = "") -> Dict[str, str]:
    names = ", ".join(e["name"] for e in mapped[:10])
    prompt = (
        "Ты дружелюбный тренер. По списку упражнений верни ТОЛЬКО JSON без markdown: "
        '{"title":"короткое название тренировки","warmup_note":"2-3 предложения про разминку",'
        '"coach_note":"1-2 предложения мотивации и логики плана"}. '
        f"Упражнения: {names}. Контекст: {source[:500]}"
    )
    raw = await call_gemini(prompt)
    if raw.startswith("⚠️") or raw.startswith("⏳"):
        return {}
    data = _parse_json_obj(raw)
    return {
        k: str(data[k]).strip()
        for k in ("title", "warmup_note", "coach_note")
        if data.get(k)
    }


async def parse_workout_plan(text: str, db: ExerciseDB) -> Dict[str, Any]:
    prompt = f'''Преобразуй план тренировки в JSON. Только JSON без markdown.
Формат: {{
  "title":"название",
  "warmup_note":"краткая разминка",
  "coach_note":"зачем такой порядок",
  "exercises":[{{"name":"...", "sets":3, "reps_target":10, "weight_kg":null}}]
}}
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
    mapped = _map_exercises(data, db)
    out: Dict[str, Any] = {"exercises": mapped}
    for k in ("title", "warmup_note", "coach_note"):
        if data.get(k):
            out[k] = str(data[k]).strip()
    if mapped and not out.get("coach_note"):
        out.update(await _coach_fields_for_plan(mapped, text[:400]))
    return out


def _local_fullbody_plan(db: ExerciseDB) -> Dict[str, Any]:
    pool = db.real_named()
    by_g: Dict[str, list] = {}
    for ex in pool:
        by_g.setdefault(ex.get("group") or "Прочее", []).append(ex)
    picked = []
    for g in ("Ноги", "Спина", "Грудь", "Плечи", "Руки", "Кор", "Пресс", "Ягодицы"):
        if by_g.get(g):
            picked.append(by_g[g][0])
        if len(picked) >= 6:
            break
    if len(picked) < 5:
        for ex in pool:
            if ex not in picked:
                picked.append(ex)
            if len(picked) >= 6:
                break
    return {
        "title": "Силовой full body",
        "warmup_note": "5–7 минут: круговые движения суставами, лёгкие приседы и махи руками, 1–2 разминочных подхода в первых упражнениях.",
        "coach_note": "Сбалансированный микс: ноги → тяга/спина → жим → плечи/руки — классика для всего тела.",
        "exercises": [
            {
                "exercise_id": e["id"],
                "name": e["name"],
                "original": e["name"],
                "sets": 3,
                "reps_target": 10,
                "weight_kg": 0.0 if ExerciseDB.is_bodyweight(e) else None,
            }
            for e in picked[:8]
        ],
    }


async def generate_workout_plan(
    db: ExerciseDB,
    scheme: str = "full_body",
    *,
    soft_session: bool = False,
) -> Dict[str, Any]:
    """Сборка дня из каталога по сплиту; Gemini — только заголовок и тон."""
    cycle = build_cycle(db, scheme, soft_session=soft_session)
    day = resolve_today_from_cycle(cycle)
    if not day or not day.get("exercises"):
        local = _local_fullbody_plan(db)
        if local["exercises"]:
            local["note"] = "Мало упражнений в базе — упрощённый план."
            local["scheme"] = scheme
            local["split"] = "full"
            local["day_key"] = "FB"
            return local
        return {"exercises": [], "error": "пустой каталог упражнений"}
    mapped = day["exercises"]
    extra = await _coach_fields_for_plan(mapped, f"{scheme} {day.get('split', '')}")
    if extra.get("title"):
        day["title"] = extra["title"]
    if extra.get("warmup_note"):
        day["warmup_note"] = extra["warmup_note"]
    if extra.get("coach_note"):
        day["coach_note"] = extra["coach_note"]
    day["cycle"] = cycle
    return day


async def generate_workout_plan_legacy(db: ExerciseDB) -> Dict[str, Any]:
    return await generate_workout_plan(db, "full_body")


def plan_to_state(plan: Dict[str, Any]) -> Dict[str, Any]:
    st: Dict[str, Any] = {
        "plan_id": str(uuid.uuid4())[:8],
        "exercises": plan.get("exercises", []),
    }
    for k in (
        "title",
        "warmup_note",
        "coach_note",
        "note",
        "split",
        "day_key",
        "scheme",
        "warmup",
    ):
        if plan.get(k):
            st[k] = plan[k]
    return st
