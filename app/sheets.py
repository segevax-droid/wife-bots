import asyncio
import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import gspread_asyncio
import google.auth.transport.requests
from google.oauth2.service_account import Credentials

from app.config import WIFE_GOOGLE_CREDENTIALS_JSON, WIFE_SHEET_ID, SUPPORT_DIR

logger = logging.getLogger("wife_bots")

_GS_MANAGER: Optional[gspread_asyncio.AsyncioGspreadClientManager] = None
_GS_CLIENT = None
_GS_SPREADSHEET = None
_GS_LOCK = asyncio.Lock()
_WS_CACHE: Dict[str, Any] = {}
_SHARED_CREDS: Optional[Credentials] = None
_CREDS_LOCK = threading.Lock()

_SHEET_VALUES: Dict[str, Tuple[int, float, List[List[str]]]] = {}
_SHEET_GEN: Dict[str, int] = {}
SHEET_READ_TTL = 45
_SETTINGS_CACHE: Optional[Dict[str, str]] = None


def get_credentials() -> Credentials:
    info = json.loads(WIFE_GOOGLE_CREDENTIALS_JSON)
    return Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/spreadsheets"])


def cached_credentials() -> Credentials:
    global _SHARED_CREDS
    with _CREDS_LOCK:
        if _SHARED_CREDS is None:
            _SHARED_CREDS = get_credentials()
        creds = _SHARED_CREDS
        expiry = getattr(creds, "expiry", None)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if (not creds.valid) or expiry is None or expiry <= now + timedelta(seconds=90):
            creds.refresh(google.auth.transport.requests.Request())
        return creds


def invalidate_sheet_cache(sheet_name: str) -> None:
    _SHEET_GEN[sheet_name] = _SHEET_GEN.get(sheet_name, 0) + 1
    _SHEET_VALUES.pop(sheet_name, None)


def reset_sheet_connection() -> None:
    global _GS_SPREADSHEET, _GS_CLIENT
    _GS_SPREADSHEET = None
    _GS_CLIENT = None
    _WS_CACHE.clear()


async def get_spreadsheet():
    global _GS_MANAGER, _GS_CLIENT, _GS_SPREADSHEET
    if not WIFE_SHEET_ID:
        return None
    async with _GS_LOCK:
        if _GS_MANAGER is None:
            _GS_MANAGER = gspread_asyncio.AsyncioGspreadClientManager(cached_credentials)
        agc = await _GS_MANAGER.authorize()
        if _GS_SPREADSHEET is None or agc is not _GS_CLIENT:
            _GS_SPREADSHEET = await agc.open_by_key(WIFE_SHEET_ID)
            _GS_CLIENT = agc
            _WS_CACHE.clear()
        return _GS_SPREADSHEET


async def get_worksheet(sheet_name: str):
    if not WIFE_SHEET_ID:
        return None
    try:
        ss = await get_spreadsheet()
        if not ss:
            return None
        cached = _WS_CACHE.get(sheet_name)
        if cached is not None:
            return cached
        try:
            ws = await ss.worksheet(sheet_name)
            _WS_CACHE[sheet_name] = ws
            return ws
        except Exception:
            pass
        ws = await ss.add_worksheet(title=sheet_name, rows=800, cols=30)
        _WS_CACHE[sheet_name] = ws
        return ws
    except Exception as e:
        logger.error("sheet open %s: %s", sheet_name, e)
        reset_sheet_connection()
        return None


def _rows_from_sheet_result(res) -> Optional[List[List[str]]]:
    if not isinstance(res, list):
        return None
    if res and isinstance(res[0], dict):
        headers = list(res[0].keys())
        rows = [headers]
        for item in res:
            rows.append([str(item.get(h, "")) for h in headers])
        return rows
    return [[str(cell) for cell in row] for row in res]


async def _read_ws_values(ws) -> Optional[List[List[str]]]:
    raw_ws = getattr(ws, "_ws", ws)
    owners = [ws] if raw_ws is ws else [ws, raw_ws]
    for owner in owners:
        for m_name in ("get_all_values", "get_values"):
            if not hasattr(owner, m_name):
                continue
            try:
                func = getattr(owner, m_name)
                if owner is raw_ws and hasattr(ws, "_agcm"):
                    res = await ws._agcm.execute(func)
                else:
                    res = func()
                if asyncio.iscoroutine(res):
                    res = await res
                rows = _rows_from_sheet_result(res)
                if rows is not None:
                    return rows
            except Exception:
                continue
    return None


