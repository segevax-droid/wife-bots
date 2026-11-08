from typing import Any, Dict, List, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _back_row(callback_data: str) -> List[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="« Назад", callback_data=callback_data)]


def trainer_main_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🏋️ Старт тренировки", callback_data="tr_start_flow")],
        [InlineKeyboardButton(text="📋 План тренировки", callback_data="tr_plan_menu")],
        [
            InlineKeyboardButton(text="📅 День", callback_data="tr_rep_d"),
            InlineKeyboardButton(text="📆 Неделя", callback_data="tr_rep_w"),
        ],
        [
            InlineKeyboardButton(text="🗓 Месяц", callback_data="tr_rep_m"),
            InlineKeyboardButton(text="🖼 Календарь", callback_data="tr_cal_png"),
        ],
        [
            InlineKeyboardButton(text="🤖 Gemini", callback_data="check_gemini_quota"),
            InlineKeyboardButton(text="↩️ Сброс", callback_data="tr_reset_session"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Составить план ИИ", callback_data="tr_gen_plan")],
            [InlineKeyboardButton(text="📎 Последний план", callback_data="tr_last_plan")],
            [InlineKeyboardButton(text="« Меню", callback_data="tr_main")],
        ]
    )


def rest_kb(timer_id: str, ex_idx: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
            [
                InlineKeyboardButton(text="⏱ 60с", callback_data=f"tr_rest:{timer_id}:60"),
                InlineKeyboardButton(text="⏱ 90с", callback_data=f"tr_rest:{timer_id}:90"),
                InlineKeyboardButton(text="⏱ 120с", callback_data=f"tr_rest:{timer_id}:120"),
            ],
    ]
    if ex_idx is not None:
        rows.append(_back_row(f"tr_back_ex:{ex_idx}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def analogues_pick_kb(ex_idx: int, items: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for i, ex in enumerate(items[:3]):
        label = (ex.get("name") or ex.get("id", ""))[:36]
        rows.append([InlineKeyboardButton(text=label, callback_data=f"tr_anlg:{ex_idx}:{i}")])
    rows.append([InlineKeyboardButton(text="🔄 Ещё ×3", callback_data=f"tr_analog:{ex_idx}")])
    rows.append(_back_row(f"tr_back_ex:{ex_idx}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def swap_plan_kb(ex_idx: int, exercises: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=x["name"][:34], callback_data=f"tr_pick_swap:{x['exercise_id']}")]
        for x in exercises
    ]
    rows.append(_back_row(f"tr_back_ex:{ex_idx}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def session_exercise_kb(ex_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭ Пропуск", callback_data=f"tr_skip_set:{ex_idx}"),
                InlineKeyboardButton(text="✅ Готово", callback_data=f"tr_finish_ex:{ex_idx}"),
            ],
            [
                InlineKeyboardButton(text="🔀 Из плана", callback_data=f"tr_swap:{ex_idx}"),
                InlineKeyboardButton(text="🔄 Аналоги", callback_data=f"tr_analog:{ex_idx}"),
            ],
            [InlineKeyboardButton(text="📖 Техника", callback_data=f"tr_tech:{ex_idx}")],
            [InlineKeyboardButton(text="🛑 Прервать", callback_data="tr_abort")],
        ]
    )


def scale_1_10(prefix: str, back_cb: Optional[str] = None) -> InlineKeyboardMarkup:
    r1 = [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}_{i}") for i in range(1, 6)]
    r2 = [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}_{i}") for i in range(6, 11)]
    rows: List[List[InlineKeyboardButton]] = [r1, r2]
    if back_cb:
        rows.append(_back_row(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scale_set_difficulty_kb(ex_idx: int, set_i: int) -> InlineKeyboardMarkup:
    r1 = [InlineKeyboardButton(text=str(i), callback_data=f"trsd:{ex_idx}:{set_i}:{i}") for i in range(1, 6)]
    r2 = [InlineKeyboardButton(text=str(i), callback_data=f"trsd:{ex_idx}:{set_i}:{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup(inline_keyboard=[r1, r2, _back_row(f"tr_back_ex:{ex_idx}")])


def pre_begin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Начать тренировку", callback_data="tr_begin_session")],
            _back_row("tr_pre_back:psy"),
        ]
    )


def tech_close_kb(mid: int, ex_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Закрыть", callback_data=f"tr_tech_close:{mid}")],
            _back_row(f"tr_back_ex:{ex_idx}"),
        ]
    )


def reminder_train_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 План", callback_data="tr_rm_plan"),
                InlineKeyboardButton(text="🏠 Лёгкая дома", callback_data="tr_rm_light"),
            ],
            [InlineKeyboardButton(text="⏸ Не сейчас", callback_data="tr_rm_no")],
        ]
    )
