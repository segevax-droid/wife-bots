import asyncio
import json
import re
from typing import Dict, Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.common import record_user_chat
from app.dnd import attach_dnd_handlers
from app.gemini import call_gemini, download_file_as_base64, transcribe_voice_message
from app.nutrition.food import (
    caption_is_weak_food_hint,
    delete_meal,
    find_meal_by_id,
    list_today_meals,
    log_meal_row,
    meal_button_label,
    parse_meal_local,
    parse_meal_grams,
    resolve_staple_macros,
    sanity,
)
from app.nutrition.norms import (
    FAT_G_KG_MAX,
    FAT_G_KG_MIN,
    MACRO_G_KG_STEP,
    PROT_G_KG_MAX,
    PROT_G_KG_MIN,
    clamp_fat_g_kg,
    clamp_prot_g_kg,
    fat_g_kg_from_rest_share,
    load_norm_targets,
    read_macro_prefs,
    recalc_norms_for_user,
    today_intake,
)
from app.nutrition.ux import bank_message, meal_logged_praise
from app.sheets import append_row_to_sheet, get_settings_map, get_sheet_rows_for_period, set_setting
from app.state import now_local

nutrition_router = Router(name="nutrition")


class NutOnboard(StatesGroup):
    height = State()
    age = State()
    sex = State()
    activity = State()
    goal = State()


class NutWeigh(StatesGroup):
    waiting = State()


def _main_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⚖️ Записать вес", callback_data="nut_weigh")],
        [
            InlineKeyboardButton(text="💰 Копилка дня", callback_data="nut_bank"),
            InlineKeyboardButton(text="🍽 Приёмы сегодня", callback_data="nut_meals"),
        ],
        [InlineKeyboardButton(text="🥗 Баланс БЖУ", callback_data="nut_bju")],
        [InlineKeyboardButton(text="🛒 Список покупок", callback_data="nut_shop")],
        [InlineKeyboardButton(text="👤 Профиль заново", callback_data="nut_onboard")],
        [InlineKeyboardButton(text="🌙 Не беспокоить", callback_data="nut_dnd")],
        [InlineKeyboardButton(text="🤖 Проверить Gemini", callback_data="check_gemini_quota")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _weigh_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data="nut_weigh_cancel")]]
    )