async def get_ws_values(ws) -> List[List[str]]:
    if not ws:
        return []
    title = str(getattr(getattr(ws, "_ws", ws), "title", "") or getattr(ws, "title", "") or "")
    now = asyncio.get_event_loop().time()
    generation = _SHEET_GEN.get(title, 0)
    cached = _SHEET_VALUES.get(title)
    fresh = bool(title and cached and cached[0] == generation and now - cached[1] < SHEET_READ_TTL)
    if fresh and cached is not None:
        return cached[2]
    rows = await _read_ws_values(ws)
    if rows is None:
        if cached and cached[0] == generation:
            return cached[2]
        return []
    if title and _SHEET_GEN.get(title, 0) == generation:
        _SHEET_VALUES[title] = (generation, now, rows)
    return rows


_SHEET_LOCKS: Dict[str, asyncio.Lock] = {}


def sheet_write_lock(sheet_name: str) -> asyncio.Lock:
    lock = _SHEET_LOCKS.get(sheet_name)
    if lock is None:
        lock = asyncio.Lock()
        _SHEET_LOCKS[sheet_name] = lock
    return lock


async def _update_sheet_cell_unlocked(sheet_name: str, row_idx: int, col_idx: int, val: str) -> bool:
    try:
        ws = await get_worksheet(sheet_name)
        if ws:
            raw_w = getattr(ws, "_ws", ws)
            if hasattr(ws, "_agcm") and hasattr(raw_w, "update_cell"):
                await ws._agcm.execute(raw_w.update_cell, row_idx, col_idx, val)
            elif hasattr(ws, "update_cell"):
                res = ws.update_cell(row_idx, col_idx, val)
                if asyncio.iscoroutine(res):
                    await res
            elif hasattr(raw_w, "update_cell"):
                await asyncio.to_thread(raw_w.update_cell, row_idx, col_idx, val)
            invalidate_sheet_cache(sheet_name)
            return True
    except Exception as e:
        logger.error("cell update %s: %s", sheet_name, e)
    return False


async def _append_row_unlocked(sheet_name: str, row: list) -> Tuple[bool, str]:
    try:
        ws = await get_worksheet(sheet_name)
        if ws:
            raw_w = getattr(ws, "_ws", ws)
            if hasattr(ws, "_agcm") and hasattr(raw_w, "append_row"):
                await ws._agcm.execute(raw_w.append_row, row)
            elif hasattr(ws, "append_row"):
                res = ws.append_row(row)
                if asyncio.iscoroutine(res):
                    await res
            elif hasattr(raw_w, "append_row"):
                await asyncio.to_thread(raw_w.append_row, row)
            invalidate_sheet_cache(sheet_name)
            return True, ""
        return False, "no worksheet"
    except Exception as e:
        return False, str(e)


async def append_row_to_sheet(sheet_name: str, row: list) -> Tuple[bool, str]:
    async with sheet_write_lock(sheet_name):
        return await _append_row_unlocked(sheet_name, row)


async def ensure_sheets() -> None:
    headers = {
        "Сессии": [
            "ID",
            "Дата",
            "Начало",
            "Конец",
            "Длительность_мин",
            "ДО_ЦНС",
            "ДО_суставы",
            "ДО_псих",
            "ПОСЛЕ_ЦНС",
            "ПОСЛЕ_суставы",
            "ПОСЛЕ_70",
            "Боль",
            "Подходов",
            "Название",
            "Тоннаж",
            "Оценка_ккал_трен",
            "Бонус_ккал",
            "JSON_упражнения",
        ],
        "Упражнения": [
            "id",
            "название",
            "синонимы",
            "группа",
            "оборудование",
            "тип",
            "техника",
            "совет1",
            "совет2",
            "совет3",
            "совет4",
        ],
        "Прогресс_упражнений": [
            "id_упр",
            "название",
            "дата",
            "подходов",
            "тоннаж",
            "ср_вес",
            "ср_повт",
            "ср_тяжесть",
            "лучший_подход",
            "JSON_сеты",
        ],
        "План_тренировок": ["дата", "json"],
        "Питание": ["Дата", "Время", "Прием", "Вес_г", "Б", "Ж", "У", "Ккал", "Комментарий"],
        "Вес_Динамика": ["Дата и время", "Вес кг", "Источник", "Комментарий ИИ"],
        "Продукты_кэш": ["ключ", "kcal_100g", "б_100g", "ж_100g", "у_100g", "источник", "уверенность", "json"],
        "Нормы_КБЖУ": [
            "дата",
            "вес",
            "рост",
            "возраст",
            "пол",
            "активность",
            "BMR",
            "TDEE",
            "дефицит_%",
            "ккал",
            "белок",
            "жиры",
            "углеводы",
            "бонус_ккал",
            "метод",
        ],
        "Настройки": ["ключ", "значение"],
        "Состояние": ["ключ", "значение"],
        "Список упражнений": [
            "id",
            "название",
            "синонимы",
            "группа",
            "оборудование",
            "тип",
            "техника",
            "совет1",
            "совет2",
            "совет3",
            "совет4",
        ],
    }
    for name, row in headers.items():
        ws = await get_worksheet(name)
        if not ws:
            continue
        vals = await get_ws_values(ws)
        if not vals or not any(str(c).strip() for c in vals[0]):
            await append_row_to_sheet(name, row)

    await _seed_exercises_if_empty()


