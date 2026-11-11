import re
from datetime import datetime
from typing import Callable, Optional, Tuple

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.sheets import get_settings_map, set_setting
from app.state import now_local

DND_ENABLED_KEY = "dnd_enabled"
DND_FROM_KEY = "dnd_from"
DND_TO_KEY = "dnd_to"
DEFAULT_FROM = "22:00"
DEFAULT_TO = "08:00"


class DndSetup(StatesGroup):
    waiting_range = State()


def _parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", (s or "").strip())
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mm <= 59:
        return h, mm
    return None


def fmt_hhmm(h: int, mm: int) -> str:
    return f"{h:02d}:{mm:02d}"


def parse_dnd_range(text: str) -> Optional[Tuple[str, str]]:
    s = (text or "").strip().lower().replace("–", "-").replace("—", "-")
    m = re.search(
        r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})",
        s,
    )
    if not m:
        m = re.search(
            r"(?:с|от)\s*(\d{1,2}:\d{2})\s*(?:до|по)\s*(\d{1,2}:\d{2})",
            s,
        )
    if not m:
        return None
    a, b = _parse_hhmm(m.group(1)), _parse_hhmm(m.group(2))
    if not a or not b:
        return None
    return fmt_hhmm(*a), fmt_hhmm(*b)


def _minutes(h: int, mm: int) -> int:
    return h * 60 + mm


def is_in_dnd_window(
    at: datetime,
    *,
    enabled: bool,
    from_hhmm: str,
    to_hhmm: str,
) -> bool:
    if not enabled:
        return False
    start = _parse_hhmm(from_hhmm)
    end = _parse_hhmm(to_hhmm)
    if not start or not end:
        return False
    now_m = _minutes(at.hour, at.minute)
    s_m = _minutes(*start)
    e_m = _minutes(*end)
    if s_m == e_m:
        return False
    if s_m > e_m:
        return now_m >= s_m or now_m < e_m
    return s_m <= now_m < e_m


async def is_dnd_active(at: Optional[datetime] = None) -> bool:
    s = await get_settings_map()
    enabled = str(s.get(DND_ENABLED_KEY, "")).strip() in ("1", "true", "yes", "on")
    return is_in_dnd_window(
        at or now_local(),
        enabled=enabled,
        from_hhmm=str(s.get(DND_FROM_KEY, DEFAULT_FROM) or DEFAULT_FROM),
        to_hhmm=str(s.get(DND_TO_KEY, DEFAULT_TO) or DEFAULT_TO),
    )


def dnd_status_message(s: dict) -> str:
    enabled = str(s.get(DND_ENABLED_KEY, "")).strip() in ("1", "true", "yes", "on")
    fr = str(s.get(DND_FROM_KEY, DEFAULT_FROM) or DEFAULT_FROM)
    to = str(s.get(DND_TO_KEY, DEFAULT_TO) or DEFAULT_TO)
    if enabled:
        active = is_in_dnd_window(
            now_local(),
            enabled=True,
            from_hhmm=fr,
            to_hhmm=to,
        )
        tail = " <i>(сейчас тихо)</i>" if active else ""
        return (
            "🌙 <b>Не беспокоить</b> — <b>включено</b>\n"
            f"Интервал: <b>{fr}</b> → <b>{to}</b>{tail}\n\n"
            "В это время боты не шлют напоминания (утро, еда, тренировки, уведомления между ботами)."
        )
    return (
        "🌙 <b>Не беспокоить</b> — <b>выключено</b>\n"
        f"Последний интервал: {fr} → {to}\n\n"
        "Включи, если ночью и рано утром не хочешь пингов."
    )