def _after_food_kb(meal_id: str = "") -> InlineKeyboardMarkup:
    rows = []
    if meal_id:
        rows.append([InlineKeyboardButton(text="🗑 Отменить запись", callback_data=f"nut_mu:{meal_id}")])
    rows.append(
        [
            InlineKeyboardButton(text="💰 Копилка", callback_data="nut_bank"),
            InlineKeyboardButton(text="⚖️ Вес", callback_data="nut_weigh"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _meal_delete_cb(meal: Dict) -> str:
    if meal.get("meal_id"):
        return f"nut_md:{meal['meal_id']}"
    return f"nut_mr:{meal['sheet_row']}"


def _meals_list_kb(meals: list) -> InlineKeyboardMarkup:
    rows = []
    for meal in reversed(meals[-12:]):
        rows.append([InlineKeyboardButton(text=meal_button_label(meal), callback_data=_meal_delete_cb(meal))])
    rows.append([InlineKeyboardButton(text="« Меню", callback_data="nut_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_delete_kb(*, meal_id: str = "", sheet_row: int = 0) -> InlineKeyboardMarkup:
    if meal_id:
        yes = f"nut_mdy:{meal_id}"
    else:
        yes = f"nut_mry:{sheet_row}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=yes),
                InlineKeyboardButton(text="Нет", callback_data="nut_meals"),
            ]
        ]
    )


def _meals_text(meals: list) -> str:
    if not meals:
        return (
            "🍽 <b>Сегодня ещё нет записей</b>\n"
            "Напиши еду текстом, голосом или фото — и сможешь сразу отменить, если ошиблась."
        )
    lines = ["🍽 <b>Приёмы сегодня</b>", "Нажми на запись, чтобы удалить.\n"]
    for meal in meals:
        t = meal.get("time") or "—"
        lines.append(
            f"• {t}  <b>{meal['name']}</b> — {meal['grams']:.0f} г · {meal['kcal']:.0f} ккал"
        )
    return "\n".join(lines)


def _deleted_text(meal: Dict) -> str:
    t = meal.get("time") or ""
    when = f"{t} " if t else ""
    return (
        f"🗑 Удалила {when}<b>{meal.get('name', 'приём')}</b> "
        f"({meal.get('grams', 0):.0f} г · {meal.get('kcal', 0):.0f} ккал).\n"
        "Копилка дня пересчитается без этой записи."
    )


def _extract_weight_kg(text: str) -> Optional[float]:
    s = (text or "").strip().replace(",", ".")
    if re.fullmatch(r"\d{2,3}(?:\.\d)?", s):
        return float(s)
    m = re.search(
        r"(?:вес|масса(?:\s+тела)?|кг|весы)\D{0,12}(\d{2,3}(?:\.\d)?)",
        s,
        re.I,
    )
    if m:
        return float(m.group(1))
    m = re.search(r"(\d{2,3}(?:\.\d)?)\s*кг\b", s, re.I)
    if m:
        return float(m.group(1))
    return None


async def _save_weight(message: types.Message, w: float, source: str = "manual") -> None:
    s = await get_settings_map()
    had_weight = bool(str(s.get("weight_kg", "")).strip())
    await set_setting("weight_kg", f"{w:.1f}")
    await append_row_to_sheet(
        "Вес_Динамика",
        [now_local().strftime("%Y-%m-%d %H:%M"), f"{w:.1f}", source, ""],
    )
    reason = "weight" if had_weight else "onboarding"
    msg = await recalc_norms_for_user({**s, "weight_kg": f"{w:.1f}"}, reason=reason)
    await message.answer(f"⚖️ Отлично, вес <b>{w:.1f} кг</b> — записала ✨")
    if msg:
        await message.answer(msg)
    await message.answer("Чем ещё помочь?", reply_markup=_main_kb())


def _sex_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Ж", callback_data="nut_sex:ж"),
                InlineKeyboardButton(text="М", callback_data="nut_sex:м"),
            ],
            [InlineKeyboardButton(text="« Назад", callback_data="nut_ob_back:age")],
        ]
    )


def _act_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in [("sed", "Мало"), ("light", "Лёгкая"), ("mod", "Умер."), ("high", "Высок.")]:
        b.button(text=label, callback_data=f"nut_act:{code}")
    b.button(text="« Назад", callback_data="nut_ob_back:sex")
    b.adjust(2)
    return b.as_markup()


def _goal_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Похудение", callback_data="nut_goal:cut"),
                InlineKeyboardButton(text="Поддержание", callback_data="nut_goal:maint"),
            ],
            [InlineKeyboardButton(text="« Назад", callback_data="nut_ob_back:act")],
        ]
    )


def _profile_base_ok(s: Dict[str, str]) -> bool:
    keys = ("height_cm", "age", "sex", "activity", "goal")
    return all(bool(str(s.get(k, "")).strip()) for k in keys)


async def _settings_complete(s: Dict[str, str]) -> bool:
    return _profile_base_ok(s) and bool(str(s.get("weight_kg", "")).strip())


