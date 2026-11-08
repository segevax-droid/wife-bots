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
from app.sheets import append_row_to_sheet, get_settings_map
from app.state import SYSTEM_STATE, now_local, save_state_pairs
from app.trainer.favorites import (
    favorite_ids_list,
    is_favorite,
    remove_favorite,
    toggle_favorite,
)
from app.trainer.program import (
    HISTORY_KEY,
    append_history_entry,
    advance_cycle_cursor,
)
from app.trainer.calendar_png import current_month_tonnage_map, month_png_bytes
from app.trainer.energy import estimate_session_kcal
from app.trainer.exercise_db import ExerciseDB
from app.trainer.kb import (
    analogues_pick_kb,
    favorites_menu_kb,
    pick_next_kb,
    plan_menu_kb,
    post_ex_fav_kb,
    pre_begin_kb,
    rest_kb,
    scale_1_10,
    scale_set_difficulty_kb,
    scheme_pick_kb,
    session_exercise_kb,
    swap_pending_kb,
    swap_plan_kb,
    tech_close_kb,
    trainer_main_kb,
    warmup_kb,
)
from app.trainer.plan_parse import generate_workout_plan, parse_workout_plan, plan_to_state
from app.trainer.progression import next_load_hint
from app.trainer.reminders import handle_light_home
from app.trainer.reports import compare_last_session, report_vs_avg_four_weeks, week_month_top_moves
from app.trainer.ux import (
    checkin_nudge,
    checkin_question,
    exercise_card,
    format_favorites_list,
    format_picker_message,
    format_plan_message,
    format_technique_message,
    format_warmup_card,
    parse_error_friendly,
    pre_begin_message,
    praise_after_set,
    rest_ping_message,
    session_kickoff,
    workout_finish_prompt,
)

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


def parse_set_local(text: str) -> Optional[Dict[str, float]]:
    s = re.sub(r"(\d),(\d)", r"\1.\2", (text or "").strip())
    m = re.search(
        r"(\d{1,3}(?:\.\d+)?)\s*(?:кг|kg)?\s*[x×*хX]\s*(\d{1,3}(?:\.\d+)?)(?:\s*[,;/]\s*(\d{1,2}))?",
        s,
    )
    if m:
        out: Dict[str, float] = {"weight": float(m.group(1)), "reps": float(m.group(2))}
        if m.group(3):
            d = int(m.group(3))
            if 1 <= d <= 10:
                out["difficulty"] = float(d)
        return out
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:кг|kg)\s+(\d{1,3})", s, re.I)
    if m:
        return {"weight": float(m.group(1)), "reps": float(m.group(2))}
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s+(?:на|по)\s+(\d{1,3})", s, re.I)
    if m:
        return {"weight": float(m.group(1)), "reps": float(m.group(2))}
    return None


async def parse_set_gemini(text: str) -> Optional[Dict[str, float]]:
    local = parse_set_local(text)
    if local:
        return local
    if not re.search(r"\d", text or ""):
        return None
    p = f'Извлеки вес (кг) и повторения. Ответ только JSON: {{"weight":число,"reps":число}}. Строка: {text}'
    raw = await call_gemini(p)
    m = re.search(r"\{[^}]+\}", raw)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
        w, r = float(j.get("weight", 0)), float(j.get("reps", 0))
        if w <= 0 or r <= 0:
            return None
        return {"weight": w, "reps": r}
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
        "🏋️ <b>Старт</b> — чек-ин, подбадривания и подходы в живом темпе\n"
        "📋 <b>План</b> — сплит (full / верх-низ), разминка, выбор упражнений кнопками\n"
        "📊 <b>Отчёты и календарь</b> — прогресс без скуки\n"
        "🔧 <b>Техника</b> на каждом упражнении — кнопкой или голосом <code>40×10</code>\n\n"
        "С чего начнём?",
        reply_markup=trainer_main_kb(),
    )
    await record_user_chat("trainer", message.chat.id)


@trainer_router.callback_query(F.data == "tr_main")
async def cb_main(call: types.CallbackQuery, state: FSMContext):
    cur = await state.get_state()
    if cur and str(cur).startswith("TrSession"):
        await call.message.answer(
            "Тренировка ещё идёт. Сначала прерви её или вернись к упражнению.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="К упражнению", callback_data="tr_resume")],
                    [InlineKeyboardButton(text="Прервать тренировку", callback_data="tr_abort")],
                ]
            ),
        )
        await call.answer()
        return
    await state.clear()
    await call.message.answer("Меню:", reply_markup=trainer_main_kb())
    await call.answer()


