import asyncio
import json
import random
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.common import record_user_chat, send_cross_notification
from app.config import BONUS_CARB_FRAC
from app.gemini import call_gemini, download_file_as_base64, transcribe_voice_message
from app.sheets import append_row_to_sheet, get_settings_map, set_setting
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
from app.trainer.calendar_png import month_png_bytes
from app.trainer.energy import estimate_session_kcal
from app.trainer.exercise_db import ExerciseDB
from app.trainer.kb import (
    analogues_pick_kb,
    favorites_menu_kb,
    pick_next_kb,
    onboard_freq_kb,
    post_ex_fav_kb,
    today_go_kb,
    rest_kb,
    scale_1_10,
    scale_set_difficulty_kb,
    session_exercise_kb,
    swap_pending_kb,
    swap_plan_kb,
    tech_close_kb,
    trainer_main_kb,
    warmup_kb,
)
from app.trainer.month_plan import (
    days_map_for_png,
    ensure_month_plan,
    load_month_plan,
    mark_session_done,
    materialize_session,
    parse_freq,
    pick_session_for_start,
)
from app.trainer.plan_parse import plan_to_state
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
    format_today_session,
    format_warmup_card,
    parse_error_friendly,
    pre_begin_message,
    is_timed_ex,
    praise_after_set,
    rest_ping_message,
    session_kickoff,
    workout_finish_prompt,
)

trainer_router = Router(name="trainer")


class TrOnboard(StatesGroup):
    freq = State()


class TrSession(StatesGroup):
    check_cns = State()
    check_joint = State()
    check_psy = State()
    in_workout = State()
    checkout_cns = State()
    checkout_joint = State()
    checkout_ctrl = State()


async def _train_freq() -> int:
    s = await get_settings_map()
    raw = s.get("train_days_per_week") or SYSTEM_STATE.get("train_days_per_week")
    if not str(raw or "").strip():
        return 0
    return parse_freq(raw)


def _bonus_kcal(session_kcal: int) -> int:
    return min(int(round(session_kcal * 0.7)), 200)


