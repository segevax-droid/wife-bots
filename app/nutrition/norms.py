import logging
from typing import Dict, Optional, Tuple

from app.config import (
    ACTIVITY_MULT_HIGH,
    ACTIVITY_MULT_LIGHT,
    ACTIVITY_MULT_MODERATE,
    ACTIVITY_MULT_SEDENTARY,
    BONUS_CARB_FRAC,
    DEFICIT_KCAL_CAP,
    DEFICIT_PCT_MAX,
    DEFICIT_PCT_MIN,
)
from app.sheets import append_row_to_sheet, get_sheet_rows_for_period
from app.state import SYSTEM_STATE, now_local, save_state_pairs

logger = logging.getLogger("wife_bots")

# Defaults match the previous fixed split (1.5 г белка / 0.9 г жира на кг).
PROT_G_KG_DEFAULT = 1.5
FAT_G_KG_DEFAULT = 0.9
PROT_G_KG_MIN, PROT_G_KG_MAX = 1.0, 2.5
FAT_G_KG_MIN, FAT_G_KG_MAX = 0.6, 1.4
MACRO_G_KG_STEP = 0.1


def _activity_mult(code: str) -> float:
    return {
        "sed": ACTIVITY_MULT_SEDENTARY,
        "light": ACTIVITY_MULT_LIGHT,
        "mod": ACTIVITY_MULT_MODERATE,
        "high": ACTIVITY_MULT_HIGH,
    }.get((code or "").strip().lower(), ACTIVITY_MULT_LIGHT)


def bmr_mifflin(kg: float, cm: float, age: int, sex: str) -> float:
    if (sex or "").lower().startswith("ж"):
        return 10 * kg + 6.25 * cm - 5 * age - 161
    return 10 * kg + 6.25 * cm - 5 * age + 5


def _float_setting(raw: str, default: float) -> float:
    try:
        v = float(str(raw or "").strip().replace(",", "."))
        if v > 0:
            return v
    except Exception:
        pass
    return default


def round_g_kg(value: float) -> float:
    return round(value * 10) / 10


def clamp_prot_g_kg(value: float) -> float:
    return min(max(round_g_kg(value), PROT_G_KG_MIN), PROT_G_KG_MAX)


def clamp_fat_g_kg(value: float) -> float:
    return min(max(round_g_kg(value), FAT_G_KG_MIN), FAT_G_KG_MAX)


def read_macro_prefs(settings: Dict[str, str]) -> Tuple[float, float]:
    p = _float_setting(settings.get("prot_g_kg", ""), PROT_G_KG_DEFAULT)
    f_ = _float_setting(settings.get("fat_g_kg", ""), FAT_G_KG_DEFAULT)
    return clamp_prot_g_kg(p), clamp_fat_g_kg(f_)


def _macros_with_bonus(
    weight_kg: float,
    target_kcal: float,
    bonus_kcal: float,
    prot_g_kg: float = PROT_G_KG_DEFAULT,
    fat_g_kg: float = FAT_G_KG_DEFAULT,
) -> Tuple[float, float, float]:
    w = max(weight_kg, 0.1)
    protein = prot_g_kg * w
    fat = fat_g_kg * w
    protein_kcal = protein * 4
    if protein_kcal >= target_kcal:
        protein = target_kcal / 4
        fat = 0.0
        carbs = 0.0
    else:
        fat_kcal = min(fat * 9, target_kcal - protein_kcal)
        fat = fat_kcal / 9
        carbs = max(target_kcal - protein_kcal - fat_kcal, 0) / 4
    if bonus_kcal > 0:
        carb_kcal_b = bonus_kcal * BONUS_CARB_FRAC
        rem = bonus_kcal - carb_kcal_b
        carbs += carb_kcal_b / 4
        protein += (rem / 2) / 4
        fat += (rem / 2) / 9
    return protein, fat, carbs


def fat_g_kg_from_rest_share(
    weight_kg: float,
    target_kcal: float,
    prot_g_kg: float,
    fat_rest_pct: int,
) -> float:
    """Доля жиров в ккал, оставшихся после белка; углеводы забирают остаток."""
    w = max(weight_kg, 0.1)
    rest = max(target_kcal - prot_g_kg * w * 4, 0)
    fat_kcal = rest * (max(0, min(fat_rest_pct, 100)) / 100.0)
    return clamp_fat_g_kg(fat_kcal / 9 / w)