@trainer_router.callback_query(F.data == "tr_resume")
async def cb_resume(call: types.CallbackQuery, state: FSMContext):
    cur = await state.get_state()
    if cur and "checkout" in str(cur):
        await _goto_checkout(call.message, state)
        await call.answer()
        return
    await state.set_state(TrSession.in_workout)
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_plan_menu")
async def cb_plan_menu(call: types.CallbackQuery):
    await call.message.answer(
        "📋 <b>План тренировки</b>\n\n"
        "• <b>Составить план ИИ</b> — название, разминка, порядок и подходы\n"
        "• Пришли свой текст или <code>/plan …</code> — разложу по упражнениям\n"
        "• <b>Последний план</b> — посмотреть, что уже готово",
        reply_markup=plan_menu_kb(),
    )
    await call.answer()


@trainer_router.callback_query(F.data == "tr_gen_plan")
async def cb_gen_plan(call: types.CallbackQuery):
    await call.message.answer(
        "🧠 <b>Схема на неделю</b>\nВыбери сплит — соберу день с учётом базы и любимых ★",
        reply_markup=scheme_pick_kb(),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_scheme:"))
async def cb_scheme(call: types.CallbackQuery):
    scheme = call.data.split(":")[-1]
    await call.answer()
    await call.message.answer("Собираю план из базы — порядок, разминка, ротация…")
    db = await ExerciseDB.load()
    if not db.by_id:
        await call.message.answer("В таблице нет упражнений. Заполни лист «Список упражнений».")
        return
    plan = await generate_workout_plan(db, scheme)
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
    cycle = plan.pop("cycle", None)
    st = plan_to_state(plan)
    pairs: Dict[str, str] = {"plan_last_json": json.dumps(st, ensure_ascii=False)}
    if cycle:
        pairs["plan_cycle_json"] = json.dumps(cycle, ensure_ascii=False)
    await save_state_pairs(pairs)
    await append_row_to_sheet("План_тренировок", [now_local().strftime("%Y-%m-%d"), json.dumps(st, ensure_ascii=False)])
    db = await ExerciseDB.load()
    await message.answer(format_plan_message(plan if plan.get("title") else {**plan, **st}, db), reply_markup=trainer_main_kb())


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
    db = await ExerciseDB.load()
    await call.message.answer(
        format_plan_message({**st, "exercises": exs}, db),
        reply_markup=trainer_main_kb(),
    )
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
        checkin_question("cns"),
        reply_markup=scale_1_10("tr_pre_cns", "tr_main"),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_back:"))
async def cb_pre_back(call: types.CallbackQuery, state: FSMContext):
    step = call.data.split(":")[-1]
    if step == "cns":
        await state.set_state(TrSession.check_cns)
        await call.message.answer(
            checkin_question("cns"),
            reply_markup=scale_1_10("tr_pre_cns", "tr_main"),
        )
    elif step == "joint":
        await state.set_state(TrSession.check_joint)
        await call.message.answer(checkin_question("joint"), reply_markup=scale_1_10("tr_pre_joint", "tr_pre_back:cns"))
    elif step == "psy":
        await state.set_state(TrSession.check_psy)
        await call.message.answer(checkin_question("psy"), reply_markup=scale_1_10("tr_pre_psy", "tr_pre_back:joint"))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_cns_"))
async def cb_pre_cns(call: types.CallbackQuery, state: FSMContext):
    val = _scale_val(call.data)
    await state.update_data(pre_cns=val)
    nudge = checkin_nudge("cns", val)
    await state.set_state(TrSession.check_joint)
    await call.message.answer(
        (nudge + "\n\n" if nudge else "") + checkin_question("joint"),
        reply_markup=scale_1_10("tr_pre_joint", "tr_pre_back:cns"),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_joint_"))
async def cb_pre_joint(call: types.CallbackQuery, state: FSMContext):
    val = _scale_val(call.data)
    await state.update_data(pre_joint=val)
    nudge = checkin_nudge("joint", val)
    await state.set_state(TrSession.check_psy)
    await call.message.answer(
        (nudge + "\n\n" if nudge else "") + checkin_question("psy"),
        reply_markup=scale_1_10("tr_pre_psy", "tr_pre_back:joint"),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pre_psy_"))
async def cb_pre_psy(call: types.CallbackQuery, state: FSMContext):
    val = _scale_val(call.data)
    await state.update_data(pre_psy=val)
    data = await state.get_data()
    nudge = checkin_nudge("psy", val)
    body = pre_begin_message(int(data.get("pre_cns", 5)), int(data.get("pre_joint", 5)), val)
    if nudge:
        body = nudge + "\n\n" + body
    await call.message.answer(body, reply_markup=pre_begin_kb())
    await call.answer()


async def _show_warmup(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    plan = data.get("plan") or {}
    await message.answer(format_warmup_card(plan), reply_markup=warmup_kb())


async def _show_picker(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    sess: List[Dict[str, Any]] = data.get("session") or []
    pending: List[int] = list(data.get("pending_indices") or [])
    done = int(data.get("completed_count") or 0)
    if not pending:
        await _goto_checkout(message, state)
        return
    await state.update_data(cur_idx=-1)
    await message.answer(
        format_picker_message(sess, pending, done),
        reply_markup=pick_next_kb(pending, sess),
    )


async def _kb_for_exercise(state: FSMContext, ex_idx: int) -> InlineKeyboardMarkup:
    data = await state.get_data()
    sess = data.get("session") or []
    ex = sess[ex_idx] if ex_idx < len(sess) else {}
    empty = not (ex.get("sets") or [])
    fav = is_favorite(str(ex.get("exercise_id", "")))
    return session_exercise_kb(ex_idx, is_fav=fav, sets_empty=empty)


@trainer_router.callback_query(F.data == "tr_begin_session")
async def cb_begin_session(call: types.CallbackQuery, state: FSMContext):
    raw = SYSTEM_STATE.get("plan_last_json")
    if not raw:
        await call.answer()
        await call.message.answer(
            "Плана ещё нет — сначала соберём тренировку.",
            reply_markup=plan_menu_kb(),
        )
        return
    plan = json.loads(raw)
    exercises = plan.get("exercises", [])
    if not exercises:
        await call.answer()
        await call.message.answer("План пуст. Составим новый?", reply_markup=plan_menu_kb())
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
    pending = list(range(len(sess)))
    await state.update_data(
        started_at=started,
        plan=plan,
        session=sess,
        cur_idx=-1,
        pending_indices=pending,
        warmup_done=False,
        completed_count=0,
        await_baseline=False,
    )
    await state.set_state(TrSession.in_workout)
    await call.message.answer(session_kickoff(len(sess), plan))
    await _show_warmup(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_warmup_done")
async def cb_warmup_done(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(warmup_done=True)
    await _show_picker(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_warmup_skip")
async def cb_warmup_skip(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if int(data.get("pre_joint", 8)) <= 4:
        await call.answer("Суставы просили разминку — хотя бы 2–3 минуты 🫧", show_alert=True)
        return
    await state.update_data(warmup_done=True)
    await _show_picker(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_nx:"))
async def cb_pick_next(call: types.CallbackQuery, state: FSMContext):
    q = int(call.data.split(":")[-1])
    data = await state.get_data()
    pending = list(data.get("pending_indices") or [])
    if q < 0 or q >= len(pending):
        await call.answer("Список обновился — открой меню снова", show_alert=True)
        return
    si = pending.pop(q)
    await state.update_data(pending_indices=pending, cur_idx=si)
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_nx_skip")
async def cb_nx_skip(call: types.CallbackQuery, state: FSMContext):
    pending = list((await state.get_data()).get("pending_indices") or [])
    if len(pending) > 1:
        pending.append(pending.pop(0))
        await state.update_data(pending_indices=pending)
    data = await state.get_data()
    sess = data.get("session") or []
    done = int(data.get("completed_count") or 0)
    try:
        await call.message.edit_text(
            format_picker_message(sess, pending, done),
            reply_markup=pick_next_kb(pending, sess),
        )
    except Exception:
        await _show_picker(call.message, state)
    await call.answer("Следующее в очереди")


@trainer_router.callback_query(F.data == "tr_nx_anlg")
async def cb_nx_anlg(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending = data.get("pending_indices") or []
    if not pending:
        await call.answer("Очередь пуста", show_alert=True)
        return
    idx = int(pending[0])
    await state.update_data(cur_idx=idx)
    await _offer_analogs(call, state, idx)


@trainer_router.callback_query(F.data == "tr_nx_end")
async def cb_nx_end(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await _goto_checkout(call.message, state)


async def _show_exercise(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("warmup_done"):
        await _show_warmup(message, state)
        return
    idx = int(data.get("cur_idx", -1))
    if idx < 0:
        await _show_picker(message, state)
        return
    sess: List[Dict[str, Any]] = data.get("session", [])
    if idx >= len(sess):
        await _goto_checkout(message, state)
        return
    pending = data.get("pending_indices") or []
    done = int(data.get("completed_count") or 0)
    total = done + len(pending) + 1
    ex = sess[idx]
    db = await ExerciseDB.load()
    base = db.by_id.get(ex.get("exercise_id", ""))
    disp_idx = done
    text = exercise_card(ex, disp_idx, total, base)
    kb = await _kb_for_exercise(state, idx)
    await message.answer(text, reply_markup=kb)


@trainer_router.message(TrSession.in_workout, F.text | F.voice | F.photo)
async def msg_set_input(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    idx = int(data.get("cur_idx", -1))
    if idx < 0:
        return
    sess: List[Dict[str, Any]] = data["session"]
    if idx >= len(sess):
        return
    ex = sess[idx]
    txt = message.text or ""
    if message.voice:
        t = await transcribe_voice_message(bot, message)
        if t:
            txt = t
        else:
            await message.answer("Не расслышала голос. Напиши, например <code>40×10</code>.")
            return
    if message.photo:
        b64 = await download_file_as_base64(bot, message.photo[-1].file_id)
        txt = await call_gemini(
            "Считай вес (кг) и повторы с фото. Ответ одной строкой: числа.",
            media_b64=b64,
            mime_type="image/jpeg",
        )
    parsed = await parse_set_gemini(txt)
    if not parsed:
        await message.answer(parse_error_friendly())
        return
    row: Dict[str, Any] = {"weight": parsed["weight"], "reps": parsed["reps"]}
    if parsed.get("difficulty"):
        row["difficulty"] = int(parsed["difficulty"])
    ex["sets"].append(row)
    await state.update_data(session=sess, await_baseline=False)
    si = len(ex["sets"]) - 1
    tgt = int(ex.get("target_sets") or 3)
    if row.get("difficulty"):
        await message.answer(
            praise_after_set(parsed["weight"], parsed["reps"], int(row["difficulty"]), si, tgt)
            + "\n\n⏱ Сколько отдыхаем?",
            reply_markup=rest_kb("r", ex_idx=idx),
        )
        return
    await message.answer(
        praise_after_set(parsed["weight"], parsed["reps"], None, si, tgt)
        + "\n\nНасколько тяжело было <b>(1–10)</b>?",
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
    await call.message.answer("⏱ Сколько отдыхаем?", reply_markup=rest_kb("r", ex_idx=idx))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_rest:"))
async def cb_rest(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    sec = int(parts[2]) if len(parts) > 2 else 90
    chat = call.message.chat.id
    data = await state.get_data()
    idx = int(data.get("cur_idx", 0))
    try:
        await call.message.delete()
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await call.answer(f"Отдых {sec} сек")

    async def ping():
        await asyncio.sleep(sec)
        try:
            kb = await _kb_for_exercise(state, idx)
            await call.bot.send_message(chat, rest_ping_message(), reply_markup=kb)
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
        "💬 Как в целом прошло упражнение? Оценка тяжести <b>1–10</b>:",
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
    completed = int(data.get("completed_count") or 0) + 1
    await state.update_data(
        session=sess,
        cur_idx=-1,
        finish_ex_idx=None,
        completed_count=completed,
    )
    eid = str(sess[idx].get("exercise_id") or "")
    if 5 <= d <= 8 and eid and not is_favorite(eid):
        await call.message.answer(
            f"«{sess[idx]['name']}» зашло нормально — в любимые?",
            reply_markup=post_ex_fav_kb(idx),
        )
        await call.answer()
        return
    await _show_picker(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_fav_post:"))
async def cb_fav_post(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    idx = int(parts[1])
    yes = parts[2] == "1"
    data = await state.get_data()
    if yes and idx < len(data.get("session") or []):
        eid = data["session"][idx].get("exercise_id")
        if eid:
            _, msg = await toggle_favorite(str(eid))
            await call.message.answer(msg)
    await _show_picker(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_fav_toggle:"))
async def cb_fav_toggle(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    sess = data.get("session") or []
    if idx >= len(sess):
        await call.answer()
        return
    eid = sess[idx].get("exercise_id")
    if not eid:
        await call.answer("Нет id", show_alert=True)
        return
    _, msg = await toggle_favorite(str(eid))
    await call.answer(msg[:180], show_alert=len(msg) > 60)
    await _show_exercise(call.message, state)


@trainer_router.callback_query(F.data == "tr_fav_menu")
async def cb_fav_menu(call: types.CallbackQuery):
    db = await ExerciseDB.load()
    items = []
    for eid in favorite_ids_list():
        ex = db.by_id.get(eid)
        if ex:
            items.append(ex)
    text = format_favorites_list([(x["id"], x["name"]) for x in items])
    await call.message.answer(text, reply_markup=favorites_menu_kb(items))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_fav_rm:"))
async def cb_fav_rm(call: types.CallbackQuery):
    eid = call.data.split(":", 1)[1]
    await remove_favorite(eid)
    await cb_fav_menu(call)


async def _offer_analogs(call: types.CallbackQuery, state: FSMContext, idx: int) -> None:
    db = await ExerciseDB.load()
    data = await state.get_data()
    ex = data["session"][idx]
    base = db.by_id.get(ex["exercise_id"])
    if not base:
        await call.answer("Нет в базе", show_alert=True)
        return
    pool = db.analogues(base, 40)
    if len(pool) < 1:
        await call.answer("В базе мало похожих — выбери другое из очереди", show_alert=True)
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
    await state.update_data(analog_exclude=excl_map, analog_picks=[c["id"] for c in picked], cur_idx=idx)
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


@trainer_router.callback_query(F.data.startswith("tr_analog:"))
async def cb_analog(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    await _offer_analogs(call, state, idx)


@trainer_router.callback_query(F.data.startswith("tr_anlg:"))
async def cb_pick_analog(call: types.CallbackQuery, state: FSMContext):
    _, idx_s, slot_s = call.data.split(":", 2)
    idx, slot = int(idx_s), int(slot_s)
    data = await state.get_data()
    picks = list(data.get("analog_picks") or [])
    if slot < 0 or slot >= len(picks):
        await call.answer("Список устарел — нажми «Аналоги ×3» ещё раз", show_alert=True)
        return
    eid = picks[slot]
    db = await ExerciseDB.load()
    picked_ex = db.by_id.get(eid)
    if not picked_ex:
        await call.answer("Упражнение не найдено", show_alert=True)
        return
    sess: List[Dict[str, Any]] = data["session"]
    _replace_session_ex(sess, idx, exercise_id=eid, name=picked_ex["name"], template=sess[idx])
    excl_map: Dict[str, List[str]] = dict(data.get("analog_exclude") or {})
    excl_map.pop(str(idx), None)
    pending = list(data.get("pending_indices") or [])
    if idx in pending:
        pending.remove(idx)
    await state.update_data(session=sess, cur_idx=idx, analog_exclude=excl_map, pending_indices=pending)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(f"🔄 Заменила на: <b>{picked_ex['name']}</b> — поехали!")
    await _show_exercise(call.message, state)
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_back_ex:"))
async def cb_back_ex(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[-1])
    await state.set_state(TrSession.in_workout)
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
    text = format_technique_message(base) if base else "Пока нет описания в базе."
    tm = await call.message.answer(text)
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
    sess = data.get("session") or []
    pending = data.get("pending_indices") or []
    ex = sess[idx] if idx < len(sess) else {}
    if ex.get("sets"):
        await call.answer("Уже есть подходы — только аналоги", show_alert=True)
        return
    await state.update_data(cur_idx=idx)
    await call.message.answer(
        "Другое из очереди:",
        reply_markup=swap_pending_kb(idx, pending, sess),
    )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_pick_swap_idx:"))
async def cb_pick_swap_idx(call: types.CallbackQuery, state: FSMContext):
    si = int(call.data.split(":")[-1])
    data = await state.get_data()
    cur = int(data.get("cur_idx", -1))
    sess: List[Dict[str, Any]] = data["session"]
    pending = list(data.get("pending_indices") or [])
    if cur < 0 or cur >= len(sess) or si >= len(sess):
        await call.answer()
        return
    sess[cur], sess[si] = sess[si], sess[cur]
    pending = [si if x == cur else cur if x == si else x for x in pending]
    await state.update_data(session=sess, pending_indices=pending)
    await _show_exercise(call.message, state)
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
    await call.message.answer(
        "Сессия на паузе — ничего страшного 🤍\nКогда будешь готова: план → старт.",
        reply_markup=trainer_main_kb(),
    )
    await call.answer()


async def _goto_checkout(message: types.Message, state: FSMContext):
    await state.set_state(TrSession.checkout_cns)
    data = await state.get_data()
    sess = data.get("session") or []
    back = f"tr_back_ex:{max(0, len(sess) - 1)}" if sess else None
    await message.answer(
        checkin_question("post_cns"),
        reply_markup=scale_1_10("tr_post_cns", back),
    )


@trainer_router.callback_query(F.data.startswith("tr_post_back:"))
async def cb_post_back(call: types.CallbackQuery, state: FSMContext):
    step = call.data.split(":")[-1]
    if step == "joint":
        await state.set_state(TrSession.checkout_joint)
        await call.message.answer(
            checkin_question("post_joint"),
            reply_markup=scale_1_10("tr_post_joint", "tr_post_back:cns"),
        )
    else:
        await state.set_state(TrSession.checkout_cns)
        data = await state.get_data()
        sess = data.get("session") or []
        back = f"tr_back_ex:{max(0, len(sess) - 1)}" if sess else None
        await call.message.answer(
            checkin_question("post_cns"),
            reply_markup=scale_1_10("tr_post_cns", back),
        )
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_post_cns_"))
async def cb_post_cns(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(post_cns=_scale_val(call.data))
    await state.set_state(TrSession.checkout_joint)
    await call.message.answer(checkin_question("post_joint"), reply_markup=scale_1_10("tr_post_joint", "tr_post_back:cns"))
    await call.answer()


@trainer_router.callback_query(F.data.startswith("tr_post_joint_"))
async def cb_post_joint(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(post_joint=_scale_val(call.data))
    await state.set_state(TrSession.checkout_ctrl)
    await call.message.answer(
        checkin_question("post_ctrl"),
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
    summary = await call_gemini(workout_finish_prompt() + json.dumps(fin, ensure_ascii=False)[:2000])
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
    plan_meta = data.get("plan") or {}
    day_key = str(plan_meta.get("day_key") or "FB")
    done_ids = [str(ex["exercise_id"]) for ex in sess if ex.get("exercise_id") and ex.get("sets")]
    hist = append_history_entry(day_key, done_ids)
    state_pairs: Dict[str, str] = {
        "last_session_date": now_local().strftime("%Y-%m-%d"),
        "pending_workout_bonus_kcal": str(bonus),
        "pending_workout_bonus_date": now_local().strftime("%Y-%m-%d"),
        HISTORY_KEY: json.dumps(hist, ensure_ascii=False),
    }
    try:
        cycle_raw = SYSTEM_STATE.get("plan_cycle_json") or ""
        if cycle_raw:
            cycle = json.loads(cycle_raw)
            cycle = advance_cycle_cursor(cycle, day_key)
            state_pairs["plan_cycle_json"] = json.dumps(cycle, ensure_ascii=False)
    except Exception:
        pass
    await save_state_pairs(state_pairs)
    await send_cross_notification(
        "trainer",
        f"Бонус от тренировки: +{bonus} ккал на сегодня (≈{BONUS_CARB_FRAC*100:.0f}% углеводы, остальное жиры/белок).",
        bot_trainer,
        bot_nutrition,
    )
    cmp1 = await report_vs_avg_four_weeks({"exercises": fin})
    await call.message.answer(
        f"🎉 <b>Тренировка в копилке!</b>\n\n{summary}\n\n📊 {cmp1}",
        reply_markup=trainer_main_kb(),
    )
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
