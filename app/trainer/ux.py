import html
import random
from typing import Any, Dict, List, Optional

from app.trainer.exercise_db import ExerciseDB
from app.trainer.favorites import is_favorite
from app.trainer.program import SPLIT_LABELS, canonical_group


def is_bodyweight_ex(ex: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> bool:
    if ExerciseDB.is_bodyweight(base or {}):
        return True
    return ExerciseDB.is_bodyweight({"name": ex.get("name"), "equipment": ""})


def _h(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


_GROUP_EMOJI: Dict[str, str] = {
    "ноги": "🦵",
    "спина": "🔙",
    "грудь": "💪",
    "плечи": "🎯",
    "руки": "💥",
    "кор": "🧱",
    "пресс": "⚡",
    "ягодицы": "🍑",
    "прочее": "✨",
}

_SCALE_HINT: Dict[str, str] = {
    "cns": "ЦНС · сон · бодрость",
    "joint": "суставы · комфорт в движении",
    "psy": "настрой · желание потренироваться",
    "post_cns": "энергия после нагрузки",
    "post_joint": "ощущения в суставах",
    "post_ctrl": "контроль техники (цель ~70% с запасом)",
}


def group_emoji(group: str) -> str:
    g = (group or "").strip().lower()
    for key, em in _GROUP_EMOJI.items():
        if key in g:
            return em
    return _GROUP_EMOJI["прочее"]


def checkin_question(kind: str) -> str:
    hint = _SCALE_HINT.get(kind, kind)
    prefixes = {
        "cns": "🌅 <b>Как ты сегодня?</b>",
        "joint": "🦴 <b>Суставы в порядке?</b>",
        "psy": "🔥 <b>Настрой на зал?</b>",
        "post_cns": "🏁 <b>Финиш близко!</b>",
        "post_joint": "🦴 <b>Как суставы?</b>",
        "post_ctrl": "🎯 <b>Последний штрих</b>",
    }
    head = prefixes.get(kind, "Оцени по шкале")
    return f"{head}\nОцени <i>{hint}</i> — от 1 (тяжело) до 10 (отлично):"


def checkin_nudge(kind: str, value: int) -> str:
    if kind == "cns":
        if value <= 4:
            return "Поняла — сегодня без гонки за рекордами, слушаем тело 🤍"
        if value >= 8:
            return "Отличный ресурс — можно планировать уверенный рабочий вес 💪"
        return "Ровный день — идеально для стабильной техники 👌"
    if kind == "joint":
        if value <= 4:
            return "Бережём суставы: меньше вес, больше контроля. Кнопка «Аналоги» всегда рядом."
        if value >= 8:
            return "Суставы счастливы — смело идём по плану ✨"
        return "Нормальный фон — разогрев обязателен, не пропускай 💫"
    if kind == "psy":
        if value <= 4:
            return "Можно укоротить тренировку — главное прийти и сделать хоть что-то. Я с тобой."
        if value >= 8:
            return "Заряжено! Сейчас соберём сильную сессию 🔥"
        return "Спокойный настрой тоже сила — пойдём шаг за шагом."
    return ""


def pre_begin_message(pre_cns: int, pre_joint: int, pre_psy: int) -> str:
    low = min(pre_cns, pre_joint, pre_psy)
    if low <= 4:
        tone = "Сегодня режим «мягко, но осмысленно» — качество важнее количества."
    elif pre_psy >= 8:
        tone = "Чувствую заряд — разогрейся 5–7 минут и вперёд, время выжимать максимум из плана!"
    else:
        tone = "Разминка 5 минут (суставы + лёгкая активация), потом жми «Начать тренировку» — я рядом на каждом подходе."
    return (
        "✅ <b>Чек-ин готов</b>\n"
        f"{tone}\n\n"
        "Во время сессии: «Техника» — коротко про упражнение, голосом или текстом можно писать <code>40×10</code>."
    )


def session_kickoff(n: int, plan_meta: Optional[Dict[str, Any]] = None) -> str:
    meta = plan_meta or {}
    note = _h((meta.get("coach_note") or meta.get("note") or "").strip())
    warmup = _h((meta.get("warmup_note") or "").strip())
    parts = [f"🚀 <b>Поехали!</b> Впереди {n} упражнений — один подход за раз."]
    if warmup:
        parts.append(f"🫧 <i>Разминка:</i> {warmup}")
    if note:
        parts.append(f"💬 {note}")
    return "\n".join(parts)


def format_plan_message(plan: Dict[str, Any], db: Optional[ExerciseDB] = None) -> str:
    exs: List[Dict[str, Any]] = plan.get("exercises") or []
    title = _h((plan.get("title") or "Тренировка на сегодня").strip())
    coach = _h((plan.get("coach_note") or plan.get("note") or "").strip())
    warmup = _h((plan.get("warmup_note") or "").strip())
    split = (plan.get("split") or "full").strip()
    day_key = (plan.get("day_key") or "").strip()
    split_lbl = SPLIT_LABELS.get(split, split)
    sub = f"{len(exs)} упражнений · {split_lbl}"
    if day_key:
        sub += f" · день {day_key}"
    lines: List[str] = [f"📋 <b>{title}</b>", f"<i>{sub}</i>", ""]
    if coach:
        lines.append(f"💬 {coach}")
        lines.append("")
    if warmup:
        lines.append(f"🫧 <b>Разминка:</b> {warmup}")
        lines.append("")
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for ex in exs:
        grp = (ex.get("group") or "").strip()
        if not grp and db and ex.get("exercise_id"):
            row = db.by_id.get(ex["exercise_id"])
            if row:
                grp = canonical_group(row)
        label = grp or "Блок"
        if label not in grouped:
            grouped[label] = []
            order.append(label)
        grouped[label].append(ex)
    for label in order:
        em = group_emoji(label)
        lines.append(f"{em} <b>{_h(label)}</b>")
        for ex in grouped[label]:
            w = ex.get("weight_kg")
            if w is not None and float(w) == 0:
                wpart = " · <b>0×</b>"
            elif w:
                wpart = f", ориентир <b>{w:g} кг</b>"
            else:
                wpart = ""
            star = "★ " if is_favorite(str(ex.get("exercise_id", ""))) else ""
            lines.append(
                f"  • {star}{_h(ex['name'])} — <b>{ex.get('sets', 3)}×{ex.get('reps_target', 10)}</b>{wpart}"
            )
        lines.append("")
    lines.append("<i>Готова? «Старт тренировки» → чек-ин → «Начать».</i>")
    return "\n".join(lines).strip()


def _short_technique(base: Optional[Dict[str, Any]]) -> str:
    if not base:
        return ""
    tech = (base.get("technique") or "").strip()
    if tech:
        return tech if len(tech) <= 160 else tech[:157] + "…"
    tips = base.get("tips") or []
    if tips:
        return tips[0]
    return ""


def exercise_card(
    ex: Dict[str, Any],
    idx: int,
    total: int,
    base: Optional[Dict[str, Any]] = None,
) -> str:
    done = len(ex.get("sets") or [])
    tgt = int(ex.get("target_sets") or 3)
    reps = ex.get("reps_target") or 10
    wplan = ex.get("weight_kg_plan")
    if wplan is None and ex.get("weight_kg") is not None:
        wplan = ex.get("weight_kg")
    progress = "▓" * done + "░" * max(0, tgt - done)
    if wplan is not None and float(wplan) == 0:
        hint = f"ориентир <b>0</b> × {reps}"
    elif wplan:
        hint = f"ориентир <b>{float(wplan):g} кг</b> × {reps}"
    elif is_bodyweight_ex(ex, base):
        hint = f"ориентир <b>0</b> × {reps}"
    else:
        hint = f"ориентир <b>{reps}</b> повторов (или <code>0×{reps}</code>)"
    grp = (base.get("group") if base else "") or ""
    equip = (base.get("equipment") if base else "") or ""
    meta_parts = [p for p in (grp, equip) if p]
    meta = f" · {' · '.join(meta_parts)}" if meta_parts else ""
    em = group_emoji(grp)
    tech_line = _short_technique(base)
    fav = is_favorite(str(ex.get("exercise_id", "")))
    name = _h(ex["name"])
    if fav:
        name = f"★ {name}"
    body = [
        f"{em} <b>Упражнение {idx + 1}/{total}</b> · подход <b>{done + 1}/{tgt}</b>  [{progress}]",
        f"<b>{name}</b>{_h(meta) if meta else ''}",
        f"🎯 {hint}",
    ]
    if tech_line:
        body.append(f"<i>{_h(tech_line)}</i>")
    ex_hint = "<code>0×10</code>" if (wplan == 0 or is_bodyweight_ex(ex, base)) else "<code>40×10</code>"
    body.append(f"✍️ Результат: {ex_hint} или «40 кг 10 раз» · тяжесть: <code>{'0×10, 7' if wplan == 0 else '40×10, 7'}</code>")
    body.append("Кнопка <b>Техника</b> — полное объяснение.")
    return "\n".join(body)


def format_technique_message(base: Dict[str, Any]) -> str:
    name = _h(base.get("name") or "Упражнение")
    grp = base.get("group") or ""
    equip = base.get("equipment") or ""
    em = group_emoji(grp)
    head = f"{em} <b>Техника: {name}</b>"
    if grp or equip:
        head += f"\n<i>{_h(' · '.join(x for x in (grp, equip) if x))}</i>"
    tech = _h((base.get("technique") or "").strip())
    tips = base.get("tips") or []
    chunks = [head, ""]
    if tech:
        chunks.append(tech)
    if tips:
        chunks.append("")
        chunks.append("<b>На что смотреть:</b>")
        for t in tips[:4]:
            chunks.append(f"  • {_h(t)}")
    return "\n".join(chunks).strip() or "Пока нет описания — спроси тренера в зале или замени упражнение."


def praise_after_set(
    weight: float,
    reps: float,
    difficulty: Optional[int],
    set_i: int,
    target_sets: int,
) -> str:
    if weight <= 0:
        core = f"✅ Записала: <b>0 × {reps:g}</b>"
    else:
        core = f"✅ Записала: <b>{weight:g} кг × {reps:g}</b>"
    if difficulty is not None:
        core += f", тяжесть <b>{difficulty}/10</b>"
        if difficulty <= 3:
            tail = " — лёгко, можно чуть смелее на следующих, если техника чистая."
        elif difficulty >= 8:
            tail = " — тяжело, ты молодец! Дыши и не гонись за весом в следующем сете."
        else:
            tail = " — ровная рабочая зона, так и надо."
        core += tail
    if set_i + 1 >= target_sets:
        core += "\n\n🏁 Это был последний подход в упражнении — жми «Завершить упражнение» или допиши, если ещё есть силы."
    else:
        cheers = [
            "Отличный подход! ",
            "Так держать! ",
            "Сильно! ",
            "Красота! ",
        ]
        core = random.choice(cheers) + core
    return core


def rest_ping_message() -> str:
    opts = [
        "⏰ Отдых закончился — следующий подход ждёт. Пиши вес×повторы, например <code>40×10</code>.",
        "💪 Готова? Следующий сет — ты уже ближе к цели. Формат: <code>50×8</code>.",
        "🔔 Время работать! Собери дыхание и жми — результат в чат: <code>40×10</code>.",
    ]
    return random.choice(opts)


def parse_error_friendly() -> str:
    return (
        "🤔 Не разобрала подход.\n"
        "Примеры: <code>60×8</code>, «60 кг 8 раз».\n"
        "Аналоги упражнения — кнопкой «Аналоги ×3», не текстом."
    )


def format_warmup_card(plan: Dict[str, Any]) -> str:
    wm = plan.get("warmup") or {}
    note = _h((plan.get("warmup_note") or "").strip())
    mins = wm.get("minutes") or 6
    lines = [
        f"🫧 <b>Разогрев · ~{mins} мин</b>",
        f"<b>Кардио:</b> {_h(wm.get('cardio') or 'лёгкое кардио в разговорном темпе.')}",
        f"<b>Суставы:</b> {_h(wm.get('mobility') or 'круговая разминка.')}",
    ]
    if note:
        lines.append("")
        lines.append(note)
    lines.append("")
    lines.append("<i>Когда готов(а) — жми «К упражнениям».</i>")
    return "\n".join(lines)


def format_picker_message(
    session: List[Dict[str, Any]],
    pending_indices: List[int],
    done_count: int,
) -> str:
    total = done_count + len(pending_indices)
    if not pending_indices:
        return "Очередь пуста — можно завершить тренировку."
    head_i = pending_indices[0]
    head = session[head_i]
    rec = _h(head.get("name") or "упражнение")
    lines = [
        f"📌 <b>Следующее</b> ({done_count + 1}/{total})",
        f"Рекомендую: <b>{rec}</b>",
        "",
        "Выбери из плана кнопкой ниже или возьми рекомендацию.",
    ]
    return "\n".join(lines)


def format_favorites_list(names: List[tuple]) -> str:
    if not names:
        return "Пока нет любимых. На карточке упражнения жми «★ В любимые»."
    lines = ["⭐ <b>Любимые упражнения</b>", ""]
    for eid, nm in names:
        lines.append(f"• {_h(nm)}")
    lines.append("")
    lines.append("<i>В плане они встречаются чуть чаще, но не больше 1–2 за тренировку.</i>")
    return "\n".join(lines)


def workout_finish_prompt() -> str:
    return (
        "Ты тёплый персональный тренер на русском. "
        "По данным сессии дай ровно 3 строки, каждая с «•»: "
        "1) искренняя похвала, 2) один конкретный инсайт по нагрузке, "
        "3) мягкий совет на следующую тренировку. Без штампов и воды. "
        "Данные (JSON): "
    )
