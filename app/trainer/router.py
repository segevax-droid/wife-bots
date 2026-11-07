import asyncio
import json
import random
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.common import record_user_chat, send_cross_notification
from app.config import BONUS_CARB_FRAC
from app.gemini import call_gemini, download_file_as_base64, transcribe_voice_message
from app.sheets import append_row_to_sheet, get_settings_map, get_worksheet, get_ws_values
from app.state import SYSTEM_STATE, now_local, save_state_pairs
from app.trainer.calendar_png import current_month_tonnage_map, month_png_bytes
from app.trainer.energy import estimate_session_kcal
from app.trainer.exercise_db import ExerciseDB
from app.trainer.kb import (
    analogues_pick_kb,
    plan_menu_kb,
    pre_begin_kb,
    rest_kb,
    scale_1_10,
    scale_set_difficulty_kb,
    session_exercise_kb,
    swap_plan_kb,
    tech_close_kb,
    trainer_main_kb,
)
from app.trainer.plan_parse import generate_workout_plan, parse_workout_plan, plan_to_state
from app.trainer.progression import next_load_hint
from app.trainer.reminders import handle_light_home
from app.trainer.reports import compare_last_session, report_vs_avg_four_weeks, week_month_top_moves

trainer_router = Router(name="trainer")


class TrSession(StatesGroup):
    check_cns = State()
    check_joint = State()
    check_psy = State()
    in_workout = State()
    checkout_cns = State()
    checkout_joint = State()
    checkout_ctrl = State()


def _bonus_kcal(session_kcal: int) -> int:
    return min(int(round(session_kcal * 0.7)), 200)


async def exercise_has_history(exercise_id: str) -> bool:
    ws = await get_worksheet("Прогресс_упражнений")
    rows = await get_ws_values(ws)
    for r in rows[1:]:
        if r and str(r[0]).strip() == exercise_id:
            return True
    return False


async def parse_set_gemini(text: str) -> Optional[Dict[str, float]]:
    p = f'Извлеки вес (кг) и повторения. Ответ только JSON: {{"weight":число,"reps":число}}. Строка: {text}'
    raw = await call_gemini(p)
    m = re.search(r"\{[^}]+\}", raw)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
        return {"weight": float(j.get("weight", 0)), "reps": float(j.get("reps", 0))}
    except Exception:
        return None


def _scale_val(data: str) -> int:
    return int(data.rsplit("_", 1)[-1])


def _pick_analog_triplet(pool: List[Dict[str, Any]], exclude: set, current_id: str) -> List[Dict[str, Any]]:
    shuffled = list(pool)
    random.shuffle(shuffled)
    picked: List[Dict[str, Any]] = []
    for c in shuffled:
        cid = c["id"]
        if cid == current_id or cid in exclude:
            continue
        picked.append(c)
        if len(picked) >= 3:
            break
    if len(picked) < 3:
        for c in shuffled:
            if c["id"] == current_id or c in picked:
                continue
            picked.append(c)
            if len(picked) >= 3:
                break
    return picked[:3]


def _replace_session_ex(sess: List[Dict[str, Any]], idx: int, *, exercise_id: str, name: str, template: Dict[str, Any]) -> None:
    sess[idx] = {
        "exercise_id": exercise_id,
        "name": name,
        "sets": [],
        "target_sets": template.get("target_sets", template.get("sets", 3)),
        "reps_target": template.get("reps_target", 10),
        "weight_kg_plan": template.get("weight_kg_plan", template.get("weight_kg")),
    }


