"""Месячный план тренера: частота из онбординга, периодизация, кардио после каждых двух силовых."""

from __future__ import annotations

import calendar
import json
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.state import SYSTEM_STATE, now_local, save_state_pairs
from app.trainer.exercise_db import ExerciseDB
from app.trainer.program import (
    DAY_TEMPLATES,
    PHASE_LABELS,
    SPLIT_SHORT,
    build_day_plan,
    cardio_day_plan,
    scale_slots_for_phase,
    scheme_for_frequency,
)

MONTH_PLAN_KEY = "month_plan_json"

MONTH_RU = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)

# weekday index 0=Mon
_FREQ_WEEKDAYS = {
    2: (1, 4),
    3: (0, 2, 4),
    4: (0, 1, 3, 4),
    5: (0, 1, 2, 3, 4),
    6: (0, 1, 2, 3, 4, 5),
}


def parse_freq(raw: Any, default: int = 3) -> int:
    try:
        n = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(2, min(n, 6))


def phase_for_week(week_index: int) -> str:
    if week_index <= 1:
        return "volume"
    if week_index == 2:
        return "intensity"
    return "deload"


def session_kind(seq_i: int) -> str:
    """0-based: сила, сила, кардио, …"""
    return "cardio" if (seq_i + 1) % 3 == 0 else "strength"


def _dates_for_month(year: int, month: int, sessions_per_week: int) -> List[Tuple[date, int]]:
    weekdays = _FREQ_WEEKDAYS.get(sessions_per_week, _FREQ_WEEKDAYS[3])
    cal = calendar.Calendar(firstweekday=0)
    out: List[Tuple[date, int]] = []
    for wi, week in enumerate(cal.monthdayscalendar(year, month)):
        for di, day in enumerate(week):
            if day and di in weekdays:
                out.append((date(year, month, day), wi))
    return out


def build_month_schedule(year: int, month: int, sessions_per_week: int) -> Dict[str, Any]:
    freq = parse_freq(sessions_per_week)
    scheme = scheme_for_frequency(freq)
    tpls = DAY_TEMPLATES.get(scheme) or DAY_TEMPLATES["full_body"]
    sessions: List[Dict[str, Any]] = []
    strength_i = 0
    for n, (d, wi) in enumerate(_dates_for_month(year, month, freq), start=1):
        phase = phase_for_week(wi)
        kind = session_kind(n - 1)
        if kind == "cardio":
            split = "cardio"
            day_key = "C"
            short = "Кардио"
            title = f"Кардио · {PHASE_LABELS[phase]}"
        else:
            tpl = tpls[strength_i % len(tpls)]
            strength_i += 1
            split = tpl["split"]
            day_key = tpl["day_key"]
            short = SPLIT_SHORT.get(split, tpl.get("title_base") or "Сила")
            title = f"{short} · {PHASE_LABELS[phase]}"
        sessions.append(
            {
                "n": n,
                "date": d.isoformat(),
                "kind": kind,
                "scheme": scheme if kind == "strength" else "cardio",
                "day_key": day_key,
                "split": split,
                "phase": phase,
                "title": title,
                "short": short,
                "done": False,
            }
        )
    return {
        "year": year,
        "month": month,
        "sessions_per_week": freq,
        "scheme": scheme,
        "sessions": sessions,
    }


def load_month_plan() -> Dict[str, Any]:
    raw = SYSTEM_STATE.get(MONTH_PLAN_KEY) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_done(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    if not old or old.get("year") != new.get("year") or old.get("month") != new.get("month"):
        return new
    done_dates = {s.get("date") for s in (old.get("sessions") or []) if s.get("done")}
    for s in new["sessions"]:
        if s.get("date") in done_dates:
            s["done"] = True
    return new


async def save_month_plan(plan: Dict[str, Any]) -> None:
    await save_state_pairs({MONTH_PLAN_KEY: json.dumps(plan, ensure_ascii=False)})


async def ensure_month_plan(sessions_per_week: int, *, today: Optional[date] = None) -> Dict[str, Any]:
    today = today or now_local().date()
    freq = parse_freq(sessions_per_week)
    cur = load_month_plan()
    if (
        cur.get("year") == today.year
        and cur.get("month") == today.month
        and parse_freq(cur.get("sessions_per_week"), -1) == freq
        and cur.get("sessions")
    ):
        return cur
    fresh = build_month_schedule(today.year, today.month, freq)
    merged = _merge_done(cur, fresh)
    await save_month_plan(merged)
    return merged


def pick_session_for_start(plan: Dict[str, Any], today: Optional[date] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    today = today or now_local().date()
    sessions: List[Dict[str, Any]] = list(plan.get("sessions") or [])
    if not sessions:
        return None, "План на месяц ещё не собран."
    pending = [s for s in sessions if not s.get("done")]
    if not pending:
        return None, "Все тренировки месяца уже закрыты — умничка."
    iso = today.isoformat()
    for s in pending:
        if s.get("date") == iso:
            return s, ""
    upcoming = [s for s in pending if str(s.get("date") or "") >= iso]
    if upcoming:
        s = upcoming[0]
        return s, f"Сегодня в календаре отдых. Ближайшая — {s['date'][8:10]}.{s['date'][5:7]}."
    s = pending[0]
    return s, f"Эта тренировка была на {s['date'][8:10]}.{s['date'][5:7]} — догоняем."


def mark_session_done(plan: Dict[str, Any], n: int) -> Dict[str, Any]:
    for s in plan.get("sessions") or []:
        if int(s.get("n") or 0) == int(n):
            s["done"] = True
            break
    return plan


def days_map_for_png(plan: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for s in plan.get("sessions") or []:
        ds = str(s.get("date") or "")
        if len(ds) < 10:
            continue
        try:
            day = int(ds[8:10])
        except ValueError:
            continue
        out[day] = {"label": s.get("short") or s.get("title") or "", "done": bool(s.get("done"))}
    return out


def materialize_session(meta: Dict[str, Any], db: ExerciseDB) -> Dict[str, Any]:
    phase = str(meta.get("phase") or "volume")
    if meta.get("kind") == "cardio":
        day = cardio_day_plan(phase)
    else:
        scheme = str(meta.get("scheme") or "full_body")
        tpls = DAY_TEMPLATES.get(scheme) or DAY_TEMPLATES["full_body"]
        day_key = meta.get("day_key")
        tpl = next((t for t in tpls if t.get("day_key") == day_key), tpls[0])
        tpl = dict(tpl)
        tpl["slots"] = scale_slots_for_phase(list(tpl.get("slots") or []), phase)
        day = build_day_plan(db, tpl, scheme=scheme, soft_session=(phase == "deload"))
    day["title"] = meta.get("title") or day.get("title")
    day["kind"] = meta.get("kind")
    day["phase"] = phase
    day["session_n"] = meta.get("n")
    day["plan_date"] = meta.get("date")
    day["short"] = meta.get("short")
    return day