async def _process_food_text(message: types.Message, text_override: Optional[str] = None) -> None:
    s = await get_settings_map()
    if not await _settings_complete(s):
        await message.answer("Сначала заверши онбординг (/start).")
        return
    txt = (text_override or message.text or "").strip()
    staple, grams = await parse_meal_grams(txt)
    m, conf, _ = await resolve_staple_macros(staple)
    if parse_meal_local(txt) and sanity(m):
        conf = max(conf, 0.55)
    if not sanity(m):
        await message.answer(
            "Не смогла оценить КБЖУ по этому описанию.\n"
            "Напиши продукт и вес, например: <code>сэндвич с курицей 180 г</code>"
        )
        return
    low_conf = conf < 0.45
    *_, meal_id = await log_meal_row(staple, grams, m)
    kcal = m["kcal"] * grams / 100
    approx = ""
    if low_conf:
        approx = "⚠️ <i>Оценка приблизительная — при необходимости отмени запись кнопкой ниже.</i>\n\n"
    await message.answer(
        f"{approx}{meal_logged_praise(kcal, s.get('goal', ''))}\n\n"
        f"<b>{staple}</b> — {grams:.0f} г\n"
        f"≈ <b>{kcal:.0f}</b> ккал · Б <b>{m['p']*grams/100:.1f}</b> / "
        f"Ж <b>{m['f']*grams/100:.1f}</b> / У <b>{m['c']*grams/100:.1f}</b> г",
        reply_markup=_after_food_kb(meal_id),
    )


@nutrition_router.message(Command("start"))
@nutrition_router.message(Command("menu"))
async def nut_start(message: types.Message, state: FSMContext):
    await state.clear()
    await record_user_chat("nutrition", message.chat.id)
    s = await get_settings_map()
    if not _profile_base_ok(s):
        await state.set_state(NutOnboard.height)
        await message.answer(
            "<b>Профиль</b>\nНапиши рост в сантиметрах одним числом.\nПример: <code>165</code>"
        )
        return
    if not str(s.get("weight_kg", "")).strip():
        await state.set_state(NutWeigh.waiting)
        await message.answer(
            "Остался вес — напиши кг одним числом.\nПример: <code>90</code>",
            reply_markup=_weigh_kb(),
        )
        return
    await message.answer(
        "Привет! Я рядом с питанием 💛\n\n"
        "🍽 <b>Еда</b> — текст, голос или фото («овсянка 200г»)\n"
        "🗑 Ошибочный приём — кнопка сразу после записи или «Приёмы сегодня»\n"
        "⚖️ <b>Вес</b> — кнопка или число <code>90</code>\n"
        "💰 <b>Копилка</b> — красивый прогресс БЖУ за день\n"
        "🥗 <b>Баланс БЖУ</b> — белок г/кг и жиры/углеводы при тех же калориях\n"
        "🛒 <b>Покупки</b> — список от ИИ под твою цель\n\n"
        "Каждое утро в 8:30 мягко напомню взвеситься ☀️",
        reply_markup=_main_kb(),
    )


@nutrition_router.message(NutOnboard.height, F.text)
async def ob_height(message: types.Message, state: FSMContext):
    if not re.match(r"^\d{2,3}$", message.text.strip()):
        await message.answer("Рост: целое число см, например 165")
        return
    await set_setting("height_cm", message.text.strip())
    await state.set_state(NutOnboard.age)
    await message.answer("Возраст (полных лет числом). Можно /start — начать сначала.")


@nutrition_router.message(NutOnboard.age, F.text)
async def ob_age(message: types.Message, state: FSMContext):
    if not re.match(r"^\d{1,3}$", message.text.strip()):
        await message.answer("Возраст числом")
        return
    await set_setting("age", message.text.strip())
    await state.set_state(NutOnboard.sex)
    await message.answer("Пол:", reply_markup=_sex_kb())


@nutrition_router.callback_query(NutOnboard.sex, F.data.startswith("nut_sex:"))
async def ob_sex(call: types.CallbackQuery, state: FSMContext):
    sex = call.data.split(":", 1)[1]
    await set_setting("sex", sex)
    await state.set_state(NutOnboard.activity)
    await call.message.answer("Активность:", reply_markup=_act_kb())
    await call.answer()


@nutrition_router.callback_query(NutOnboard.activity, F.data.startswith("nut_act:"))
async def ob_act(call: types.CallbackQuery, state: FSMContext):
    act = call.data.split(":", 1)[1]
    await set_setting("activity", act)
    await state.set_state(NutOnboard.goal)
    await call.message.answer("Цель:", reply_markup=_goal_kb())
    await call.answer()


