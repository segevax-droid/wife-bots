import os
from pathlib import Path
from typing import List, Optional, Set

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

WIFE_TRAINER_TOKEN = os.getenv("WIFE_TRAINER_TOKEN", "").strip()
WIFE_NUTRITION_TOKEN = os.getenv("WIFE_NUTRITION_TOKEN", "").strip()
WIFE_GEMINI_API_KEY = os.getenv("WIFE_GEMINI_API_KEY", "").strip()
WIFE_SHEET_ID = os.getenv("WIFE_SHEET_ID", "").strip()
WIFE_GOOGLE_CREDENTIALS_JSON = os.getenv("WIFE_GOOGLE_CREDENTIALS_JSON", "").strip()
WIFE_TZ = os.getenv("WIFE_TZ", "Europe/Moscow").strip()
WIFE_RENDER_EXTERNAL_URL = os.getenv("WIFE_RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WIFE_WEBHOOK_SECRET = os.getenv("WIFE_WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))
WIFE_USE_POLLING = os.getenv("WIFE_USE_POLLING", "").strip().lower() in ("1", "true", "yes")

_raw_ids = os.getenv("WIFE_ALLOWED_CHAT_IDS", "").strip()
WIFE_ALLOWED_CHAT_IDS: Set[int] = set()
for part in _raw_ids.replace(";", ",").split(","):
    part = part.strip()
    if part.isdigit():
        WIFE_ALLOWED_CHAT_IDS.add(int(part))

# Gemini (hub-compatible; fallback on 404/503)
WIFE_GEMINI_MODEL = os.getenv("WIFE_GEMINI_MODEL", "gemini-2.5-flash").strip()

_raw_meal_times = os.getenv("WIFE_MEAL_REMINDER_TIMES", "").strip()
WIFE_MEAL_REMINDER_TIMES: List[str] = []
for part in _raw_meal_times.replace(";", ",").split(","):
    p = part.strip()
    if p:
        WIFE_MEAL_REMINDER_TIMES.append(p)

# Progression thresholds (tunable; documented in progression.py)
DIFF_EASY_MAX = 4
DIFF_MODERATE_MAX = 7
DIFF_HARD_MAX = 9

# Activity multipliers for TDEE (standard classification — Mifflin-St Jeor protocol)
# Source: common clinical/dietetics practice for PAL categories.
ACTIVITY_MULT_SEDENTARY = 1.2
ACTIVITY_MULT_LIGHT = 1.375
ACTIVITY_MULT_MODERATE = 1.55
ACTIVITY_MULT_HIGH = 1.725

# Deficit cap (avoid over-restriction)
DEFICIT_PCT_MIN = 0.15
DEFICIT_PCT_MAX = 0.20
DEFICIT_KCAL_CAP = 500

BONUS_CARB_FRAC = 0.75

SUPPORT_DIR = Path(__file__).resolve().parent.parent / "data"

def sheet_id_clean(raw: str) -> str:
    s = (raw or "").strip()
    if "/d/" in s:
        import re
        m = re.search(r"/d/([a-zA-Z0-9-_]+)", s)
        if m:
            return m.group(1)
    return s

WIFE_SHEET_ID = sheet_id_clean(WIFE_SHEET_ID)
