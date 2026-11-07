import calendar
import io
from datetime import date
from typing import Dict

from PIL import Image, ImageDraw, ImageFont

from app.state import now_local


def _font(size: int):
    for name in ("DejaVuSans.ttf", "arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


async def month_png_bytes(year: int, month: int, session_by_day: Dict[int, float]) -> bytes:
    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdayscalendar(year, month)
    w, h = 900, 720
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    font = _font(16)
    font_b = _font(22)

    title = f"{month:02d}.{year} workouts"
    draw.text((20, 16), title, fill="black", font=font_b)
    headers = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cell_w, cell_h = 120, 90
    ox, oy = 20, 60
    for i, hd in enumerate(headers):
        draw.text((ox + i * cell_w + 35, oy), hd, fill="#333", font=font)

    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            x, y = ox + di * cell_w, oy + 30 + wi * cell_h
            draw.rectangle([x, y, x + cell_w - 4, y + cell_h - 4], outline="#ccc")
            if day == 0:
                continue
            tons = session_by_day.get(day, 0.0)
            mark = "[x]" if tons > 0 else "[ ]"
            fill = "#0a7" if tons > 0 else "black"
            draw.text((x + 8, y + 6), f"{day} {mark}", fill=fill, font=font)
            draw.text((x + 8, y + 36), f"{tons:.0f} kg", fill="#555", font=font)

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