@nutrition_router.callback_query(NutOnboard.goal, F.data.startswith("nut_goal:"))
async def ob_goal(call: types.CallbackQuery, state: FSMContext):
    goal = call.data.split(":", 1)[1]
    await set_setting("goal", goal)
    await state.set_state(NutWeigh.waiting)
    await call.message.answer(
        "Теперь текущий вес — одним числом кг.\nПример: <code>90</code> или <code>89.5</code>",
        reply_markup=_weigh_kb(),
    )
    await call.answer()


@nutrition_router.callback_query(F.data.startswith("nut_ob_back:"))
async def ob_back(call: types.CallbackQuery, state: FSMContext):
    step = call.data.split(":")[-1]
    if step == "age":
        await state.set_state(NutOnboard.height)
        await call.message.answer("Рост в см одним числом.\nПример: <code>165</code>")
    elif step == "sex":
        await state.set_state(NutOnboard.age)
        await call.message.answer("Возраст (полных лет числом).")
    else:
        await state.set_state(NutOnboard.sex)
        await call.message.answer("Пол:", reply_markup=_sex_kb())
    await call.answer()


@nutrition_router.callback_query(F.data == "nut_onboard")
async def redo_ob(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(NutOnboard.height)
    await call.message.answer("Рост в см.")
    await call.answer()


@nutrition_router.callback_query(F.data == "nut_weigh")
async def cb_weigh(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(NutWeigh.waiting)
    await call.message.answer(
        "Напиши вес в кг одним числом.\nПример: <code>90</code> или <code>89.5</code>",
        reply_markup=_weigh_kb(),
    )
    await call.answer()


@nutrition_router.callback_query(F.data == "nut_weigh_cancel")
async def cb_weigh_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Ок, вес не записываю.", reply_markup=_main_kb())
    await call.answer()


@nutrition_router.message(NutWeigh.waiting, F.text | F.voice)
async def weigh_waiting(message: types.Message, state: FSMContext, bot: Bot):
    txt = message.text or ""
    if message.voice:
        txt = await transcribe_voice_message(bot, message) or ""
    w = _extract_weight_kg(txt)
    if w is None:
        m = re.search(r"(\d{2,3}(?:[.,]\d)?)", txt)
        if m:
            w = float(m.group(1).replace(",", "."))
    if w is None or w < 30 or w > 250:
        await message.answer("Не увидела вес. Пришли число, например <code>90</code>.", reply_markup=_weigh_kb())
        return
    await state.clear()
    await _save_weight(message, w)


@nutrition_router.message(StateFilter(None), F.text.regexp(r"^\s*\d{2,3}([.,]\d)?\s*$"))
async def weight_number(message: types.Message):
    s = await get_settings_map()
    if not _profile_base_ok(s):
        return
    w = float(message.text.replace(",", ".").strip())
    if w < 30 or w > 250:
        await _process_food_text(message)
        return
    await _save_weight(message, w)


@nutrition_router.message(StateFilter(None), F.text & ~F.text.regexp(r"^\s*\d{2,3}([.,]\d)?\s*$"))
async def food_text_free(message: types.Message):
    s = await get_settings_map()
    if _profile_base_ok(s) and not str(s.get("weight_kg", "")).strip():
        await message.answer("Сначала запиши вес — кнопкой или числом.")
        return
    w = _extract_weight_kg(message.text or "")
    if w is not None and w >= 30 and w <= 250:
        # «Масса тела: 90» / «вес 90» — не еда
        await _save_weight(message, w)
        return
    await _process_food_text(message)


@nutrition_router.message(StateFilter(None), F.voice)
async def food_voice(message: types.Message, bot: Bot):
    t = await transcribe_voice_message(bot, message)
    if not t:
        await message.answer("Не расшифровала голос. Напиши текстом?")
        return
    w = _extract_weight_kg(t)
    if w is not None and 30 <= w <= 250:
        await _save_weight(message, w, source="voice")
        return
    await _process_food_text(message, text_override=t)


async def _food_text_from_photo_caption(caption: str, b64: str) -> str:
    cap = (caption or "").strip()
    if not caption_is_weak_food_hint(cap):
        return cap
    desc = await call_gemini(
        "Опиши еду на фото: название блюда, ключевые ингредиенты, примерный вес в граммах если видно. "
        "Одна строка на русском, без вступлений.",
        media_b64=b64,
        mime_type="image/jpeg",
    )
    desc = (desc or "").strip()
    if cap and not caption_is_weak_food_hint(cap):
        return f"{desc}. {cap}"
    return desc or cap


@nutrition_router.message(StateFilter(None), F.photo)
async def photo_any(message: types.Message, bot: Bot):
    b64 = await download_file_as_base64(bot, message.photo[-1].file_id)
    if message.caption:
        text = await _food_text_from_photo_caption(message.caption, b64)
        if not text.strip():
            await message.answer("Не разобрала фото. Напиши подписью, что это и сколько грамм.")
            return
        await _process_food_text(message, text_override=text)
        return
    raw_w = await call_gemini("На фото весы? Если да — число кг одним числом, иначе слово НЕТ.", media_b64=b64, mime_type="image/jpeg")
    m = re.search(r"(\d{2}(?:\.\d)?)", raw_w)
    s = await get_settings_map()
    if m and "НЕТ" not in raw_w.upper():
        w = float(m.group(1).replace(",", "."))
        if 30 <= w <= 250:
            await _save_weight(message, w, source="photo")
            return
    desc = await call_gemini("Опиши еду на фото одним коротким предложением на русском.", media_b64=b64, mime_type="image/jpeg")
    await _process_food_text(message, text_override=desc)


def _bju_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="− белок", callback_data="nut_bju_p:-"),
                InlineKeyboardButton(text="+ белок", callback_data="nut_bju_p:+"),
            ],
            [
                InlineKeyboardButton(text="− жиры", callback_data="nut_bju_f:-"),
                InlineKeyboardButton(text="+ жиры", callback_data="nut_bju_f:+"),
            ],
            [
                InlineKeyboardButton(text="Больше У", callback_data="nut_bju_cf:30"),
                InlineKeyboardButton(text="Баланс", callback_data="nut_bju_cf:45"),
                InlineKeyboardButton(text="Больше Ж", callback_data="nut_bju_cf:60"),
            ],
            [InlineKeyboardButton(text="« Меню", callback_data="nut_menu")],
        ]
    )