async def recalc_norms_for_user(settings: Dict[str, str], reason: str = "", consume_bonus: bool = True) -> str:
    try:
        w = float(str(settings.get("weight_kg", "")).replace(",", "."))
        h = float(str(settings.get("height_cm", "")).replace(",", "."))
        age = int(float(settings.get("age", "0")))
        sex = settings.get("sex", "").strip()
        act = settings.get("activity", "light").strip()
        goal = settings.get("goal", "cut").strip()
    except Exception:
        return ""

    if w <= 0 or h <= 0 or age <= 0 or not sex:
        return ""

    bmr = bmr_mifflin(w, h, age, sex)
    tdee = bmr * _activity_mult(act)
    deficit_pct = DEFICIT_PCT_MIN if goal == "cut" else DEFICIT_PCT_MAX
    deficit_abs = min(tdee * deficit_pct, DEFICIT_KCAL_CAP, max(0.0, tdee - bmr))
    base_kcal = max(bmr, tdee - deficit_abs)

    bonus = 0.0
    if reason in ("weight", "daily", "onboarding"):
        try:
            bonus = float(str(SYSTEM_STATE.get("pending_workout_bonus_kcal", "0")).replace(",", "."))
        except Exception:
            bonus = 0.0
        if bonus > 0 and consume_bonus:
            await save_state_pairs({"pending_workout_bonus_kcal": "0"})

    total_kcal = base_kcal + bonus
    prot_g_kg, fat_g_kg = read_macro_prefs(settings)
    protein, fat, carbs_g = _macros_with_bonus(w, base_kcal, bonus, prot_g_kg, fat_g_kg)

    prev_w = str(SYSTEM_STATE.get("last_norms_weight", "")).strip()
    notify = ""
    if prev_w:
        try:
            if abs(w - float(prev_w.replace(",", "."))) >= 2.0:
                notify = "\nВес изменился ≥2 кг от последнего пересчёта — нормы пересчитаны."
        except Exception:
            pass

    row = [
        now_local().strftime("%Y-%m-%d %H:%M"),
        f"{w:.1f}",
        f"{h:.0f}",
        str(age),
        sex,
        act,
        f"{bmr:.0f}",
        f"{tdee:.0f}",
        f"{deficit_pct*100:.0f}",
        f"{total_kcal:.0f}",
        f"{protein:.0f}",
        f"{fat:.0f}",
        f"{carbs_g:.0f}",
        f"{bonus:.0f}",
        reason or "mifflin",
    ]
    await append_row_to_sheet("Нормы_КБЖУ", row)
    await save_state_pairs({"last_norms_weight": f"{w:.1f}"})

    return (
        f"<b>Нормы на сегодня</b>\n"
        f"Базовый обмен (BMR): {bmr:.0f} ккал\n"
        f"С учётом активности (TDEE): {tdee:.0f} ккал\n"
        f"Дефицит: {deficit_pct*100:.0f}% (−{deficit_abs:.0f} ккал)\n"
        f"Цель: <b>{total_kcal:.0f} ккал</b>"
        f"{f' +{bonus:.0f} бонус от тренировки' if bonus else ''}\n\n"
        f"Белки: {protein:.0f} г ({prot_g_kg:.1f} г/кг)\n"
        f"Жиры: {fat:.0f} г ({fat_g_kg:.1f} г/кг)\n"
        f"Углеводы: {carbs_g:.0f} г (остаток калорий)"
        f"{notify}"
    )


async def load_norm_targets(settings: Dict[str, str]) -> Optional[Dict[str, float]]:
    try:
        w = float(str(settings.get("weight_kg", "")).replace(",", "."))
        h = float(str(settings.get("height_cm", "")).replace(",", "."))
        age = int(float(settings.get("age", "0")))
        sex = settings.get("sex", "").strip()
        act = settings.get("activity", "light").strip()
        goal = settings.get("goal", "cut").strip()
    except Exception:
        return None
    if w <= 0 or h <= 0 or age <= 0 or not sex:
        return None
    bmr = bmr_mifflin(w, h, age, sex)
    tdee = bmr * _activity_mult(act)
    deficit_pct = DEFICIT_PCT_MIN if goal == "cut" else DEFICIT_PCT_MAX
    deficit_abs = min(tdee * deficit_pct, DEFICIT_KCAL_CAP, max(0.0, tdee - bmr))
    base_kcal = max(bmr, tdee - deficit_abs)
    try:
        bonus = float(str(SYSTEM_STATE.get("pending_workout_bonus_kcal", "0")).replace(",", "."))
    except Exception:
        bonus = 0.0
    total_kcal = base_kcal + bonus
    prot_g_kg, fat_g_kg = read_macro_prefs(settings)
    p, f_, c = _macros_with_bonus(w, base_kcal, bonus, prot_g_kg, fat_g_kg)
    p0, f0, _ = _macros_with_bonus(w, base_kcal, 0.0, prot_g_kg, fat_g_kg)
    clamped = abs(prot_g_kg * w - p0) > 1.0 or abs(fat_g_kg * w - f0) > 1.0
    return {
        "kcal": total_kcal,
        "p": p,
        "f": f_,
        "c": c,
        "base_kcal": base_kcal,
        "bonus": bonus,
        "prot_g_kg": prot_g_kg,
        "fat_g_kg": fat_g_kg,
        "weight_kg": w,
        "clamped": 1.0 if clamped else 0.0,
    }


async def today_intake() -> Tuple[float, float, float, float]:
    rows = await get_sheet_rows_for_period("Питание", 3, date_col=0)
    day = now_local().strftime("%Y-%m-%d")
    p, f_, c, k = 0.0, 0.0, 0.0, 0.0
    for r in rows:
        if not r:
            continue
        dcell = str(r[0]).strip()
        if not dcell.startswith(day):
            continue
        try:
            p += float(str(r[4]).replace(",", "."))
            f_ += float(str(r[5]).replace(",", "."))
            c += float(str(r[6]).replace(",", "."))
            k += float(str(r[7]).replace(",", "."))
        except Exception:
            continue
    return p, f_, c, k


def progress_bar(cur: float, tgt: float, n: int = 8) -> str:
    if tgt <= 0:
        return "░" * n
    ratio = min(max(cur / tgt, 0.0), 1.0)
    filled = int(round(ratio * n))
    return "█" * filled + "░" * (n - filled)
