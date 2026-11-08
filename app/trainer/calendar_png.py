import calendar
import io
from datetime import date
from typing import Any, Dict

from PIL import Image, ImageDraw, ImageFont

from app.state import now_local

_WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_MONTH_RU = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def _font(size: int):
    for name in (
        "arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list:
    words = (text or "").split()
    if not words:
        return []
    lines: list = []
    cur = words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines[:3]


async def month_png_bytes(year: int, month: int, session_by_day: Dict[int, Any]) -> bytes:
    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdayscalendar(year, month)
    cell_w, cell_h = 124, 96
    ox, oy = 24, 72
    w = ox * 2 + cell_w * 7
    h = oy + 28 + cell_h * len(weeks) + 24
    img = Image.new("RGB", (w, h), "#f7faf7")
    draw = ImageDraw.Draw(img)
    font = _font(15)
    font_s = _font(13)
    font_b = _font(24)

    month_name = _MONTH_RU[month] if 1 <= month <= 12 else str(month)
    title = f"План · {month_name} {year}"
    tb = draw.textbbox((0, 0), title, font=font_b)
    draw.text(((w - (tb[2] - tb[0])) / 2, 18), title, fill="#1a3d2b", font=font_b)

    for i, hd in enumerate(_WEEKDAYS_RU):
        draw.text((ox + i * cell_w + 42, oy - 22), hd, fill="#3d5c4a", font=font)

    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            x, y = ox + di * cell_w, oy + 8 + wi * cell_h
            info = session_by_day.get(day) if day else None
            if info:
                bg = "#c8ebd4" if info.get("done") else "#e7f5ea"
            else:
                bg = "#ffffff" if day else "#eef2ee"
            draw.rounded_rectangle([x, y, x + cell_w - 6, y + cell_h - 8], radius=10, fill=bg, outline="#d5e0d8")
            if day == 0:
                continue
            mark = " ✓" if info and info.get("done") else ""
            draw.text((x + 10, y + 8), f"{day}{mark}", fill="#1a3d2b", font=font)
            if info:
                label = str(info.get("label") or "")
                for li, line in enumerate(_wrap(draw, label, font_s, cell_w - 22)):
                    draw.text((x + 10, y + 34 + li * 16), line, fill="#2f5d44", font=font_s)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def current_month_tonnage_map() -> Dict[int, float]:
    from app.sheets import get_sheet_rows_for_period

    rows = await get_sheet_rows_for_period("Сессии", 40, date_col=1)
    y, m = now_local().year, now_local().month
    out: Dict[int, float] = {}
    for r in rows:
        if len(r) < 16:
            continue
        ds = str(r[1])
        try:
            d = date.fromisoformat(ds[:10])
        except Exception:
            continue
        if d.year != y or d.month != m:
            continue
        try:
            t = float(str(r[15]).replace(",", "."))
        except Exception:
            t = 0.0
        out[d.day] = out.get(d.day, 0.0) + t
    return out
