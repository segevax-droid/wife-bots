import hashlib
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from aiohttp import ClientSession, ClientTimeout

from app.gemini import call_gemini
from app.sheets import append_row_to_sheet, delete_sheet_row, get_worksheet, get_ws_values
from app.state import now_local

logger = logging.getLogger("wife_bots")


def cache_key(name: str) -> str:
    return hashlib.sha256((name or "").strip().lower().encode()).hexdigest()[:24]


async def cache_lookup(key: str) -> Optional[Dict[str, Any]]:
    ws = await get_worksheet("Продукты_кэш")
    rows = await get_ws_values(ws)
    for r in rows[1:]:
        if r and str(r[0]).strip() == key and len(r) > 7:
            try:
                return {
                    "kcal": float(str(r[1]).replace(",", ".")),
                    "p": float(str(r[2]).replace(",", ".")),
                    "f": float(str(r[3]).replace(",", ".")),
                    "c": float(str(r[4]).replace(",", ".")),
                    "src": str(r[5]),
                    "conf": float(str(r[6]).replace(",", ".")),
                }
            except Exception:
                return None
    return None


async def cache_store(key: str, data: Dict[str, Any], src: str) -> None:
    row = [
        key,
        f"{data.get('kcal', 0):.1f}",
        f"{data.get('p', 0):.2f}",
        f"{data.get('f', 0):.2f}",
        f"{data.get('c', 0):.2f}",
        src,
        f"{data.get('confidence', 0.8):.2f}",
        json.dumps(data, ensure_ascii=False),
    ]
    await append_row_to_sheet("Продукты_кэш", row)


async def off_search(staple: str) -> Optional[Dict[str, Any]]:
    q = quote_plus(staple[:120])
    url = f"https://world.openfoodfacts.org/cgi/search.pl?search_terms={q}&search_simple=1&action=process&json=1&page_size=2"
    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                js = await resp.json()
        prods = js.get("products") or []
        if not prods:
            return None
        p = prods[0]
        n = p.get("nutriments") or {}
        def g(k):
            v = n.get(k)
            try:
                return float(v)
            except Exception:
                return None
        return {
            "name": p.get("product_name") or staple,
            "kcal": g("energy-kcal_100g") or g("energy-kcal"),
            "p": g("proteins_100g"),
            "f": g("fat_100g"),
            "c": g("carbohydrates_100g"),
            "raw": p.get("code", ""),
        }
    except Exception as e:
        logger.warning("OFF %s", e)
        return None


async def gemini_macros(staple: str, off: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], float]:
    ctx = json.dumps(off or {}, ensure_ascii=False)[:800]
    prompt = (
        f"Продукт: {staple}\nДанные OFF: {ctx}\n"
        "Верни JSON: {\"kcal\":..,\"p\":..,\"f\":..,\"c\":..} на 100г, \"confidence\":0..1."
    )
    raw = await call_gemini(prompt)
    m = re.search(r"\{[\s\S]*\}", raw)
    conf = 0.5
    if not m:
        return {"kcal": 0, "p": 0, "f": 0, "c": 0}, conf
    try:
        j = json.loads(m.group(0))
        conf = float(j.get("confidence", 0.7))
        return {
            "kcal": float(j.get("kcal", 0)),
            "p": float(j.get("p", 0)),
            "f": float(j.get("f", 0)),
            "c": float(j.get("c", 0)),
        }, conf
    except Exception:
        return {"kcal": 0, "p": 0, "f": 0, "c": 0}, 0.3


def sanity(m: Dict[str, float]) -> bool:
    k = m.get("kcal", 0)
    return 0 < k < 950


async def resolve_staple_macros(staple: str) -> Tuple[Dict[str, Any], float, str]:
    k = cache_key(staple)
    hit = await cache_lookup(k)
    if hit and sanity(hit):
        return hit, hit.get("conf", 0.9), "cache"
    off = await off_search(staple)
    if off and all(off.get(x) is not None for x in ("kcal", "p", "f", "c")):
        data = {"kcal": float(off["kcal"]), "p": float(off["p"]), "f": float(off["f"]), "c": float(off["c"]), "confidence": 0.85}
        if sanity(data):
            await cache_store(k, data, "off")
            return data, 0.85, "off"
    data, conf = await gemini_macros(staple, off)
    if off:
        for kk, vv in {"kcal": off.get("kcal"), "p": off.get("p"), "f": off.get("f"), "c": off.get("c")}.items():
            if vv is not None:
                data[kk] = float(vv)
    data["confidence"] = conf
    await cache_store(k, {**data, "confidence": conf}, "off+gemini")
    return data, conf, "off+gemini"