def parse_set_local(text: str) -> Optional[Dict[str, float]]:
    s = re.sub(r"(\d),(\d)", r"\1.\2", (text or "").strip())
    m = re.search(
        r"(\d{1,3}(?:\.\d+)?)\s*(?:кг|kg)?\s*[x×*хX]\s*(\d{1,3}(?:\.\d+)?)(?:\s*[,;/]\s*(\d{1,2}))?",
        s,
        re.I,
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
    m = re.search(r"(?:^|[\s])(\d{1,3})\s*[,;/]\s*(\d{1,2})\s*$", s)
    if m:
        r, d = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 10:
            return {"weight": 0.0, "reps": float(r), "difficulty": float(d)}
    m = re.search(r"^[x×*]\s*(\d{1,3})$", s, re.I)
    if m:
        return {"weight": 0.0, "reps": float(m.group(1))}
    if re.fullmatch(r"\d{1,3}", s):
        return {"weight": 0.0, "reps": float(s)}
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


def parse_duration_local(text: str) -> Optional[Dict[str, float]]:
    s = re.sub(r"(\d),(\d)", r"\1.\2", (text or "").strip().lower())
    m = re.search(r"(\d{1,4})\s*(?:секунд|сек|сек\.|с\.|с\b|sec|s\b)", s)
    if m:
        sec = float(m.group(1))
        if 3 <= sec <= 3600:
            return {"weight": 0.0, "reps": sec}
    m = re.search(r"(?:^|[\s])(\d{1,4})\s*[,;/]\s*(\d{1,2})\s*$", s)
    if m:
        sec, d = int(m.group(1)), int(m.group(2))
        if 3 <= sec <= 3600 and 1 <= d <= 10:
            return {"weight": 0.0, "reps": float(sec), "difficulty": float(d)}
    if re.fullmatch(r"\d{1,4}", s.strip()):
        sec = float(s.strip())
        if 3 <= sec <= 3600:
            return {"weight": 0.0, "reps": sec}
    return None


async def parse_duration_gemini(text: str) -> Optional[Dict[str, float]]:
    local = parse_duration_local(text)
    if local:
        return local
    if not re.search(r"\d", text or ""):
        return None
    p = (
        f'Извлеки длительность удержания в СЕКУНДАХ (целое число). '
        f'Ответ только JSON: {{"weight":0,"reps":число_секунд}}. Строка: {text}'
    )
    raw = await call_gemini(p)
    m = re.search(r"\{[^}]+\}", raw)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
        sec = float(j.get("reps", j.get("seconds", 0)))
        if sec < 3 or sec > 3600:
            return None
        return {"weight": 0.0, "reps": sec}
    except Exception:
        return None


def _session_ex_from_plan(ex: Dict[str, Any], db: ExerciseDB) -> Dict[str, Any]:
    w = ex.get("weight_kg")
    base = db.by_id.get(ex.get("exercise_id", ""))
    if w is None and (ExerciseDB.is_bodyweight(base) or ExerciseDB.is_bodyweight({"name": ex.get("name")})):
        w = 0
    timed = ex.get("is_timed") or ExerciseDB.is_timed(base) or ExerciseDB.is_timed({"name": ex.get("name")})
    row: Dict[str, Any] = {
        "exercise_id": ex["exercise_id"],
        "name": ex["name"],
        "sets": [],
        "target_sets": ex.get("sets", 3),
        "weight_kg_plan": w,
    }
    if timed:
        row["is_timed"] = True
        row["duration_sec_target"] = int(ex.get("duration_sec_target") or 45)
    else:
        row["reps_target"] = ex.get("reps_target", 10)
    return row


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
    row: Dict[str, Any] = {
        "exercise_id": exercise_id,
        "name": name,
        "sets": [],
        "target_sets": template.get("target_sets", template.get("sets", 3)),
        "weight_kg_plan": template.get("weight_kg_plan", template.get("weight_kg")),
    }
    if template.get("is_timed"):
        row["is_timed"] = True
        row["duration_sec_target"] = int(template.get("duration_sec_target") or 45)
    else:
        row["reps_target"] = template.get("reps_target", 10)
    sess[idx] = row


async def _need_onboard(message: types.Message, state: FSMContext) -> bool:
    if await _train_freq():
        return False
    await state.set_state(TrOnboard.freq)
    await message.answer(
        "👋 Я собираю программу за тебя — тебе не нужно выбирать упражнения.\n\n"
        "Сколько раз в неделю тренируемся? От этого зависит план на месяц "
        "и как часто будет кардио (после каждых двух силовых).",
        reply_markup=onboard_freq_kb(),
    )
    return True


@trainer_router.message(Command("start"))
@trainer_router.message(Command("menu"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await record_user_chat("trainer", message.chat.id)
    if await _need_onboard(message, state):
        return
    await message.answer(
        "Привет! Я твой тренер 💪\n\n"
        "🏋️ <b>Старт</b> — сегодняшняя тренировка по плану\n"
        "🗓 <b>План на месяц</b> — что делаем и когда\n"
        "📊 Отчёты — день / неделя / месяц\n\n"
        "С чего начнём?",
        reply_markup=trainer_main_kb(),
    )


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


@trainer_router.callback_query(F.data.startswith("tr_ob_freq:"))
async def cb_onboard_freq(call: types.CallbackQuery, state: FSMContext):
    freq = parse_freq(call.data.split(":")[-1])
    await set_setting("train_days_per_week", str(freq))
    await save_state_pairs({"train_days_per_week": str(freq), "onboarding_complete": "1"})
    await state.clear()
    await call.message.answer("Собираю план на месяц…")
    plan = await ensure_month_plan(freq)
    n = len(plan.get("sessions") or [])
    scheme = "верх/низ" if freq >= 4 else "всё тело"
    await call.message.answer(
        f"Готово: <b>{freq} тренировки в неделю</b> · схема «{scheme}».\n"
        f"В этом месяце {n} сессий, кардио — после каждых двух силовых.\n\n"
        "Жми «Старт тренировки», когда будешь в зале.",
        reply_markup=trainer_main_kb(),
    )
    await call.answer()


async def _save_plan(message: types.Message, plan: dict) -> None:
    st = plan_to_state(plan)
    await save_state_pairs({"plan_last_json": json.dumps(st, ensure_ascii=False)})
    await append_row_to_sheet("План_тренировок", [now_local().strftime("%Y-%m-%d"), json.dumps(st, ensure_ascii=False)])
    db = await ExerciseDB.load()
    await message.answer(format_plan_message(plan if plan.get("title") else {**plan, **st}, db), reply_markup=trainer_main_kb())


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
async def cb_cal(call: types.CallbackQuery, state: FSMContext):
    freq = await _train_freq()
    if not freq:
        await _need_onboard(call.message, state)
        await call.answer()
        return
    plan = await ensure_month_plan(freq)
    nl = now_local()
    png = await month_png_bytes(nl.year, nl.month, days_map_for_png(plan))
    caption = (
        f"🗓 <b>План на месяц</b> · {freq}× в неделю\n"
        "Кардио — после каждых двух силовых. Название в клетке — тип сессии."
    )
    await call.message.answer_photo(BufferedInputFile(png, filename="plan.png"), caption=caption)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_reset_session")
async def cb_reset(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Сессия сброшена.", reply_markup=trainer_main_kb())
    await call.answer()


@trainer_router.callback_query(F.data == "tr_start_flow")
async def cb_start_flow(call: types.CallbackQuery, state: FSMContext):
    freq = await _train_freq()
    if not freq:
        await _need_onboard(call.message, state)
        await call.answer()
        return
    month = await ensure_month_plan(freq)
    meta, note = pick_session_for_start(month)
    if not meta:
        await call.message.answer(note or "План на месяц пуст.", reply_markup=trainer_main_kb())
        await call.answer()
        return
    await state.update_data(pending_session=meta)
    await call.message.answer(format_today_session(meta, note), reply_markup=today_go_kb())
    await call.answer()


@trainer_router.callback_query(F.data == "tr_go")
async def cb_go(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("pending_session"):
        await cb_start_flow(call, state)
        return
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
    await call.message.answer(body)
    await _begin_session(call.message, state)
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


async def _after_set_logged(
    message: types.Message,
    state: FSMContext,
    idx: int,
    praise: str,
    *,
    ask_difficulty: bool = False,
    set_i: int = 0,
) -> None:
    rest_note = (
        praise
        + "\n\n<i>Отдых — по желанию (таймер ниже). Следующий подход можно писать сразу в карточке 👇</i>"
    )
    if ask_difficulty:
        await message.answer(
            praise + "\n\nНасколько тяжело было <b>(1–10)</b>?",
            reply_markup=scale_set_difficulty_kb(idx, set_i),
        )
        return
    timer_id = f"{idx}_{set_i}_{random.randint(1000, 9999)}"
    await message.answer(rest_note, reply_markup=rest_kb(timer_id, ex_idx=idx))
    await _show_exercise(message, state)


async def _kb_for_exercise(state: FSMContext, ex_idx: int) -> InlineKeyboardMarkup:
    data = await state.get_data()
    sess = data.get("session") or []
    ex = sess[ex_idx] if ex_idx < len(sess) else {}
    empty = not (ex.get("sets") or [])
    fav = is_favorite(str(ex.get("exercise_id", "")))
    return session_exercise_kb(ex_idx, is_fav=fav, sets_empty=empty)


async def _begin_session(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    meta = data.get("pending_session")
    db = await ExerciseDB.load()
    if meta:
        plan = materialize_session(meta, db)
    else:
        try:
            plan = json.loads(SYSTEM_STATE.get("plan_last_json") or "{}")
        except Exception:
            plan = {}
    exercises = plan.get("exercises") or []
    if not exercises:
        await message.answer(
            "Не смогла собрать упражнения. Проверь лист «Список упражнений».",
            reply_markup=trainer_main_kb(),
        )
        return
    st = plan_to_state(plan)
    await save_state_pairs({"plan_last_json": json.dumps(st, ensure_ascii=False)})
    sess: List[Dict[str, Any]] = [_session_ex_from_plan(ex, db) for ex in exercises]
    pending = list(range(len(sess)))
    await state.update_data(
        started_at=now_local().isoformat(),
        plan=plan,
        session=sess,
        cur_idx=-1,
        pending_indices=pending,
        warmup_done=False,
        completed_count=0,
        await_baseline=False,
        session_n=plan.get("session_n") or (meta or {}).get("n"),
    )
    await state.set_state(TrSession.in_workout)
    await message.answer(session_kickoff(len(sess), plan))
    await _show_warmup(message, state)


@trainer_router.callback_query(F.data == "tr_begin_session")
async def cb_begin_session(call: types.CallbackQuery, state: FSMContext):
    await _begin_session(call.message, state)
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
    db = await ExerciseDB.load()
    base = db.by_id.get(ex.get("exercise_id", ""))
    timed = is_timed_ex(ex, base)
    if message.photo:
        b64 = await download_file_as_base64(bot, message.photo[-1].file_id)
        if timed:
            txt = await call_gemini(
                "Считай длительность удержания в СЕКУНДАХ с фото таймера. Ответ одной строкой: число секунд.",
                media_b64=b64,
                mime_type="image/jpeg",
            )
        else:
            txt = await call_gemini(
                "Считай вес (кг) и повторы с фото. Ответ одной строкой: числа.",
                media_b64=b64,
                mime_type="image/jpeg",
            )
    if timed:
        parsed = await parse_duration_gemini(txt)
    else:
        parsed = await parse_set_gemini(txt)
    if not parsed:
        await message.answer(parse_error_friendly(is_timed=timed))
        return
    row: Dict[str, Any] = {"weight": parsed["weight"], "reps": parsed["reps"]}
    if parsed.get("difficulty"):
        row["difficulty"] = int(parsed["difficulty"])
    ex["sets"].append(row)
    await state.update_data(session=sess, await_baseline=False)
    si = len(ex["sets"]) - 1
    tgt = int(ex.get("target_sets") or 3)
    if row.get("difficulty"):
        await _after_set_logged(
            message,
            state,
            idx,
            praise_after_set(
                parsed["weight"],
                parsed["reps"],
                int(row["difficulty"]),
                si,
                tgt,
                is_timed=timed,
            ),
        )
        return
    await _after_set_logged(
        message,
        state,
        idx,
        praise_after_set(parsed["weight"], parsed["reps"], None, si, tgt, is_timed=timed),
        ask_difficulty=True,
        set_i=si,
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
    tgt = int(ex.get("target_sets") or 3)
    last = ex["sets"][si]
    db = await ExerciseDB.load()
    base = db.by_id.get(ex.get("exercise_id", ""))
    timed = is_timed_ex(ex, base)
    praise = praise_after_set(
        float(last.get("weight", 0)),
        float(last.get("reps", 0)),
        int(diff),
        si,
        tgt,
        is_timed=timed,
    )
    await _after_set_logged(call.message, state, idx, praise)
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


@trainer_router.callback_query(F.data.startswith("tr_rest_skip:"))
async def cb_rest_skip(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await call.answer("Ок, без таймера")


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
    db = await ExerciseDB.load()
    sess[idx] = _session_ex_from_plan(ex, db)
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
        if ex.get("is_timed"):
            tnx = 0.0
        else:
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
    try:
        n = int(data.get("session_n") or plan_meta.get("session_n") or 0)
        month = load_month_plan()
        if n and month:
            month = mark_session_done(month, n)
            state_pairs["month_plan_json"] = json.dumps(month, ensure_ascii=False)
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
async def cb_rm_plan(call: types.CallbackQuery, state: FSMContext):
    await cb_start_flow(call, state)


@trainer_router.callback_query(F.data == "tr_rm_light")
async def cb_rm_light(call: types.CallbackQuery):
    await handle_light_home(call.message.bot, call.message.chat.id)
    await call.answer()


@trainer_router.callback_query(F.data == "tr_rm_no")
async def cb_rm_no(call: types.CallbackQuery):
    await call.answer()
