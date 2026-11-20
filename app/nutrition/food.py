import hashlib
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from aiohttp import ClientSession, ClientTimeout

from app.gemini import call_gemini
from app.sheets import (
    append_row_to_sheet,
    delete_sheet_row,
    get_worksheet,
    get_ws_values,
    update_sheet_row_cells,
)
from app.state import now_local

logger = logging.getLogger("wife_bots")

_MACRO_CACHE_VER = "v2"
_KBJU_REL_TOL = 0.12
_RU_STOP = frozenset(
    {
        "из",
        "с",
        "и",
        "в",
        "на",
        "для",
        "без",
        "со",
        "по",
        "к",
        "у",
        "г",
        "гр",
        "пекарни",
        "пекарня",
        "магазина",
        "ресторана",
    }
)


def cache_key(name: str) -> str:
    raw = f"{_MACRO_CACHE_VER}|{(name or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def macro_kcal(m: Dict[str, float]) -> float:
    return 4.0 * m.get("p", 0) + 4.0 * m.get("c", 0) + 9.0 * m.get("f", 0)


def macros_consistent(m: Dict[str, float], rel_tol: float = _KBJU_REL_TOL) -> bool:
    k = m.get("kcal", 0)
    if k <= 0:
        return False
    mk = macro_kcal(m)
    if mk <= 0:
        return False
    return abs(k - mk) / k <= rel_tol


def harmonize_macros(m: Dict[str, float]) -> Dict[str, float]:
    """На 100 г: согласовать ккал с Б/Ж/У (4·4·9)."""
    p, f, c = m.get("p", 0), m.get("f", 0), m.get("c", 0)
    k = m.get("kcal", 0)
    mk = macro_kcal(m)
    if mk <= 0 and k > 0:
        return {"kcal": k, "p": p, "f": f, "c": c}
    if k <= 0 and mk > 0:
        return {"kcal": round(mk), "p": p, "f": f, "c": c}
    if k <= 0:
        return {"kcal": 0, "p": p, "f": f, "c": c}
    if abs(k - mk) / k <= 0.08:
        return {"kcal": round(mk), "p": p, "f": f, "c": c}
    if mk > 0:
        ratio = k / mk
        return {
            "kcal": k,
            "p": round(p * ratio, 2),
            "f": round(f * ratio, 2),
            "c": round(c * ratio, 2),
        }
    return {"kcal": k, "p": p, "f": f, "c": c}