async def _seed_exercises_if_empty() -> None:
    import json as js

    ws = await get_worksheet("Упражнения")
    if not ws:
        return
    vals = await get_ws_values(ws)
    data_rows = vals[1:] if vals and len(vals) > 1 else []
    if any(len(r) > 1 and str(r[1]).strip() for r in data_rows):
        return
    path = SUPPORT_DIR / "exercises_seed.json"
    if not path.exists():
        logger.warning("exercises_seed.json missing")
        return
    items = js.loads(path.read_text(encoding="utf-8"))
    rows = [
        [
            it.get("id", ""),
            it.get("name", ""),
            it.get("synonyms", ""),
            it.get("group", ""),
            it.get("equipment", ""),
            it.get("type", ""),
            it.get("technique", ""),
            it.get("tip1", ""),
            it.get("tip2", ""),
            it.get("tip3", ""),
            it.get("tip4", ""),
        ]
        for it in items
    ]
    async with sheet_write_lock("Упражнения"):
        try:
            raw_w = getattr(ws, "_ws", ws)
            if hasattr(ws, "_agcm") and hasattr(raw_w, "append_rows"):
                await ws._agcm.execute(raw_w.append_rows, rows)
            elif hasattr(ws, "append_rows"):
                res = ws.append_rows(rows)
                if asyncio.iscoroutine(res):
                    await res
            else:
                for row in rows:
                    await _append_row_unlocked("Упражнения", row)
            invalidate_sheet_cache("Упражнения")
            logger.info("Seeded %s exercises", len(rows))
        except Exception as e:
            logger.error("seed exercises: %s", e)


async def get_sheet_rows_for_period(sheet_name: str, days: int, date_col: int = 0) -> List[List[str]]:
    from datetime import date, timedelta
    from app.state import now_local

    ws = await get_worksheet(sheet_name)
    if not ws:
        return []
    rows = await get_ws_values(ws)
    if len(rows) < 2:
        return []
    cutoff = now_local().date() - timedelta(days=days - 1)
    out: List[List[str]] = []
    for r in rows[1:]:
        if not r:
            continue
        cell = r[date_col] if len(r) > date_col else ""
        d = _parse_row_date(str(cell))
        if d and d >= cutoff:
            out.append(r)
    return out


def _parse_row_date(raw_cell: str):
    from datetime import datetime

    s = (raw_cell or "").strip()
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if iso:
        try:
            return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).date()
        except Exception:
            return None
    m = re.search(r"(\d{2})[./](\d{2})[./](\d{4})", s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
        except Exception:
            return None
    return None


async def get_settings_map() -> Dict[str, str]:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is not None:
        return dict(_SETTINGS_CACHE)

    async def _inner() -> Dict[str, str]:
        ws = await get_worksheet("Настройки")
        if not ws:
            return {}
        rows = await get_ws_values(ws)
        out: Dict[str, str] = {}
        for r in rows[1:]:
            if len(r) >= 2 and str(r[0]).strip():
                out[str(r[0]).strip()] = str(r[1]).strip()
        return out

    try:
        out = await asyncio.wait_for(_inner(), timeout=12)
        _SETTINGS_CACHE = dict(out)
        return out
    except Exception as e:
        logger.warning("get_settings_map: %s", e)
        return dict(_SETTINGS_CACHE or {})


async def set_setting(key: str, value: str) -> None:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        _SETTINGS_CACHE = {}
    _SETTINGS_CACHE[key] = value
    ws = await get_worksheet("Настройки")
    if not ws:
        return
    rows = await get_ws_values(ws)
    for idx, r in enumerate(rows, start=1):
        if r and str(r[0]).strip() == key:
            await _update_sheet_cell_unlocked("Настройки", idx, 2, value)
            return
    await append_row_to_sheet("Настройки", [key, value])
