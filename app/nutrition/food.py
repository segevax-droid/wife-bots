import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from aiohttp import ClientSession, ClientTimeout

from app.gemini import call_gemini
from app.sheets import append_row_to_sheet, get_worksheet, get_ws_values
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
    data, conf = await gemini_macros(staple, off)
    if off:
        for kk, vv in {"kcal": off.get("kcal"), "p": off.get("p"), "f": off.get("f"), "c": off.get("c")}.items():
            if vv is not None:
                data[kk] = float(vv)
    data["confidence"] = conf
    await cache_store(k, {**data, "confidence": conf}, "off+gemini")
    return data, conf, "off+gemini"


async def parse_meal_grams(text: str) -> Tuple[str, float]:
    prompt = f"Извлеки staple (продукт) и массу в граммах из фразы. JSON: {{\"staple\":\"\",\"grams\":число}}. Фраза: {text}"
    raw = await call_gemini(prompt)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return text.strip(), 100.0
    try:
        j = json.loads(m.group(0))
        return str(j.get("staple", text)).strip(), float(j.get("grams", 100))
    except Exception:
        return text.strip(), 100.0


async def log_meal_row(staple: str, grams: float, m: Dict[str, float]) -> Tuple[float, float, float, float]:
    scale = grams / 100.0
    kcal = m.get("kcal", 0) * scale
    p = m.get("p", 0) * scale
    f = m.get("f", 0) * scale
    c = m.get("c", 0) * scale
    ts = now_local().strftime("%Y-%m-%d %H:%M")
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
            "",
        ],
    )
    return p, f, c, kcal
