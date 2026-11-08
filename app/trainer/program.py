"""Детерминированный подбор упражнений по сплиту, истории и любимым."""

import json
import random
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.state import SYSTEM_STATE
from app.trainer.exercise_db import ExerciseDB

FB_GROUP_ORDER = ("Ноги", "Спина", "Грудь", "Плечи", "Руки", "Кор", "Пресс", "Ягодицы")

# Sheet group labels vary; match by substring, then by exercise name.
_GROUP_KEYS: List[Tuple[str, str]] = [
    ("ягодиц", "Ягодицы"),
    ("бицепс бедра", "Ноги"),
    ("задн", "Ноги"),
    ("квадр", "Ноги"),
    ("икр", "Ноги"),
    ("голен", "Ноги"),
    ("ног", "Ноги"),
    ("спин", "Спина"),
    ("широк", "Спина"),
    ("трапец", "Спина"),
    ("грудь", "Грудь"),
    ("грудн", "Грудь"),
    ("пектор", "Грудь"),
    ("плеч", "Плечи"),
    ("делт", "Плечи"),
    ("трицепс", "Руки"),
    ("бицепс", "Руки"),
    ("рук", "Руки"),
    ("пресс", "Пресс"),
    ("кор", "Кор"),
    ("core", "Кор"),
]

_NAME_KEYS: List[Tuple[tuple, str]] = [
    (("ягодичн", "hip thrust", "glute"), "Ягодицы"),
    (("присед", "выпад", "жим ногами", "разгибан", "сгибан ног", "румынск", "становая", "икры", "голен"), "Ноги"),
    (("подтяг", "тяга", "гиперэкстенз", "лицфейс", "пульдаун", "pull"), "Спина"),
    (("жим лёж", "жим лежа", "отжим", "сведен", "пуловер", "бабочка", "pec"), "Грудь"),
    (("жим стоя", "жим сидя", "махи", "разведен", "арнольд"), "Плечи"),
    (("бицепс", "трицепс", "молот", "французск", "сгибан рук"), "Руки"),
    (("скручиван", "планка", "пресс", "crunch"), "Пресс"),
]


def canonical_group(ex: Dict[str, Any]) -> str:
    raw = (ex.get("group") or "").strip().lower()
    name = (ex.get("name") or "").strip().lower()
    for key, g in _GROUP_KEYS:
        if key in raw:
            return g
    for keys, g in _NAME_KEYS:
        if any(k in name for k in keys):
            return g
    for key, g in _GROUP_KEYS:
        if key in name:
            return g
    return (ex.get("group") or "").strip() or "Прочее"


def _is_placeholder_name(name: str) -> bool:
    return bool(re.search(r"упражнение\s+\d+", name or "", re.I))


def _catalog(db: ExerciseDB) -> List[Dict[str, Any]]:
    """All exercises, annotated with canonical group. Prefer real names inside a group."""
    out: List[Dict[str, Any]] = []
    for ex in db.by_id.values():
        row = dict(ex)
        row["_cg"] = canonical_group(ex)
        row["_ph"] = _is_placeholder_name(ex.get("name") or "")
        out.append(row)
    return out
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
                {"groups": ["Руки"], "role": "aux", "sets": 3, "reps": 12},
                {"groups": ["Кор", "Пресс"], "role": "isolation", "sets": 3, "duration_sec": 45},
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
                {"groups": ["Кор", "Пресс"], "role": "isolation", "sets": 3, "duration_sec": 45},
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


def _pool_for_groups(catalog: List[Dict[str, Any]], groups: List[str], taken_ids: Set[str]) -> List[Dict[str, Any]]:
    want = set(groups)
    pool = [ex for ex in catalog if ex["id"] not in taken_ids and ex.get("_cg") in want]
    if not pool:
        return []
    real = [ex for ex in pool if not ex.get("_ph")]
    return real or pool


def pick_from_groups(
    catalog: List[Dict[str, Any]],
    groups: List[str],
    taken_ids: Set[str],
    *,
    history: Set[str],
    last_sess: Set[str],
    fav: Set[str],
) -> Optional[Dict[str, Any]]:
    pool = _pool_for_groups(catalog, groups, taken_ids)
    if not pool:
        return None
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for ex in pool:
        s = 0.0
        if ex["id"] in last_sess:
            s -= 1.5
        if ex["id"] in history:
            s -= 0.8
        if ex["id"] in fav:
            s += 1.0
        if ex.get("_ph"):
            s -= 3.0
        s += random.uniform(0, 0.2)
        scored.append((s, ex))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


def _plan_weight(ex: Dict[str, Any]) -> Optional[float]:
    if ExerciseDB.is_bodyweight(ex):
        return 0.0
    return None


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


