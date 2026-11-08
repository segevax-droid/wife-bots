"""Детерминированный подбор упражнений по сплиту, истории и любимым."""

import json
import random
from typing import Any, Dict, List, Optional, Set, Tuple

from app.state import SYSTEM_STATE
from app.trainer.exercise_db import ExerciseDB
from app.trainer.favorites import load_favorite_ids

HISTORY_KEY = "plan_history_json"
CYCLE_KEY = "plan_cycle_json"

SCHEME_LABELS = {
    "full_body": "Full body",
    "upper_lower": "Верх / низ",
}

DAY_TEMPLATES: Dict[str, List[Dict[str, Any]]] = {
    "full_body": [
        {
            "day_key": "FB",
            "split": "full",
            "title_base": "Full body",
            "slots": [
                {"groups": ["Ноги"], "role": "primary", "sets": 3, "reps": 8},
                {"groups": ["Спина"], "role": "primary", "sets": 3, "reps": 10},
                {"groups": ["Грудь"], "role": "primary", "sets": 3, "reps": 10},
                {"groups": ["Плечи"], "role": "aux", "sets": 3, "reps": 12},
                {"groups": ["Ягодицы", "Ноги"], "role": "aux", "sets": 3, "reps": 12},
                {"groups": ["Кор", "Пресс"], "role": "isolation", "sets": 3, "reps": 15},
            ],
        }
    ],
    "upper_lower": [
        {
            "day_key": "A",
            "split": "lower",
            "title_base": "Низ",
            "slots": [
                {"groups": ["Ноги"], "role": "primary", "sets": 3, "reps": 8},
                {"groups": ["Ягодицы"], "role": "primary", "sets": 3, "reps": 10},
                {"groups": ["Ноги"], "role": "aux", "sets": 3, "reps": 10},
                {"groups": ["Ноги", "Ягодицы"], "role": "aux", "sets": 3, "reps": 12},
                {"groups": ["Кор", "Пресс"], "role": "isolation", "sets": 3, "reps": 15},
            ],
        },
        {
            "day_key": "B",
            "split": "upper",
            "title_base": "Верх",
            "slots": [
                {"groups": ["Спина"], "role": "primary", "sets": 3, "reps": 8},
                {"groups": ["Грудь"], "role": "primary", "sets": 3, "reps": 10},
                {"groups": ["Спина"], "role": "aux", "sets": 3, "reps": 10},
                {"groups": ["Плечи"], "role": "aux", "sets": 3, "reps": 12},
                {"groups": ["Руки"], "role": "isolation", "sets": 3, "reps": 12},
            ],
        },
    ],
}

WARMUP_BY_SPLIT = {
    "lower": {
        "cardio": "3–4 мин вело или быстрая ходьба, дыхание ровное.",
        "mobility": "колени, таз, поясница; круги в суставах.",
        "minutes": 6,
    },
    "upper": {
        "cardio": "3–4 мин гребля / дорожка или 2 мин лёгкая гимнастика.",
        "mobility": "плечи, лопатки, грудной отдел.",
        "minutes": 6,
    },
    "full": {
        "cardio": "4–5 мин лёгкого кардио (разговорный темп).",
        "mobility": "все основные суставы; 1–2 разминочных подхода в первом упражнении.",
        "minutes": 7,
    },
}


def load_history() -> List[Dict[str, Any]]:
    raw = SYSTEM_STATE.get(HISTORY_KEY) or "[]"
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def history_ids_for_day(day_key: str, last_n: int = 3) -> Set[str]:
    out: Set[str] = set()
    for row in reversed(load_history()):
        if row.get("day_key") != day_key:
            continue
        for eid in row.get("exercise_ids") or []:
            out.add(str(eid))
        last_n -= 1
        if last_n <= 0:
            break
    return out


def last_session_ids() -> Set[str]:
    hist = load_history()
    if not hist:
        return set()
    return {str(x) for x in (hist[-1].get("exercise_ids") or [])}


def _role_of(ex: Dict[str, Any]) -> str:
    typ = (ex.get("type") or "").lower()
    name = (ex.get("name") or "").lower()
    if "базов" in typ or "compound" in typ or "основ" in typ:
        return "primary"
    if "изол" in typ or "isolation" in typ:
        return "isolation"
    if any(w in name for w in ("присед", "тяг", "жим", "подтяг", "отжим")):
        return "primary"
    return "aux"


def _pool_for_slot(db: ExerciseDB, groups: List[str], role: str) -> List[Dict[str, Any]]:
    pool: List[Dict[str, Any]] = []
    for ex in db.real_named():
        g = (ex.get("group") or "").strip()
        if groups and g not in groups:
            continue
        r = _role_of(ex)
        if role == "primary" and r not in ("primary", "aux"):
            continue
        if role == "aux" and r == "isolation":
            continue
        if role == "isolation" and r == "primary":
            continue
        pool.append(ex)
    if not pool and groups:
        for ex in db.real_named():
            if (ex.get("group") or "").strip() in groups:
                pool.append(ex)
    return pool


def _score_ex(
    ex: Dict[str, Any],
    *,
    history: Set[str],
    last_sess: Set[str],
    fav: Set[str],
    fav_count: int,
    max_fav: int,
    role: str,
    soft_session: bool,
) -> float:
    eid = ex["id"]
    if eid in last_sess:
        return -100.0
    score = random.uniform(0, 0.3)
    if eid in history:
        score -= 2.0
    elif eid in fav:
        score += 1.0
        if eid not in last_sess:
            score += 0.5
    is_fav = eid in fav
    if is_fav and fav_count >= max_fav:
        return -50.0
    if is_fav and role == "primary" and fav_count >= 1 and soft_session:
        return -50.0
    return score