def dnd_menu_kb(prefix: str, *, enabled: bool) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(text="🔕 Выключить", callback_data=f"{prefix}_dnd:off")
        if enabled
        else InlineKeyboardButton(text="🔔 Включить", callback_data=f"{prefix}_dnd:on")
    )
    back = f"{prefix}_main" if prefix == "tr" else "nut_menu"
    rows = [
        [toggle],
        [
            InlineKeyboardButton(text="22:00–08:00", callback_data=f"{prefix}_dnd:p:2200-0800"),
            InlineKeyboardButton(text="23:00–07:00", callback_data=f"{prefix}_dnd:p:2300-0700"),
        ],
        [InlineKeyboardButton(text="✏️ Свой интервал", callback_data=f"{prefix}_dnd:custom")],
        [InlineKeyboardButton(text="« Меню", callback_data=back)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _apply_preset(preset: str) -> Tuple[str, str]:
    m = re.match(r"^(\d{2})(\d{2})-(\d{2})(\d{2})$", preset)
    if not m:
        return DEFAULT_FROM, DEFAULT_TO
    return f"{m.group(1)}:{m.group(2)}", f"{m.group(3)}:{m.group(4)}"


def attach_dnd_handlers(
    router: Router,
    *,
    prefix: str,
    main_menu_kb: Callable[[], InlineKeyboardMarkup],
) -> None:
    back_cb = "tr_main" if prefix == "tr" else "nut_menu"

    @router.callback_query(F.data == f"{prefix}_dnd")
    async def cb_dnd_menu(call: CallbackQuery, state: FSMContext):
        await state.clear()
        s = await get_settings_map()
        await call.message.answer(dnd_status_message(s), reply_markup=dnd_menu_kb(prefix, enabled=_enabled(s)))
        await call.answer()

    @router.callback_query(F.data == f"{prefix}_dnd:off")
    async def cb_dnd_off(call: CallbackQuery, state: FSMContext):
        await state.clear()
        await set_setting(DND_ENABLED_KEY, "0")
        s = await get_settings_map()
        await call.message.answer("🔕 Режим «Не беспокоить» выключен.", reply_markup=dnd_menu_kb(prefix, enabled=False))
        await call.answer()

    @router.callback_query(F.data == f"{prefix}_dnd:on")
    async def cb_dnd_on(call: CallbackQuery, state: FSMContext):
        await state.clear()
        s = await get_settings_map()
        fr = str(s.get(DND_FROM_KEY, "")).strip() or DEFAULT_FROM
        to = str(s.get(DND_TO_KEY, "")).strip() or DEFAULT_TO
        await set_setting(DND_ENABLED_KEY, "1")
        if not str(s.get(DND_FROM_KEY, "")).strip():
            await set_setting(DND_FROM_KEY, fr)
        if not str(s.get(DND_TO_KEY, "")).strip():
            await set_setting(DND_TO_KEY, to)
        s = await get_settings_map()
        await call.message.answer(dnd_status_message(s), reply_markup=dnd_menu_kb(prefix, enabled=True))
        await call.answer()

    @router.callback_query(F.data.startswith(f"{prefix}_dnd:p:"))
    async def cb_dnd_preset(call: CallbackQuery, state: FSMContext):
        await state.clear()
        preset = call.data.split(":", 2)[-1]
        fr, to = await _apply_preset(preset)
        await set_setting(DND_FROM_KEY, fr)
        await set_setting(DND_TO_KEY, to)
        await set_setting(DND_ENABLED_KEY, "1")
        s = await get_settings_map()
        await call.message.answer(
            f"🌙 Включено: <b>{fr}</b> → <b>{to}</b>",
            reply_markup=dnd_menu_kb(prefix, enabled=True),
        )
        await call.answer()

    @router.callback_query(F.data == f"{prefix}_dnd:custom")
    async def cb_dnd_custom(call: CallbackQuery, state: FSMContext):
        await state.set_state(DndSetup.waiting_range)
        await call.message.answer(
            "Напиши интервал одной строкой.\n"
            "Примеры: <code>22:00-08:00</code> или <code>с 23:00 до 07:00</code>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data=back_cb)]]
            ),
        )
        await call.answer()

    @router.message(DndSetup.waiting_range, F.text)
    async def msg_dnd_range(message: Message, state: FSMContext):
        parsed = parse_dnd_range(message.text or "")
        if not parsed:
            await message.answer(
                "Не разобрала время. Формат: <code>22:00-08:00</code>",
            )
            return
        fr, to = parsed
        await set_setting(DND_FROM_KEY, fr)
        await set_setting(DND_TO_KEY, to)
        await set_setting(DND_ENABLED_KEY, "1")
        await state.clear()
        s = await get_settings_map()
        await message.answer(dnd_status_message(s), reply_markup=dnd_menu_kb(prefix, enabled=True))

    # nutrition needs nut_menu callback to return to main
    if prefix == "nut":

        @router.callback_query(F.data == "nut_menu")
        async def cb_nut_menu(call: CallbackQuery, state: FSMContext):
            await state.clear()
            await call.message.answer("Меню:", reply_markup=main_menu_kb())
            await call.answer()


def _enabled(s: dict) -> bool:
    return str(s.get(DND_ENABLED_KEY, "")).strip() in ("1", "true", "yes", "on")
