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
    for g in ("Ноги", "Спина", "Грудь", "Плечи", "Руки", "Кор", "Ягодицы", "Пресс"):
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
                "weight_kg": None,
            }
            for e in picked[:8]
        ],
    }


async def generate_workout_plan(db: ExerciseDB, hint: str = "") -> Dict[str, Any]:
    """AI builds a full-body plan; names must come from the provided catalog only."""
    catalog = []
    for ex in db.real_named()[:50]:
        catalog.append({"id": ex["id"], "name": ex["name"], "group": ex.get("group", "")})
    prompt = (
        "Ты дружелюбный тренер. Составь план на 1 тренировку (5–8 упражнений) ТОЛЬКО из каталога. "
        "Бери точные name из каталога, без «упражнение 1/2». "
        "Только JSON без markdown: "
        '{"title":"...","warmup_note":"2-3 предложения","coach_note":"мотивация и логика",'
        '"exercises":[{"name":"...","sets":3,"reps_target":10,"weight_kg":null}]}. '
        f"Подсказка: {hint or 'общее развитие силы'}. "
        f"Каталог: {json.dumps(catalog, ensure_ascii=False)[:4000]}"
    )
    raw = await call_gemini(prompt)
    if not (raw.startswith("⚠️") or raw.startswith("⏳")):
        data = _parse_json_obj(raw)
        mapped = _map_exercises(data, db) if data else []
        if mapped:
            out: Dict[str, Any] = {"exercises": mapped}
            for k in ("title", "warmup_note", "coach_note"):
                if data.get(k):
                    out[k] = str(data[k]).strip()
            if not out.get("coach_note"):
                out.update(await _coach_fields_for_plan(mapped, hint))
            return out
    local = _local_fullbody_plan(db)
    if local["exercises"]:
        local["note"] = "Gemini недоступен — собран базовый план из базы упражнений."
        return local
    return {"exercises": [], "error": raw if raw.startswith("⚠️") else "пустой каталог упражнений"}


def plan_to_state(plan: Dict[str, Any]) -> Dict[str, Any]:
    st: Dict[str, Any] = {
        "plan_id": str(uuid.uuid4())[:8],
        "exercises": plan.get("exercises", []),
    }
    for k in ("title", "warmup_note", "coach_note", "note"):
        if plan.get(k):
            st[k] = plan[k]
    return st