def _bju_text(tgt: Dict[str, float]) -> str:
    p_kg = tgt["prot_g_kg"]
    f_kg = tgt["fat_g_kg"]
    kcal = tgt["kcal"]
    pk = tgt["p"] * 4
    fk = tgt["f"] * 9
    ck = tgt["c"] * 4
    p_pct = pk / kcal * 100 if kcal > 0 else 0
    f_pct = fk / kcal * 100 if kcal > 0 else 0
    c_pct = ck / kcal * 100 if kcal > 0 else 0
    note = ""
    if tgt.get("clamped"):
        note = (
            "\n\n⚠️ При выбранном белке жиры уперлись в остаток калорий — "
            "углеводы = 0, сумма ккал не растёт. Уменьши белок или жиры."
        )
    return (
        f"🥗 <b>Баланс БЖУ</b>\n"
        f"Калории считает бот — <b>{kcal:.0f} ккал</b> не меняются.\n"
        f"Ты задаёшь белок на кг веса, жиры и углеводы делят остаток.\n\n"
        f"Белок: <b>{p_kg:.1f} г/кг</b> → {tgt['p']:.0f} г ({p_pct:.0f}% ккал)\n"
        f"Жиры: <b>{f_kg:.1f} г/кг</b> → {tgt['f']:.0f} г ({f_pct:.0f}% ккал)\n"
        f"Углеводы: остаток → {tgt['c']:.0f} г ({c_pct:.0f}% ккал)\n\n"
        f"Шаг ±{MACRO_G_KG_STEP:.1f} г/кг. Белок {PROT_G_KG_MIN:.1f}–{PROT_G_KG_MAX:.1f}, "
        f"жиры {FAT_G_KG_MIN:.1f}–{FAT_G_KG_MAX:.1f}.\n"
        f"<i>Больше У / Баланс / Больше Ж</i> — доля жиров в калориях после белка "
        f"(30% / 45% / 60%)."
        f"{note}"
    )