@trainer_router.message(Command("start"))
@trainer_router.message(Command("menu"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я твой тренер 💪\n\n"
        "• <b>План</b> — соберём тренировку\n"
        "• <b>Старт</b> — чек-ин и живые подходы\n"
        "• <b>Отчёты / календарь</b> — как растёшь\n"
        "• <b>Проверить Gemini</b> — связь с ИИ\n\n"
        "Готова? Жми «План» или «Старт».",
        reply_markup=trainer_main_kb(),
    )
    await record_user_chat("trainer", message.chat.id)


@trainer_router.callback_query(F.data == "tr_main")
async def cb_main(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Меню:", reply_markup=trainer_main_kb())
    await call.answer()


@trainer_router.callback_query(F.data == "tr_plan_menu")
async def cb_plan_menu(call: types.CallbackQuery):
    await call.message.answer(
        "План: «Составить план ИИ» — тренер сам соберёт тренировку.\n"
        "Или пришли текст / «/plan …» — разберём вручную.",
        reply_markup=plan_menu_kb(),
    )
    await call.answer()


@trainer_router.callback_query(F.data == "tr_gen_plan")
async def cb_gen_plan(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer("Собираю план из твоей базы упражнений…")
    db = await ExerciseDB.load()
    if not db.by_id:
        await call.message.answer("В таблице нет упражнений. Заполни лист «Список упражнений».")
        return
    plan = await generate_workout_plan(db)
    if plan.get("error"):
        await call.message.answer(str(plan["error"]))
        return
    await _save_plan(call.message, plan)


@trainer_router.message(Command("plan"))
async def ingest_plan_cmd(message: types.Message):
    await _ingest_plan_msg(message)


@trainer_router.message(~StateFilter(TrSession), F.text, ~F.text.startswith("/"))
async def ingest_long_plan(message: types.Message):
    if message.text and len(message.text) > 80:
        await _ingest_plan_msg(message)


async def _save_plan(message: types.Message, plan: dict) -> None:
    st = plan_to_state(plan)
    await save_state_pairs({"plan_last_json": json.dumps(st, ensure_ascii=False)})
    await append_row_to_sheet("План_тренировок", [now_local().strftime("%Y-%m-%d"), json.dumps(st, ensure_ascii=False)])
    n = len(st.get("exercises", []))
    names = ", ".join(e["name"] for e in st.get("exercises", [])[:8])
    extra = ("\n<i>" + plan["note"] + "</i>") if plan.get("note") else ""
    lines = "\n".join(f"• {e['name']} — {e['sets']}×{e['reps_target']}" for e in st.get("exercises", []))
    await message.answer(
        f"<b>План готов</b> ({n} упражнений){extra}\n{lines}",
        reply_markup=trainer_main_kb(),
    )


async def _ingest_plan_msg(message: types.Message):
    try:
        await record_user_chat("trainer", message.chat.id)
    except Exception:
        pass
    text = message.text or ""
    if text.startswith("/plan"):
        text = text.replace("/plan", "", 1).strip()
    db = await ExerciseDB.load()
    plan = await parse_workout_plan(text, db)
    if plan.get("error"):
        await message.answer(str(plan["error"]))
        return
    await _save_plan(message, plan)


@trainer_router.callback_query(F.data == "tr_last_plan")
async def cb_last_plan(call: types.CallbackQuery):
    try:
        st = json.loads(SYSTEM_STATE.get("plan_last_json") or "{}")
    except Exception:
        st = {}
    exs = st.get("exercises") or []
    if not exs:
        await call.message.answer("Сохранённого плана нет. Нажми «План тренировки».", reply_markup=trainer_main_kb())
        await call.answer()
        return
    lines = "\n".join(f"• {e.get('name')} — {e.get('sets')}×{e.get('reps_target')}" for e in exs)
    await call.message.answer(f"<b>Последний план</b>\n{lines}", reply_markup=trainer_main_kb())
    await call.answer()


@trainer_router.callback_query(F.data == "tr_rep_d")
async def cb_rep_d(call: types.CallbackQuery):
    await call.message.answer(await compare_last_session())
    await call.answer()


@trainer_router.callback_query(F.data == "tr_rep_w")
async def cb_rep_w(call: types.CallbackQuery):
    await call.message.answer(await week_month_top_moves(7))
    await call.answer()


@trainer_router.callback_query(F.data == "tr_rep_m")
async def cb_rep_m(call: types.CallbackQuery):
    await call.message.answer(await week_month_top_moves(30))
    await call.answer()


@trainer_router.callback_query(F.data == "tr_cal_png")
async def cb_cal(call: types.CallbackQuery):
    m = await current_month_tonnage_map()
    nl = now_local()
    png = await month_png_bytes(nl.year, nl.month, m)
    await call.message.answer_photo(BufferedInputFile(png, filename="cal.png"))
    await call.answer()


@trainer_router.callback_query(F.data == "tr_reset_session")
async def cb_reset(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Сессия сброшена.", reply_markup=trainer_main_kb())
    await call.answer()


@trainer_router.callback_query(F.data == "tr_start_flow")
async def cb_start_flow(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(TrSession.check_cns)
    await call.message.answer(
        "Как самочувствие?\nЦНС / сон — от 1 до 10:",
        reply_markup=scale_1_10("tr_pre_cns", "tr_main"),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_back:"))
async def cb_pre_back(call: types.CallbackQuery, state: FSMContext):
    step = call.data.split(":")[-1]
    if step == "cns":
        await state.set_state(TrSession.check_cns)
        await call.message.answer(
            "Как самочувствие?\nЦНС / сон — от 1 до 10:",
            reply_markup=scale_1_10("tr_pre_cns", "tr_main"),
        )
    elif step == "joint":
        await state.set_state(TrSession.check_joint)
        await call.message.answer("Суставы сегодня — от 1 до 10?", reply_markup=scale_1_10("tr_pre_joint", "tr_pre_back:cns"))
    elif step == "psy":
        await state.set_state(TrSession.check_psy)
        await call.message.answer("Настрой и мотивация — от 1 до 10?", reply_markup=scale_1_10("tr_pre_psy", "tr_pre_back:joint"))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_cns_"))
async def cb_pre_cns(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(pre_cns=_scale_val(call.data))
    await state.set_state(TrSession.check_joint)
    await call.message.answer("Суставы сегодня — от 1 до 10?", reply_markup=scale_1_10("tr_pre_joint", "tr_pre_back:cns"))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_joint_"))
async def cb_pre_joint(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(pre_joint=_scale_val(call.data))
    await state.set_state(TrSession.check_psy)
    await call.message.answer("Настрой и мотивация — от 1 до 10?", reply_markup=scale_1_10("tr_pre_psy", "tr_pre_back:joint"))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_psy_"))
async def cb_pre_psy(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(pre_psy=_scale_val(call.data))
    await call.message.answer(
        "Супер. Сделай короткую разминку и жми «Начать» — зафиксирую старт.",
        reply_markup=pre_begin_kb(),
    )
    await call.answer()


@trainer_router.callback_query(F.data == "tr_begin_session")
async def cb_begin_session(call: types.CallbackQuery, state: FSMContext):
    raw = SYSTEM_STATE.get("plan_last_json")
    if not raw:
        await call.answer("Нет плана. Отправь /plan.", show_alert=True)
        return
    plan = json.loads(raw)
    exercises = plan.get("exercises", [])
    if not exercises:
        await call.answer("План пуст.", show_alert=True)
        return
    started = now_local().isoformat()
    sess: List[Dict[str, Any]] = []
    for ex in exercises:
        sess.append(
            {
                "exercise_id": ex["exercise_id"],
                "name": ex["name"],
                "sets": [],
                "target_sets": ex.get("sets", 3),
                "reps_target": ex.get("reps_target", 10),
                "weight_kg_plan": ex.get("weight_kg"),
            }
        )
    await state.update_data(started_at=started, plan=plan, session=sess, cur_idx=0, await_baseline=False)
    await state.set_state(TrSession.in_workout)
    await _show_exercise(call.message, state)
    await call.answer()


async def _show_exercise(message: types.Message, state: FSMContext):
    data = await state.get_data()
    idx = int(data.get("cur_idx", 0))
    sess: List[Dict[str, Any]] = data.get("session", [])
    if idx >= len(sess):
        await _goto_checkout(message, state)
        return
    ex = sess[idx]
    text = (
        f"Упражнение {idx+1}/{len(sess)}\n"
        f"<b>{ex['name']}</b>\n"
        f"Подход {len(ex['sets'])+1}. Запиши вес×повторы, когда будешь готова."
    )
    await message.answer(text, reply_markup=session_exercise_kb(idx))


@trainer_router.callback_query(F.data.startswith("tr_set_done:"))
async def cb_set_done(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    sess = data["session"]
    ex = sess[idx]
    hist = await exercise_has_history(ex["exercise_id"])
    need_bl = (not hist) and len(ex["sets"]) == 0
    await state.update_data(cur_idx=idx)
    tip = "Первый раз это упражнение — напиши свои вес и повторы (например 40×10)." if need_bl else "Напиши вес и повторы: например <code>40×10</code> или голосом."
    await call.message.answer(tip)
    await call.answer()


@trainer_router.message(TrSession.in_workout, F.text | F.voice | F.photo)
async def msg_set_input(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data.get("await_baseline") and message.photo:
        await message.answer("Для веса пришли число или голос/текст.")
        return
    idx = int(data.get("cur_idx", 0))
    sess: List[Dict[str, Any]] = data["session"]
    ex = sess[idx]
    txt = message.text or ""
    if message.voice:
        t = await transcribe_voice_message(bot, message)
        if t:
            txt = t
    if message.photo:
        b64 = await download_file_as_base64(bot, message.photo[-1].file_id)
        mime = "image/jpeg"
        txt = await call_gemini(
            "Считай вес (кг) и повторы с фото. Ответ одной строкой: числа.",
            media_b64=b64,
            mime_type=mime,
        )
    parsed = await parse_set_gemini(txt)
    if not parsed:
        await message.answer("Не расслышала. Пример: <code>60×8</code> или «60 кг 8 раз».")
        return
    ex["sets"].append({"weight": parsed["weight"], "reps": parsed["reps"]})
    await state.update_data(session=sess, await_baseline=False)
    si = len(ex["sets"]) - 1
    await message.answer(
        f"Записала: {parsed['weight']:g} кг × {parsed['reps']:g}.\nНасколько тяжело было (1–10)?",
        reply_markup=scale_set_difficulty_kb(idx, si),
    )


@trainer_router.callback_query(F.data.startswith("trsd:"))
async def cb_set_diff(call: types.CallbackQuery, state: FSMContext):
    _, idx_s, si_s, d_s = call.data.split(":")
    idx, si, diff = int(idx_s), int(si_s), int(d_s)
    data = await state.get_data()
    sess = data["session"]
    ex = sess[idx]
    if si < len(ex["sets"]):
        ex["sets"][si]["difficulty"] = diff
    await state.update_data(session=sess)
    await call.message.answer("Сколько отдыхаем?", reply_markup=rest_kb("r", ex_idx=idx))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_rest:"))
async def cb_rest(call: types.CallbackQuery):
    parts = call.data.split(":")
    sec = int(parts[2]) if len(parts) > 2 else 90
    chat = call.message.chat.id
    try:
        await call.message.edit_text(f"Отдыхаем {sec} сек… ⏳")
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await call.answer(f"{sec} сек")

    async def ping():
        await asyncio.sleep(sec)
        try:
            await call.bot.send_message(chat, "Приступай к следующему подходу 💪")
        except Exception:
            pass

    asyncio.create_task(ping())


@trainer_router.callback_query(F.data.startswith("tr_skip_set:"))
async def cb_skip(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    sess = data["session"]
    ex = sess[idx]
    ex["sets"].append({"weight": 0, "reps": 0, "skipped": True})
    await state.update_data(session=sess)
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_finish_ex:"))
async def cb_finish_ex(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    await state.update_data(finish_ex_idx=idx, cur_idx=idx)
    await call.message.answer(
        "Оценка тяжести всего упражнения 1–10?",
        reply_markup=scale_1_10("tr_ex_diff", f"tr_back_ex:{idx}"),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_ex_diff_"))
async def cb_ex_diff(call: types.CallbackQuery, state: FSMContext):
    d = _scale_val(call.data)
    data = await state.get_data()
    idx = int(data.get("finish_ex_idx", data.get("cur_idx", 0)))
    sess = data["session"]
    sess[idx]["exercise_difficulty"] = d
    await state.update_data(session=sess, cur_idx=idx + 1, finish_ex_idx=None)
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_analog:"))
async def cb_analog(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    db = await ExerciseDB.load()
    data = await state.get_data()
    ex = data["session"][idx]
    base = db.by_id.get(ex["exercise_id"])
    if not base:
        await call.answer("Нет в базе", show_alert=True)
        return
    pool = db.analogues(base, 40)
    if len(pool) < 1:
        await call.answer("В базе мало похожих — попробуй «Другое упражнение»", show_alert=True)
        return
    excl_map: Dict[str, List[str]] = dict(data.get("analog_exclude") or {})
    exclude = set(excl_map.get(str(idx), []))
    exclude.add(ex["exercise_id"])
    picked = _pick_analog_triplet(pool, exclude, ex["exercise_id"])
    if not picked:
        await call.answer("Больше вариантов нет — сброшу список", show_alert=True)
        excl_map[str(idx)] = []
        exclude = {ex["exercise_id"]}
        picked = _pick_analog_triplet(pool, exclude, ex["exercise_id"])
    if not picked:
        await call.answer("Не нашла аналогов", show_alert=True)
        return
    exclude.update(c["id"] for c in picked)
    excl_map[str(idx)] = list(exclude)
    await state.update_data(analog_exclude=excl_map, cur_idx=idx)
    text = (
        f"<b>Аналоги к «{ex['name']}»</b>\n"
        "Жми упражнение — заменю его в тренировке.\n"
        "«Ещё ×3» — другие варианты."
    )
    kb = analogues_pick_kb(idx, picked)
    answered = False
    if call.message.text and "Аналоги" in (call.message.text or ""):
        try:
            await call.message.edit_text(text, reply_markup=kb)
            answered = True
        except Exception:
            pass
    if not answered:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_anlg:"))
async def cb_pick_analog(call: types.CallbackQuery, state: FSMContext):
    _, idx_s, eid = call.data.split(":", 2)
    idx = int(idx_s)
    db = await ExerciseDB.load()
    picked_ex = db.by_id.get(eid)
    if not picked_ex:
        await call.answer("Упражнение не найдено", show_alert=True)
        return
    data = await state.get_data()
    sess: List[Dict[str, Any]] = data["session"]
    _replace_session_ex(sess, idx, exercise_id=eid, name=picked_ex["name"], template=sess[idx])
    excl_map: Dict[str, List[str]] = dict(data.get("analog_exclude") or {})
    excl_map.pop(str(idx), None)
    await state.update_data(session=sess, cur_idx=idx, analog_exclude=excl_map)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(f"Ок, теперь: <b>{picked_ex['name']}</b> 💪")
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_back_ex:"))
async def cb_back_ex(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    await state.update_data(cur_idx=idx, finish_ex_idx=None)
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_tech:"))
async def cb_tech(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    db = await ExerciseDB.load()
    data = await state.get_data()
    ex = data["session"][idx]
    base = db.by_id.get(ex["exercise_id"])
    tips = base.get("tips", []) if base else []
    text = (base.get("technique", "") + "\n" + "\n".join(f"• {t}" for t in tips[:4])).strip()
    tm = await call.message.answer(text or "Нет советов.")
    await tm.edit_reply_markup(reply_markup=tech_close_kb(tm.message_id, idx))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_tech_close:"))
async def cb_tech_close(call: types.CallbackQuery):
    await call.message.delete()
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_swap:"))
async def cb_swap(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    plan = data.get("plan", {}).get("exercises", [])
    await state.update_data(cur_idx=idx)
    await call.message.answer("Другое из плана:", reply_markup=swap_plan_kb(idx, plan))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pick_swap:"))
async def cb_pick_swap(call: types.CallbackQuery, state: FSMContext):
    eid = call.data.split(":", 1)[1]
    data = await state.get_data()
    plan = data["plan"]["exercises"]
    ex = next(x for x in plan if x["exercise_id"] == eid)
    sess = data["session"]
    idx = data["cur_idx"]
    sess[idx] = {
        "exercise_id": ex["exercise_id"],
        "name": ex["name"],
        "sets": [],
        "target_sets": ex.get("sets", 3),
        "reps_target": ex.get("reps_target", 10),
        "weight_kg_plan": ex.get("weight_kg"),
    }
    await state.update_data(session=sess)
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_abort")
async def cb_abort(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Сессия прервана.", reply_markup=trainer_main_kb())
    await call.answer()


async def _goto_checkout(message: types.Message, state: FSMContext):
    await state.set_state(TrSession.checkout_cns)
    data = await state.get_data()
    sess = data.get("session") or []
    back = f"tr_back_ex:{max(0, len(sess) - 1)}" if sess else None
    await message.answer(
        "Финиш почти! Как ЦНС после тренировки (1–10)?",
        reply_markup=scale_1_10("tr_post_cns", back),
    )


@trainer_router.callback_query(F.data.startswith("tr_post_back:"))
async def cb_post_back(call: types.CallbackQuery, state: FSMContext):
    step = call.data.split(":")[-1]
    if step == "cns":
        data = await state.get_data()
        sess = data.get("session") or []
        await state.set_state(TrSession.in_workout)
        await state.update_data(cur_idx=max(0, len(sess) - 1))
        await _show_exercise(call.message, state)
    elif step == "joint":
        await state.set_state(TrSession.checkout_cns)
        data = await state.get_data()
        sess = data.get("session") or []
        back = f"tr_back_ex:{max(0, len(sess) - 1)}" if sess else None
        await call.message.answer(
            "Финиш почти! Как ЦНС после тренировки (1–10)?",
            reply_markup=scale_1_10("tr_post_cns", back),
        )
    elif step == "ctrl":
        await state.set_state(TrSession.checkout_joint)
        await call.message.answer("Суставы после тренировки (1–10)?", reply_markup=scale_1_10("tr_post_joint", "tr_post_back:cns"))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_post_cns_"))
async def cb_post_cns(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(post_cns=_scale_val(call.data))
    await state.set_state(TrSession.checkout_joint)
    await call.message.answer("Суставы после тренировки (1–10)?", reply_markup=scale_1_10("tr_post_joint", "tr_post_back:cns"))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_post_joint_"))
async def cb_post_joint(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(post_joint=_scale_val(call.data))
    await state.set_state(TrSession.checkout_ctrl)
    await call.message.answer(
        "Насколько держала контроль техники ~70% (1–10)?",
        reply_markup=scale_1_10("tr_post_ctrl", "tr_post_back:joint"),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_post_ctrl_"))
async def cb_post_ctrl(call: types.CallbackQuery, state: FSMContext, bot_trainer: Bot, bot_nutrition: Bot):
    data = await state.get_data()
    post_j = int(data.get("post_joint", 5))
    pain = post_j <= 3
    await state.update_data(post_ctrl=_scale_val(call.data), pain=pain)
    sess = data.get("session", [])
    fin = []
    tons_total = 0.0
    for ex in sess:
        sets = [s for s in ex["sets"] if not s.get("skipped")]
        tnx = sum(s["weight"] * s["reps"] for s in sets)
        diffs = [s.get("difficulty") for s in sets if s.get("difficulty") is not None]
        avg_d = sum(diffs) / len(diffs) if diffs else float(ex.get("exercise_difficulty", 5) or 5)
        avg_w = sum(s["weight"] for s in sets) / len(sets) if sets else 0
        avg_r = sum(s["reps"] for s in sets) / len(sets) if sets else 0
        ex_out = {
            "exercise_id": ex["exercise_id"],
            "name": ex["name"],
            "sets": ex["sets"],
            "tonnage": tnx,
            "avg_weight": avg_w,
            "avg_reps": avg_r,
            "avg_difficulty": avg_d,
        }
        fin.append(ex_out)
        tons_total += tnx
        hint = next_load_hint(avg_d, pain, avg_w > 0, avg_w, int(avg_r) if avg_r else None)[1]
        ex_out["progress_note"] = hint.get("why", "")
        await append_row_to_sheet(
            "Прогресс_упражнений",
            [
                ex["exercise_id"],
                ex["name"],
                now_local().strftime("%Y-%m-%d"),
                str(len(sets)),
                f"{tnx:.1f}",
                f"{avg_w:.2f}",
                f"{avg_r:.1f}",
                f"{avg_d:.1f}",
                json.dumps(sets, ensure_ascii=False),
                "",
            ],
        )
    started = data.get("started_at", "")
    end = now_local().isoformat()
    try:
        t0 = datetime.fromisoformat(started)
        dur = (now_local().replace(tzinfo=t0.tzinfo) - t0).total_seconds() / 60.0
    except Exception:
        dur = 0.0
    settings = await get_settings_map()
    try:
        bw = float(str(settings.get("weight_kg", "70")).replace(",", "."))
    except Exception:
        bw = 70.0
    sets_count = sum(len([s for s in ex["sets"] if not s.get("skipped")]) for ex in sess)
    skcal = estimate_session_kcal(bw, dur, sets_count, pain)
    bonus = _bonus_kcal(skcal)
    summary = await call_gemini(
        f"Дай ровно 3 буллета (•) краткого итога тренировки на русском. Данные: {json.dumps(fin, ensure_ascii=False)[:2000]}"
    )
    row = [
        str(uuid.uuid4())[:12],
        now_local().strftime("%Y-%m-%d %H:%M"),
        started,
        end,
        f"{dur:.1f}",
        str(data.get("pre_cns", "")),
        str(data.get("pre_joint", "")),
        str(data.get("pre_psy", "")),
        str(data.get("post_cns", "")),
        str(data.get("post_joint", "")),
        str(data.get("post_ctrl", "")),
        "1" if pain else "0",
        str(sets_count),
        f"тренировка {now_local().strftime('%Y-%m-%d')}",
        f"{tons_total:.1f}",
        str(skcal),
        str(bonus),
        json.dumps({"exercises": fin}, ensure_ascii=False),
    ]
    await append_row_to_sheet("Сессии", row)
    await save_state_pairs(
        {
            "last_session_date": now_local().strftime("%Y-%m-%d"),
            "pending_workout_bonus_kcal": str(bonus),
            "pending_workout_bonus_date": now_local().strftime("%Y-%m-%d"),
        }
    )
    await send_cross_notification(
        "trainer",
        f"Бонус от тренировки: +{bonus} ккал на сегодня (≈{BONUS_CARB_FRAC*100:.0f}% углеводы, остальное жиры/белок).",
        bot_trainer,
        bot_nutrition,
    )
    cmp1 = await report_vs_avg_four_weeks({"exercises": fin})
    await call.message.answer(f"{summary}\n\n{cmp1}")
    await state.clear()
    await call.answer()


@trainer_router.callback_query(F.data == "tr_rm_plan")
async def cb_rm_plan(call: types.CallbackQuery):
    await call.message.answer("Отправь текст плана в чат или /plan …")
    await call.answer()


@trainer_router.callback_query(F.data == "tr_rm_light")
async def cb_rm_light(call: types.CallbackQuery):
    await handle_light_home(call.message.bot, call.message.chat.id)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_rm_no")
async def cb_rm_no(call: types.CallbackQuery):
    await call.answer()