def pick_for_slot(
    db: ExerciseDB,
    groups: List[str],
    role: str,
    *,
    taken_ids: Set[str],
    history: Set[str],
    last_sess: Set[str],
    fav: Set[str],
    fav_count: int,
    fav_primary_count: int,
    soft_session: bool,
) -> Optional[Dict[str, Any]]:
    pool = [ex for ex in _pool_for_slot(db, groups, role) if ex["id"] not in taken_ids]
    if not pool:
        return None
    max_fav_day = 2 if not soft_session else 1
    max_fav_primary = 1

    def capped_fav_remaining() -> int:
        return max(0, max_fav_day - fav_count)

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for ex in pool:
        fc = fav_count
        if ex["id"] in fav and capped_fav_remaining() <= 0:
            s = -50.0
        elif ex["id"] in fav and role == "primary" and fav_primary_count >= max_fav_primary:
            s = -50.0
        else:
            s = _score_ex(
                ex,
                history=history,
                last_sess=last_sess,
                fav=fav,
                fav_count=fav_count if ex["id"] in fav else fav_count,
                max_fav=max_fav_day,
                role=role,
                soft_session=soft_session,
            )
        scored.append((s, ex))
    scored.sort(key=lambda x: -x[0])
    best = scored[0]
    if best[0] < -10:
        # force non-favorite if possible
        for s, ex in scored:
            if ex["id"] not in fav and s > -10:
                return ex
    return best[1] if best[0] > -10 else scored[0][1] if scored else None


def build_day_plan(
    db: ExerciseDB,
    day_tpl: Dict[str, Any],
    *,
    scheme: str,
    soft_session: bool = False,
) -> Dict[str, Any]:
    day_key = day_tpl["day_key"]
    split = day_tpl["split"]
    history = history_ids_for_day(day_key)
    last_sess = last_session_ids()
    fav = load_favorite_ids()
    taken: Set[str] = set()
    fav_count = 0
    fav_primary = 0
    exercises: List[Dict[str, Any]] = []

    for slot in day_tpl["slots"]:
        ex = pick_for_slot(
            db,
            slot["groups"],
            slot["role"],
            taken_ids=taken,
            history=history,
            last_sess=last_sess,
            fav=fav,
            fav_count=fav_count,
            fav_primary_count=fav_primary,
            soft_session=soft_session,
        )
        if not ex:
            continue
        taken.add(ex["id"])
        if ex["id"] in fav:
            fav_count += 1
            if slot["role"] == "primary":
                fav_primary += 1
        exercises.append(
            {
                "exercise_id": ex["id"],
                "name": ex["name"],
                "original": ex["name"],
                "sets": slot.get("sets", 3),
                "reps_target": slot.get("reps", 10),
                "weight_kg": None,
                "role": slot["role"],
            }
        )

    warmup = WARMUP_BY_SPLIT.get(split, WARMUP_BY_SPLIT["full"])
    title = f"{day_tpl['title_base']}: рабочий день"
    warmup_note = (
        f"{warmup['minutes']} мин: {warmup['cardio']} {warmup['mobility']}"
    )
    coach = {
        "lower": "Ноги и ягодицы в начале, изоляция в конце — классика для низа.",
        "upper": "Тяни → жми → плечи/руки: верх без перекоса.",
        "full": "Сбалансированный микс по всему телу за одну сессию.",
    }.get(split, "Ровный темп, техника важнее веса.")

    return {
        "title": title,
        "day_key": day_key,
        "split": split,
        "scheme": scheme,
        "warmup_note": warmup_note,
        "warmup": warmup,
        "coach_note": coach,
        "exercises": exercises,
    }


def build_cycle(db: ExerciseDB, scheme: str, *, soft_session: bool = False) -> Dict[str, Any]:
    tpls = DAY_TEMPLATES.get(scheme) or DAY_TEMPLATES["full_body"]
    days = [build_day_plan(db, t, scheme=scheme, soft_session=soft_session) for t in tpls]
    return {
        "scheme": scheme,
        "days": days,
        "cursor": {"next_day_key": days[0]["day_key"] if days else "FB"},
    }


def resolve_today_from_cycle(cycle: Dict[str, Any]) -> Dict[str, Any]:
    days = cycle.get("days") or []
    if not days:
        return {}
    key = (cycle.get("cursor") or {}).get("next_day_key") or days[0].get("day_key")
    for d in days:
        if d.get("day_key") == key:
            return d
    return days[0]


def advance_cycle_cursor(cycle: Dict[str, Any], done_day_key: str) -> Dict[str, Any]:
    days = cycle.get("days") or []
    keys = [d.get("day_key") for d in days]
    if done_day_key not in keys:
        return cycle
    i = keys.index(done_day_key)
    nxt = keys[(i + 1) % len(keys)]
    c = dict(cycle)
    c["cursor"] = {"next_day_key": nxt}
    return c


def append_history_entry(day_key: str, exercise_ids: List[str]) -> Dict[str, Any]:
    from app.state import now_local

    hist = load_history()
    hist.append(
        {
            "date": now_local().strftime("%Y-%m-%d"),
            "day_key": day_key,
            "exercise_ids": exercise_ids,
        }
    )
    return hist[-20:]


SPLIT_LABELS = {
    "lower": "низ",
    "upper": "верх",
    "full": "full body",
}