async def _show_bju(call: types.CallbackQuery, *, edit: bool) -> None:
    s = await get_settings_map()
    tgt = await load_norm_targets(s)
    if not tgt:
        await call.answer("Сначала заверши профиль и вес", show_alert=True)
        return
    text = _bju_text(tgt)
    kb = _bju_kb()
    if edit:
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await call.message.answer(text, reply_markup=kb)
    else:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@nutrition_router.callback_query(F.data == "nut_bju")
async def cb_bju(call: types.CallbackQuery):
    await _show_bju(call, edit=False)


@nutrition_router.callback_query(F.data.startswith("nut_bju_p:"))
async def cb_bju_prot(call: types.CallbackQuery):
    s = await get_settings_map()
    p, f_ = read_macro_prefs(s)
    delta = MACRO_G_KG_STEP if call.data.endswith(":+") else -MACRO_G_KG_STEP
    nxt = clamp_prot_g_kg(p + delta)
    if nxt == p:
        await call.answer("Граница диапазона")
        return
    await set_setting("prot_g_kg", f"{nxt:.1f}")
    await recalc_norms_for_user({**s, "prot_g_kg": f"{nxt:.1f}"}, reason="macros", consume_bonus=False)
    await _show_bju(call, edit=True)


@nutrition_router.callback_query(F.data.startswith("nut_bju_f:"))
async def cb_bju_fat(call: types.CallbackQuery):
    s = await get_settings_map()
    p, f_ = read_macro_prefs(s)
    delta = MACRO_G_KG_STEP if call.data.endswith(":+") else -MACRO_G_KG_STEP
    nxt = clamp_fat_g_kg(f_ + delta)
    if nxt == f_:
        await call.answer("Граница диапазона")
        return
    await set_setting("fat_g_kg", f"{nxt:.1f}")
    await recalc_norms_for_user({**s, "fat_g_kg": f"{nxt:.1f}"}, reason="macros", consume_bonus=False)
    await _show_bju(call, edit=True)


@nutrition_router.callback_query(F.data.startswith("nut_bju_cf:"))
async def cb_bju_carb_fat(call: types.CallbackQuery):
    s = await get_settings_map()
    tgt = await load_norm_targets(s)
    if not tgt:
        await call.answer("Сначала заверши профиль и вес", show_alert=True)
        return
    try:
        fat_pct = int(call.data.split(":")[1])
    except ValueError:
        await call.answer("Не поняла долю", show_alert=True)
        return
    p_kg, _ = read_macro_prefs(s)
    nxt = fat_g_kg_from_rest_share(tgt["weight_kg"], tgt["base_kcal"], p_kg, fat_pct)
    await set_setting("fat_g_kg", f"{nxt:.1f}")
    await recalc_norms_for_user({**s, "fat_g_kg": f"{nxt:.1f}"}, reason="macros", consume_bonus=False)
    await _show_bju(call, edit=True)


@nutrition_router.callback_query(F.data == "nut_bank")
async def cb_bank(call: types.CallbackQuery):
    s = await get_settings_map()
    tgt = await load_norm_targets(s)
    if not tgt:
        await call.answer("Нет профиля", show_alert=True)
        return
    p, f_, c, k = await today_intake()
    text = bank_message(p, f_, c, k, tgt)
    extra = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить приём", callback_data="nut_meals")],
            [InlineKeyboardButton(text="« Меню", callback_data="nut_menu")],
        ]
    )
    await call.message.answer(text, reply_markup=extra)
    await call.answer()


