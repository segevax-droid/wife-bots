from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def morning_weigh_prompt_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить утро")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
