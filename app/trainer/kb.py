from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def trainer_main_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Старт тренировки", callback_data="tr_start_flow")],
        [InlineKeyboardButton(text="План тренировки", callback_data="tr_plan_menu")],
        [
            InlineKeyboardButton(text="Отчёт день", callback_data="tr_rep_d"),
            InlineKeyboardButton(text="Отчёт неделя", callback_data="tr_rep_w"),
        ],
        [
            InlineKeyboardButton(text="Отчёт месяц", callback_data="tr_rep_m"),
            InlineKeyboardButton(text="Календарь", callback_data="tr_cal_png"),
        ],
        [
            InlineKeyboardButton(text="Проверить Gemini", callback_data="check_gemini_quota"),
            InlineKeyboardButton(text="Сброс сессии", callback_data="tr_reset_session"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Составить план ИИ", callback_data="tr_gen_plan")],
            [InlineKeyboardButton(text="Последний план", callback_data="tr_last_plan")],
            [InlineKeyboardButton(text="« Меню", callback_data="tr_main")],
        ]
    )


def rest_kb(timer_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="60с", callback_data=f"tr_rest:{timer_id}:60"),
                InlineKeyboardButton(text="90с", callback_data=f"tr_rest:{timer_id}:90"),
                InlineKeyboardButton(text="120с", callback_data=f"tr_rest:{timer_id}:120"),
            ]
        ]
    )


def session_exercise_kb(ex_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подход +1", callback_data=f"tr_set_done:{ex_idx}"),
                InlineKeyboardButton(text="Пропуск сета", callback_data=f"tr_skip_set:{ex_idx}"),
            ],
            [
                InlineKeyboardButton(text="Другое упражнение (тренажёр)", callback_data=f"tr_swap:{ex_idx}"),
                InlineKeyboardButton(text="Аналоги ×3", callback_data=f"tr_analog:{ex_idx}"),
            ],
            [
                InlineKeyboardButton(text="Техника", callback_data=f"tr_tech:{ex_idx}"),
                InlineKeyboardButton(text="Завершить упражнение", callback_data=f"tr_finish_ex:{ex_idx}"),
            ],
            [InlineKeyboardButton(text="Прервать тренировку", callback_data="tr_abort")],
        ]
    )


def scale_1_10(prefix: str) -> InlineKeyboardMarkup:
    r1 = [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}_{i}") for i in range(1, 6)]
    r2 = [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}_{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup(inline_keyboard=[r1, r2])


def scale_set_difficulty_kb(ex_idx: int, set_i: int) -> InlineKeyboardMarkup:
    r1 = [InlineKeyboardButton(text=str(i), callback_data=f"trsd:{ex_idx}:{set_i}:{i}") for i in range(1, 6)]
    r2 = [InlineKeyboardButton(text=str(i), callback_data=f"trsd:{ex_idx}:{set_i}:{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup(inline_keyboard=[r1, r2])


def tech_close_kb(mid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Закрыть", callback_data=f"tr_tech_close:{mid}")]])


def reminder_train_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="План следующей", callback_data="tr_rm_plan"),
                InlineKeyboardButton(text="Лёгкая дома", callback_data="tr_rm_light"),
            ],
            [InlineKeyboardButton(text="Не сейчас", callback_data="tr_rm_no")],
        ]
    )