def parse_meal_local(text: str) -> Optional[Tuple[str, float]]:
    s = (text or "").strip()
    m = re.search(r"^(.+?)\s+(\d+(?:[.,]\d+)?)\s*(?:г|гр|g)\b", s, re.I)
    if m:
        return m.group(1).strip(), float(m.group(2).replace(",", "."))
    m = re.search(r"^(\d+(?:[.,]\d+)?)\s*(?:г|гр|g)\s+(.+)$", s, re.I)
    if m:
        return m.group(2).strip(), float(m.group(1).replace(",", "."))
    return None


async def parse_meal_grams(text: str) -> Tuple[str, float]:
    local = parse_meal_local(text)
    if local:
        return local
    prompt = f'Извлеки продукт и массу в граммах. JSON: {{"staple":"","grams":число}}. Фраза: {text}'
    raw = await call_gemini(prompt)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return text.strip(), 100.0
    try:
        j = json.loads(m.group(0))
        return str(j.get("staple", text)).strip(), float(j.get("grams", 100))
    except Exception:
        return text.strip(), 100.0


def new_meal_id() -> str:
    return uuid.uuid4().hex[:8]


def _cell(row: List[str], idx: int) -> str:
    return str(row[idx]).strip() if len(row) > idx else ""


def _meal_id_from_row(row: List[str]) -> str:
    comment = _cell(row, 8)
    if re.fullmatch(r"[0-9a-f]{8}", comment, re.I):
        return comment.lower()
    return ""


def _parse_meal_row(row: List[str], sheet_row: int) -> Optional[Dict[str, Any]]:
    name = _cell(row, 2)
    if not name:
        return None
    time_raw = _cell(row, 1) or _cell(row, 0)
    hm = re.search(r"(\d{1,2}:\d{2})", time_raw)
    try:
        grams = float(_cell(row, 3).replace(",", ".") or "0")
    except Exception:
        grams = 0.0
    try:
        kcal = float(_cell(row, 7).replace(",", ".") or "0")
    except Exception:
        kcal = 0.0
    return {
        "sheet_row": sheet_row,
        "date": _cell(row, 0),
        "time": hm.group(1) if hm else "",
        "name": name,
        "grams": grams,
        "kcal": kcal,
        "meal_id": _meal_id_from_row(row),
    }


async def list_today_meals() -> List[Dict[str, Any]]:
    ws = await get_worksheet("Питание")
    if not ws:
        return []
    rows = await get_ws_values(ws)
    day = now_local().strftime("%Y-%m-%d")
    out: List[Dict[str, Any]] = []
    for idx, r in enumerate(rows[1:], start=2):
        if not r or not _cell(r, 0).startswith(day):
            continue
        parsed = _parse_meal_row(r, idx)
        if parsed:
            out.append(parsed)
    return out


def _meal_matches_id(row: List[str], meal_id: str) -> bool:
    return _meal_id_from_row(row) == meal_id.lower()


async def find_meal_by_id(meal_id: str) -> Optional[Dict[str, Any]]:
    if not meal_id:
        return None
    ws = await get_worksheet("Питание")
    if not ws:
        return None
    rows = await get_ws_values(ws)
    for idx, r in enumerate(rows[1:], start=2):
        if r and _meal_matches_id(r, meal_id):
            return _parse_meal_row(r, idx)
    return None


async def delete_meal(meal_id: str = "", sheet_row: int = 0) -> Optional[Dict[str, Any]]:
    ws = await get_worksheet("Питание")
    if not ws:
        return None
    rows = await get_ws_values(ws)
    target: Optional[Dict[str, Any]] = None
    if meal_id:
        for idx, r in enumerate(rows[1:], start=2):
            if r and _meal_matches_id(r, meal_id):
                target = _parse_meal_row(r, idx)
                break
    elif 2 <= sheet_row <= len(rows):
        day = now_local().strftime("%Y-%m-%d")
        r = rows[sheet_row - 1]
        if r and _cell(r, 0).startswith(day):
            target = _parse_meal_row(r, sheet_row)
    if not target:
        return None
    ok = await delete_sheet_row("Питание", int(target["sheet_row"]))
    return target if ok else None


def meal_button_label(meal: Dict[str, Any]) -> str:
    t = meal.get("time") or ""
    name = str(meal.get("name") or "приём")[:28]
    grams = meal.get("grams") or 0
    prefix = f"✕ {t} " if t else "✕ "
    return f"{prefix}{name} {grams:.0f}г"[:64]


async def log_meal_row(staple: str, grams: float, m: Dict[str, float]) -> Tuple[float, float, float, float, str]:
    scale = grams / 100.0
    kcal = m.get("kcal", 0) * scale
    p = m.get("p", 0) * scale
    f = m.get("f", 0) * scale
    c = m.get("c", 0) * scale
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    meal_id = new_meal_id()
    await append_row_to_sheet(
        "Питание",
        [
            ts,
            ts,
            staple,
            f"{grams:.0f}",
            f"{p:.1f}",
            f"{f:.1f}",
            f"{c:.1f}",
            f"{kcal:.0f}",
            meal_id,
        ],
    )
    return p, f, c, kcal, meal_id