def _staple_tokens(text: str) -> set:
    words = re.findall(r"[a-zа-яё0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 4 and w not in _RU_STOP}


def off_product_trusted(staple: str, off: Dict[str, Any]) -> bool:
    off_name = str(off.get("name") or "").strip()
    if not off_name:
        return False
    a, b = _staple_tokens(staple), _staple_tokens(off_name)
    if not a:
        return False
    if not b:
        return len(a) <= 2
    overlap = len(a & b)
    if overlap >= 2:
        return True
    if overlap >= 1 and len(a) <= 3:
        return True
    return False


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
    off_block = ""
    if off and off_product_trusted(staple, off):
        off_block = f"\nПроверенные данные OFF (тот же продукт): {json.dumps(off, ensure_ascii=False)[:800]}"
    brand_hint = ""
    if re.search(r"хлебник|xleb", staple, re.I):
        brand_hint = (
            "\nБренд Хлебник / xleb.ru — ориентируйся на КБЖУ этого блюда у бренда, "
            "не на «среднюю выпечку» и не на другой продукт из OFF."
        )
    prompt = (
        f"Продукт (РФ): {staple}{off_block}{brand_hint}\n"
        "Верни JSON: {\"kcal\":..,\"p\":..,\"f\":..,\"c\":..} на 100 г готового продукта, \"confidence\":0..1.\n"
        "Правила: kcal в пределах ±7% от 4×белки+4×углеводы+9×жиры; "
        "если в тексте бренд/сеть — КБЖУ именно этого блюда; "
        "готовые сэндвичи/фокаччи с начинкой — не как сухой хлеб."
    )
    raw = await call_gemini(prompt)
    m = re.search(r"\{[\s\S]*\}", raw)
    conf = 0.5
    if not m:
        return {"kcal": 0, "p": 0, "f": 0, "c": 0}, conf
    try:
        j = json.loads(m.group(0))
        conf = float(j.get("confidence", 0.7))
        data = harmonize_macros(
            {
                "kcal": float(j.get("kcal", 0)),
                "p": float(j.get("p", 0)),
                "f": float(j.get("f", 0)),
                "c": float(j.get("c", 0)),
            }
        )
        if not macros_consistent(data):
            conf = min(conf, 0.45)
        return data, conf
    except Exception:
        return {"kcal": 0, "p": 0, "f": 0, "c": 0}, 0.3


MEAL_MACRO_KEYS = ("kcal", "p", "f", "c")


def macros_only(m: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for key in MEAL_MACRO_KEYS:
        try:
            out[key] = float(m.get(key, 0))
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def sanity(m: Dict[str, float]) -> bool:
    k = m.get("kcal", 0)
    return 0 < k < 950


_WEAK_CAPTION = re.compile(
    r"^(?:вот|смотри|это|фото|here|ок|ok|да|ну|ага)(?:[!.…]|\s*)*$",
    re.I,
)


def caption_is_weak_food_hint(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    if _WEAK_CAPTION.match(s):
        return True
    if len(s) <= 4 and not parse_meal_local(s):
        return True
    return False


async def my_product_lookup(staple: str) -> Optional[Dict[str, float]]:
    s = (staple or "").strip().lower()
    if len(s) < 2:
        return None
    ws = await get_worksheet("Мои_продукты")
    if not ws:
        return None
    rows = await get_ws_values(ws)
    best: Optional[Dict[str, float]] = None
    best_len = 0
    tokens = _staple_tokens(staple)
    for r in rows[1:]:
        if len(r) < 5:
            continue
        name = str(r[0]).strip().lower()
        alias = str(r[1]).strip().lower() if len(r) > 1 else ""
        if not name:
            continue
        matched = name in s or s in name
        if alias and (alias in s or s in alias):
            matched = True
        if not matched and tokens:
            keys = _staple_tokens(name) | _staple_tokens(alias)
            if keys and len(tokens & keys) >= 1 and len(name) >= 3:
                matched = True
        if not matched:
            continue
        try:
            data = {
                "kcal": float(str(r[2]).replace(",", ".")),
                "p": float(str(r[3]).replace(",", ".")),
                "f": float(str(r[4]).replace(",", ".")),
                "c": float(str(r[5]).replace(",", ".")) if len(r) > 5 else 0.0,
            }
        except Exception:
            continue
        if not sanity(data):
            continue
        key_len = max(len(name), len(alias))
        if key_len >= best_len:
            best_len = key_len
            best = harmonize_macros(data)
    return best


def _num(s: str) -> float:
    return float(str(s).replace(",", ".").strip())


def _macros_per100_from_portion(
    grams: float, kcal: Optional[float] = None, p: Optional[float] = None, f: Optional[float] = None, c: Optional[float] = None
) -> Optional[Dict[str, float]]:
    if grams <= 0:
        return None
    scale = 100.0 / grams
    out: Dict[str, float] = {}
    if kcal is not None:
        out["kcal"] = kcal * scale
    if p is not None:
        out["p"] = p * scale
    if f is not None:
        out["f"] = f * scale
    if c is not None:
        out["c"] = c * scale
    if len(out) < 2 and kcal is None:
        return None
    for key in MEAL_MACRO_KEYS:
        out.setdefault(key, 0.0)
    data = harmonize_macros(out)
    return data if sanity(data) else None


_RE_PER100 = re.compile(
    r"(?:на\s+)?100\s*(?:г|гр|g)\s*[:·\-]\s*"
    r"(\d+(?:[.,]\d+)?)(?:\s+(\d+(?:[.,]\d+)?))?(?:\s+(\d+(?:[.,]\d+)?))?(?:\s+(\d+(?:[.,]\d+)?))?",
    re.I,
)
_RE_KCAL = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:ккал|kcal)\b", re.I)
_RE_BJU_SLASH = re.compile(
    r"б\s*/\s*ж\s*/\s*у\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)",
    re.I,
)
_RE_BJU_WORDS = re.compile(
    r"б(?:ел)?\s*(\d+(?:[.,]\d+)?)\s*ж(?:ир)?\s*(\d+(?:[.,]\d+)?)\s*у(?:гл)?\s*(\d+(?:[.,]\d+)?)",
    re.I,
)


def _extract_macro_fields(s: str) -> Tuple[str, bool, Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Возвращает (очищенный текст, per100_явно, kcal, p, f, c)."""
    per100 = False
    kcal = p = f = c = None
    m = _RE_PER100.search(s)
    if m:
        per100 = True
        kcal = _num(m.group(1))
        if m.group(2):
            p, f, c = _num(m.group(2)), _num(m.group(3) or "0"), _num(m.group(4) or "0")
        s = s[: m.start()] + s[m.end() :]
    m = _RE_BJU_SLASH.search(s)
    if m:
        p, f, c = _num(m.group(1)), _num(m.group(2)), _num(m.group(3))
        s = s[: m.start()] + s[m.end() :]
    else:
        m = _RE_BJU_WORDS.search(s)
        if m:
            p, f, c = _num(m.group(1)), _num(m.group(2)), _num(m.group(3))
            s = s[: m.start()] + s[m.end() :]
    m = _RE_KCAL.search(s)
    if m:
        kcal = _num(m.group(1))
        s = s[: m.start()] + s[m.end() :]
    return re.sub(r"\s+", " ", s).strip(" ·—-\t"), per100, kcal, p, f, c


def parse_macros_snippet(text: str, grams: float) -> Optional[Dict[str, float]]:
    """Только цифры (после «Как на этикетке») — на 100г или на порцию grams."""
    s = (text or "").strip()
    if not s:
        return None
    cleaned, per100, kcal, p, f, c = _extract_macro_fields(s)
    if per100 and kcal is not None:
        data = {"kcal": kcal, "p": p or 0, "f": f or 0, "c": c or 0}
        data = harmonize_macros(data)
        return data if sanity(data) else None
    if kcal is not None or any(x is not None for x in (p, f, c)):
        portion = _macros_per100_from_portion(grams, kcal, p, f, c)
        if portion:
            return portion
    if cleaned:
        nums = re.findall(r"\d+(?:[.,]\d+)?", cleaned)
        if len(nums) >= 4:
            data = harmonize_macros(
                {"kcal": _num(nums[0]), "p": _num(nums[1]), "f": _num(nums[2]), "c": _num(nums[3])}
            )
            return data if sanity(data) else None
        if len(nums) == 1 and kcal is None:
            data = harmonize_macros({"kcal": _num(nums[0]), "p": 0, "f": 0, "c": 0})
            return data if sanity(data) else None
    return None


def parse_meal_with_manual_macros(text: str) -> Optional[Dict[str, Any]]:
    s = (text or "").strip()
    if not s:
        return None
    cleaned, per100, kcal, p, f, c = _extract_macro_fields(s)
    has_macros = per100 or kcal is not None or any(x is not None for x in (p, f, c))
    if not has_macros:
        return None
    local = parse_meal_local(cleaned) or parse_meal_local(s)
    if not local:
        return None
    staple, grams = local
    if per100 and kcal is not None:
        m = harmonize_macros({"kcal": kcal, "p": p or 0, "f": f or 0, "c": c or 0})
    else:
        m = _macros_per100_from_portion(grams, kcal, p, f, c)
    if not m or not sanity(m):
        return None
    return {"staple": staple, "grams": grams, "per100": m}


async def store_manual_macros(staple: str, per100: Dict[str, float]) -> None:
    k = cache_key(staple)
    await cache_store(k, {**per100, "confidence": 1.0}, "ручная")


async def resolve_staple_macros(staple: str) -> Tuple[Dict[str, Any], float, str]:
    custom = await my_product_lookup(staple)
    if custom and macros_consistent(custom):
        return macros_only(custom), 1.0, "мои_продукты"
    k = cache_key(staple)
    hit = await cache_lookup(k)
    if hit and sanity(hit):
        data = harmonize_macros(macros_only(hit))
        if macros_consistent(data):
            conf = hit.get("conf", 0.9)
            if hit.get("src") == "ручная":
                conf = max(conf, 1.0)
            return data, conf, hit.get("src", "cache")
    off = await off_search(staple)
    off_ok = bool(off and off_product_trusted(staple, off))
    if off_ok and all(off.get(x) is not None for x in MEAL_MACRO_KEYS):
        data = harmonize_macros(macros_only(off))
        if sanity(data) and macros_consistent(data):
            await cache_store(k, {**data, "confidence": 0.85}, "off")
            return data, 0.85, "off"
    data, conf = await gemini_macros(staple, off if off_ok else None)
    if off_ok:
        merged = dict(data)
        for kk, vv in {
            "kcal": off.get("kcal"),
            "p": off.get("p"),
            "f": off.get("f"),
            "c": off.get("c"),
        }.items():
            if vv is not None:
                merged[kk] = float(vv)
        data = harmonize_macros(merged)
        if macros_consistent(data):
            conf = max(conf, 0.75)
    if sanity(data) and len((staple or "").strip()) >= 12:
        conf = max(conf, 0.5)
    src = "gemini" if not off_ok else "off+gemini"
    await cache_store(k, {**data, "confidence": conf}, src)
    return macros_only(data), conf, src


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


async def update_meal_row(
    sheet_row: int,
    staple: str,
    grams: float,
    m: Dict[str, float],
) -> Tuple[float, float, float, float]:
    scale = grams / 100.0
    kcal = m.get("kcal", 0) * scale
    p = m.get("p", 0) * scale
    f = m.get("f", 0) * scale
    c = m.get("c", 0) * scale
    ok = await update_sheet_row_cells(
        "Питание",
        sheet_row,
        {
            3: staple,
            4: f"{grams:.0f}",
            5: f"{p:.1f}",
            6: f"{f:.1f}",
            7: f"{c:.1f}",
            8: f"{kcal:.0f}",
        },
    )
    if not ok:
        return 0.0, 0.0, 0.0, 0.0
    return p, f, c, kcal


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
