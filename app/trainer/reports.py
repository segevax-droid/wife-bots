import json
import statistics
from typing import Dict, List, Optional, Tuple

from app.gemini import call_gemini
from app.sheets import get_sheet_rows_for_period, get_worksheet, get_ws_values


def _parse_session_json(cell: str) -> Optional[dict]:
    try:
        return json.loads(cell or "")
    except Exception:
        return None


async def load_sessions(days: int) -> List[dict]:
    rows = await get_sheet_rows_for_period("Сессии", days, date_col=1)
    out = []
    for r in rows:
        if len(r) < 18:
            continue
        try:
            jid = str(r[0]).strip()
            d = str(r[1]).strip()
            ex_json = r[17] if len(r) > 17 else "{}"
            js = _parse_session_json(ex_json) or {}
            out.append({"id": jid, "date": d, "json": js})
        except Exception:
            continue
    return out


async def compare_last_session() -> str:
    ws = await get_worksheet("Сессии")
    rows = await get_ws_values(ws)
    data = rows[1:] if len(rows) > 1 else []
    if len(data) < 2:
        return "Недостаточно сессий для сравнения."
    last = data[-1]
    prev = data[-2]
    lj = _parse_session_json(last[17] if len(last) > 17 else "") or {}
    pj = _parse_session_json(prev[17] if len(prev) > 17 else "") or {}
    last_ex = {e.get("exercise_id"): e for e in lj.get("exercises", [])}
    prev_ex = {e.get("exercise_id"): e for e in pj.get("exercises", [])}
    lines = []
    for eid, cur in last_ex.items():
        old = prev_ex.get(eid)
        if not old:
            continue
        ct, pt = float(cur.get("tonnage", 0) or 0), float(old.get("tonnage", 0) or 0)
        pct = (ct - pt) / pt * 100 if pt else 0.0
        lines.append(f"{cur.get('name','?')}: тоннаж {pct:+.1f}% к прошлой")
    return "\n".join(lines) if lines else "Нет пересечений по упражнениям с прошлой сессией."


async def four_week_agg() -> Dict[str, Tuple[float, float, float, float]]:
    sessions = await load_sessions(28)
    by_ex: Dict[str, List[dict]] = {}
    for s in sessions:
        for e in s["json"].get("exercises", []):
            eid = e.get("exercise_id")
            if not eid:
                continue
            by_ex.setdefault(eid, []).append(e)
    agg: Dict[str, Tuple[float, float, float, float]] = {}
    for eid, items in by_ex.items():
        tons = [float(x.get("tonnage", 0) or 0) for x in items]
        weights = [float(x.get("avg_weight", 0) or 0) for x in items if x.get("avg_weight")]
        reps = [float(x.get("avg_reps", 0) or 0) for x in items if x.get("avg_reps")]
        diff = [float(x.get("avg_difficulty", 0) or 0) for x in items if x.get("avg_difficulty")]
        agg[eid] = (
            statistics.mean(tons) if tons else 0.0,
            statistics.mean(weights) if weights else 0.0,
            statistics.mean(reps) if reps else 0.0,
            statistics.mean(diff) if diff else 0.0,
        )
    return agg


async def report_vs_avg_four_weeks(last_session_json: dict) -> str:
    agg = await four_week_agg()
    lines = []
    for e in last_session_json.get("exercises", []):
        eid = e.get("exercise_id")
        if not eid or eid not in agg:
            continue
        at, _, _, ad = agg[eid]
        ct = float(e.get("tonnage", 0) or 0)
        pct = (ct - at) / at * 100 if at else 0.0
        lower_better = ""
        if ad > 0:
            cur_d = float(e.get("avg_difficulty", 0) or 0)
            dp = (cur_d - ad) / ad * 100 if ad else 0.0
            lower_better = f"; тяжесть {dp:+.1f}% к ср (ниже лучше)"
        lines.append(f"{e.get('name','?')}: тоннаж {pct:+.1f}% к ср. за 4 нед{lower_better}")
    return "\n".join(lines) if lines else "Нет базы для сравнения (мало истории)."


async def week_month_top_moves(days: int) -> str:
    sessions = await load_sessions(days)
    by_ex: Dict[str, List[dict]] = {}
    for s in sessions:
        for e in s["json"].get("exercises", []):
            eid = e.get("exercise_id")
            if eid:
                by_ex.setdefault(eid, []).append(e)
    deltas: List[Tuple[float, str]] = []
    for eid, hist in by_ex.items():
        if len(hist) < 2:
            continue
        name = hist[-1].get("name", "?")
        tons = [float(x.get("tonnage", 0) or 0) for x in hist]
        last, mean_prev = tons[-1], statistics.mean(tons[:-1])
        if mean_prev <= 0:
            continue
        pct = (last - mean_prev) / mean_prev * 100
        deltas.append((pct, name))
    if not deltas:
        return "Мало данных за период."
    deltas.sort(key=lambda x: -x[0])
    up = deltas[:5]
    down = sorted(deltas, key=lambda x: x[0])[:3]
    text = "Топ рост:\n" + "\n".join(f"{n}: {p:+.1f}%" for p, n in up)
    text += "\nТоп падение:\n" + "\n".join(f"{n}: {p:+.1f}%" for p, n in down)
    big = [x for x in deltas if abs(x[0]) > 10]
    if big:
        prompt = f"Кратко (1–2 строки) прокомментируй отклонения >10% по тоннажу: {big[:8]}"
        text += "\n" + await call_gemini(prompt)
    return text