@nutrition_router.callback_query(F.data == "nut_shop")
async def cb_shop(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer("🛒 Составляю список покупок под твою цель…")
    rows = await get_sheet_rows_for_period("Питание", 4, date_col=0)
    sample = "\n".join([str(r[2]) for r in rows[-12:] if len(r) > 2])
    s = await get_settings_map()
    prompt = f"Список покупок на ~4 дня, кратко маркированный. Цель: {s.get('goal')}. Недавние приёмы:\n{sample}"
    await call.message.answer(await call_gemini(prompt))


@nutrition_router.callback_query(F.data == "nut_meals")
async def cb_meals(call: types.CallbackQuery):
    meals = await list_today_meals()
    await call.message.answer(_meals_text(meals), reply_markup=_meals_list_kb(meals))
    await call.answer()


@nutrition_router.callback_query(F.data.startswith("nut_md:"))
async def cb_meal_delete_ask_id(call: types.CallbackQuery):
    meal_id = call.data.split(":", 1)[1]
    meal = await find_meal_by_id(meal_id)
    if not meal:
        await call.answer("Запись уже удалена", show_alert=True)
        meals = await list_today_meals()
        await call.message.answer(_meals_text(meals), reply_markup=_meals_list_kb(meals))
        return
    await call.message.answer(
        f"Удалить <b>{meal['name']}</b> ({meal['grams']:.0f} г · {meal['kcal']:.0f} ккал)?",
        reply_markup=_confirm_delete_kb(meal_id=meal_id),
    )
    await call.answer()


@nutrition_router.callback_query(F.data.startswith("nut_mr:"))
async def cb_meal_delete_ask_row(call: types.CallbackQuery):
    try:
        sheet_row = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Не поняла запись", show_alert=True)
        return
    meals = await list_today_meals()
    meal = next((m for m in meals if m["sheet_row"] == sheet_row), None)
    if not meal:
        await call.answer("Запись уже удалена", show_alert=True)
        await call.message.answer(_meals_text(meals), reply_markup=_meals_list_kb(meals))
        return
    await call.message.answer(
        f"Удалить <b>{meal['name']}</b> ({meal['grams']:.0f} г · {meal['kcal']:.0f} ккал)?",
        reply_markup=_confirm_delete_kb(sheet_row=sheet_row),
    )
    await call.answer()


async def _finish_meal_delete(call: types.CallbackQuery, deleted: Optional[Dict]) -> None:
    if not deleted:
        await call.answer("Уже удалено или не нашла запись", show_alert=True)
        meals = await list_today_meals()
        await call.message.answer(_meals_text(meals), reply_markup=_meals_list_kb(meals))
        return
    meals = await list_today_meals()
    text = _deleted_text(deleted)
    if meals:
        text += "\n\n" + _meals_text(meals)
    await call.message.answer(text, reply_markup=_meals_list_kb(meals))
    await call.answer("Удалила")


@nutrition_router.callback_query(F.data.startswith("nut_mdy:"))
async def cb_meal_delete_yes_id(call: types.CallbackQuery):
    meal_id = call.data.split(":", 1)[1]
    deleted = await delete_meal(meal_id=meal_id)
    await _finish_meal_delete(call, deleted)


@nutrition_router.callback_query(F.data.startswith("nut_mry:"))
async def cb_meal_delete_yes_row(call: types.CallbackQuery):
    try:
        sheet_row = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Не поняла запись", show_alert=True)
        return
    deleted = await delete_meal(sheet_row=sheet_row)
    await _finish_meal_delete(call, deleted)


@nutrition_router.callback_query(F.data.startswith("nut_mu:"))
async def cb_meal_undo(call: types.CallbackQuery):
    meal_id = call.data.split(":", 1)[1]
    deleted = await delete_meal(meal_id=meal_id)
    if not deleted:
        await call.answer("Уже отменено", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=_after_food_kb())
    except Exception:
        pass
    await call.message.answer(_deleted_text(deleted), reply_markup=_main_kb())
    await call.answer("Отменила")


attach_dnd_handlers(nutrition_router, prefix="nut", main_menu_kb=_main_kb)