def _append_plan_ex(exercises: List[Dict[str, Any]], ex: Dict[str, Any], slot: Dict[str, Any]) -> None:
    timed = ExerciseDB.is_timed(ex)
    row: Dict[str, Any] = {
        "exercise_id": ex["id"],
        "name": ex["name"],
        "original": ex["name"],
        "sets": slot.get("sets", 3),
        "weight_kg": _plan_weight(ex),
        "role": slot.get("role", "aux"),
        "group": ex.get("_cg") or canonical_group(ex),
    }
    if timed:
        row["is_timed"] = True
        row["duration_sec_target"] = int(slot.get("duration_sec", 45))
    else:
        row["reps_target"] = slot.get("reps", 10)
    exercises.append(row)


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
    catalog = _catalog(db)
    taken: Set[str] = set()
    exercises: List[Dict[str, Any]] = []
    used_groups: Set[str] = set()
    fav_count = 0
    max_fav = 1 if soft_session else 2

    for slot in day_tpl["slots"]:
        groups = list(slot["groups"])
        if split == "full":
            groups = [g for g in groups if g not in used_groups] or groups
        ex = pick_from_groups(
            catalog,
            groups,
            taken,
            history=history,
            last_sess=last_sess,
            fav=fav if fav_count < max_fav else set(),
        )
        if not ex and split == "full":
            leftover = [g for g in FB_GROUP_ORDER if g not in used_groups]
            ex = pick_from_groups(
                catalog, leftover, taken, history=history, last_sess=last_sess, fav=set()
            )
        if not ex:
            continue
        g = ex.get("_cg") or canonical_group(ex)
        if split == "full" and g in used_groups:
            continue
        taken.add(ex["id"])
        if g:
            used_groups.add(g)
        if ex["id"] in fav:
            fav_count += 1
        _append_plan_ex(exercises, ex, slot)

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
    "full": "всё тело",
    "cardio": "кардио",
}

PHASE_LABELS = {
    "volume": "объём",
    "intensity": "интенсив",
    "deload": "разгрузка",
}

SPLIT_SHORT = {
    "lower": "Низ",
    "upper": "Верх",
    "full": "Всё тело",
    "cardio": "Кардио",
}


def scheme_for_frequency(sessions_per_week: int) -> str:
    return "upper_lower" if sessions_per_week >= 4 else "full_body"


def scale_slots_for_phase(slots: List[Dict[str, Any]], phase: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in slots:
        s = dict(raw)
        if phase == "intensity":
            if "reps" in s:
                s["reps"] = max(5, int(s["reps"]) - 2)
            if "duration_sec" in s:
                s["duration_sec"] = min(75, int(s["duration_sec"]) + 15)
        elif phase == "deload":
            s["sets"] = max(2, int(s.get("sets", 3)) - 1)
            if "duration_sec" in s:
                s["duration_sec"] = max(20, int(s["duration_sec"]) - 10)
        elif phase == "volume":
            if "reps" in s:
                s["reps"] = int(s["reps"]) + 2
        out.append(s)
    return out


def cardio_day_plan(phase: str) -> Dict[str, Any]:
    if phase == "intensity":
        exercises = [
            {
                "exercise_id": "cardio_wu",
                "name": "Разгон: лёгкое кардио",
                "original": "Разгон: лёгкое кардио",
                "sets": 1,
                "is_timed": True,
                "duration_sec_target": 300,
                "weight_kg": 0.0,
                "group": "Кардио",
            },
            {
                "exercise_id": "cardio_int",
                "name": "Интервалы 40/80 с",
                "original": "Интервалы 40/80 с",
                "sets": 8,
                "is_timed": True,
                "duration_sec_target": 40,
                "weight_kg": 0.0,
                "group": "Кардио",
            },
            {
                "exercise_id": "cardio_cd",
                "name": "Заминка ходьбой",
                "original": "Заминка ходьбой",
                "sets": 1,
                "is_timed": True,
                "duration_sec_target": 300,
                "weight_kg": 0.0,
                "group": "Кардио",
            },
        ]
        title = "Кардио · интервалы"
        coach = "После двух силовых — пульсовая работа. 40 с быстрее, 80 с спокойно."
        warmup = "3 мин лёгкого шага, затем 2 ускорения по 20 с."
        minutes = 8
    elif phase == "deload":
        exercises = [
            {
                "exercise_id": "cardio_easy",
                "name": "Спокойная ходьба / вело",
                "original": "Спокойная ходьба / вело",
                "sets": 1,
                "is_timed": True,
                "duration_sec_target": 1200,
                "weight_kg": 0.0,
                "group": "Кардио",
            }
        ]
        title = "Кардио · восстановление"
        coach = "Разгрузочная неделя: разговорный темп, без гонки за пульсом."
        warmup = "Суставы 2 минуты и сразу в спокойный темп."
        minutes = 4
    else:
        exercises = [
            {
                "exercise_id": "cardio_z2",
                "name": "Кардио зона 2 (ходьба / эллипс / вело)",
                "original": "Кардио зона 2",
                "sets": 1,
                "is_timed": True,
                "duration_sec_target": 1500,
                "weight_kg": 0.0,
                "group": "Кардио",
            }
        ]
        title = "Кардио · зона 2"
        coach = "После двух силовых — аэробная база. Можно говорить предложениями."
        warmup = "2–3 мин лёгкого шага, затем рабочий темп."
        minutes = 5
    return {
        "title": title,
        "day_key": "C",
        "split": "cardio",
        "scheme": "cardio",
        "kind": "cardio",
        "phase": phase,
        "warmup_note": warmup,
        "warmup": {"cardio": warmup, "mobility": "свободное дыхание, плечи и бёдра.", "minutes": minutes},
        "coach_note": coach,
        "exercises": exercises,
    }
