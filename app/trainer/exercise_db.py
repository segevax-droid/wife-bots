import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from app.sheets import get_worksheet, get_ws_values

_SHEET_CANDIDATES = ("Список упражнений", "Упражнения")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _col_map(header: List[str]) -> Dict[str, int]:
    aliases = {
        "id": ("id", "ид", "код"),
        "name": ("название", "name", "упражнение", "упр"),
        "synonyms": ("синонимы", "synonyms", "синоним", "алиасы"),
        "group": ("группа", "group", "мышцы", "мышечная группа"),
        "equipment": ("оборудование", "equipment", "инвентарь"),
        "type": ("тип", "type"),
        "technique": ("техника", "technique", "описание"),
        "tip1": ("совет1", "tip1", "совет 1"),
        "tip2": ("совет2", "tip2", "совет 2"),
        "tip3": ("совет3", "tip3", "совет 3"),
        "tip4": ("совет4", "tip4", "совет 4"),
    }
    out: Dict[str, int] = {}
    low = [_norm(h) for h in header]
    for key, names in aliases.items():
        for i, h in enumerate(low):
            if h in names:
                out[key] = i
                break
    return out


class ExerciseDB:
    def __init__(self, rows: List[List[str]], header: Optional[List[str]] = None):
        self.rows = rows
        self.by_id: Dict[str, Dict[str, Any]] = {}
        cmap = _col_map(header or []) if header else {}
        for i, r in enumerate(rows):
            if not r:
                continue
            if cmap:
                eid = str(r[cmap["id"]]).strip() if "id" in cmap and cmap["id"] < len(r) else f"ex{i+1:04d}"
                name = str(r[cmap["name"]]).strip() if "name" in cmap and cmap["name"] < len(r) else ""
                if not name and len(r) > 1:
                    name = str(r[1]).strip()
                if not eid:
                    eid = f"ex{i+1:04d}"
                syn = str(r[cmap["synonyms"]]).strip() if "synonyms" in cmap and cmap["synonyms"] < len(r) else ""
                group = str(r[cmap["group"]]).strip() if "group" in cmap and cmap["group"] < len(r) else ""
                equip = str(r[cmap["equipment"]]).strip() if "equipment" in cmap and cmap["equipment"] < len(r) else ""
                typ = str(r[cmap["type"]]).strip() if "type" in cmap and cmap["type"] < len(r) else ""
                tech = str(r[cmap["technique"]]).strip() if "technique" in cmap and cmap["technique"] < len(r) else ""
                tips = []
                for tk in ("tip1", "tip2", "tip3", "tip4"):
                    if tk in cmap and cmap[tk] < len(r) and str(r[cmap[tk]]).strip():
                        tips.append(str(r[cmap[tk]]).strip())
            else:
                if len(r) < 2:
                    continue
                eid = str(r[0]).strip() or f"ex{i+1:04d}"
                name = str(r[1]).strip()
                syn = str(r[2]).strip() if len(r) > 2 else ""
                group = str(r[3]).strip() if len(r) > 3 else ""
                equip = str(r[4]).strip() if len(r) > 4 else ""
                typ = str(r[5]).strip() if len(r) > 5 else ""
                tech = str(r[6]).strip() if len(r) > 6 else ""
                tips = [str(r[j]).strip() for j in range(7, 11) if j < len(r) and str(r[j]).strip()]
            if not name:
                continue
            self.by_id[eid] = {
                "id": eid,
                "name": name,
                "synonyms": syn,
                "group": group,
                "equipment": equip,
                "type": typ,
                "technique": tech,
                "tips": tips,
            }

    def match(self, name: str) -> Optional[Dict[str, Any]]:
        n = _norm(name)
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for ex in self.by_id.values():
            candidates = [ex["name"]] + [p.strip() for p in ex["synonyms"].split(",") if p.strip()]
            for c in candidates:
                ratio = SequenceMatcher(None, n, _norm(c)).ratio()
                if best is None or ratio > best[0]:
                    best = (ratio, ex)
        if best and best[0] >= 0.45:
            return best[1]
        return None

    def top_similar(self, name: str, n: int = 5) -> List[Dict[str, Any]]:
        scored: List[Tuple[float, Dict[str, Any]]] = []
        q = _norm(name)
        for ex in self.by_id.values():
            candidates = [ex["name"]] + [p.strip() for p in ex["synonyms"].split(",") if p.strip()]
            rmax = max(SequenceMatcher(None, q, _norm(c)).ratio() for c in candidates) if candidates else 0
            scored.append((rmax, ex))
        scored.sort(key=lambda x: -x[0])
        return [x[1] for x in scored[:n]]

    def analogues(self, ex: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        g, eq, t = ex.get("group"), ex.get("equipment"), ex.get("type")
        pool: List[Dict[str, Any]] = []
        for other in self.by_id.values():
            if other["id"] == ex["id"]:
                continue
            if g and other.get("group") == g:
                pool.append(other)
        if len(pool) < 6 and eq:
            for other in self.by_id.values():
                if other["id"] == ex["id"] or other in pool:
                    continue
                if other.get("equipment") == eq:
                    pool.append(other)
        if len(pool) < 6 and t:
            for other in self.by_id.values():
                if other["id"] == ex["id"] or other in pool:
                    continue
                if other.get("type") == t:
                    pool.append(other)
        return pool[: max(limit, 30)]

    @staticmethod
    def is_bodyweight(ex: Optional[Dict[str, Any]]) -> bool:
        if not ex:
            return False
        name = (ex.get("name") or "").lower()
        equip = (ex.get("equipment") or "").lower()
        if any(
            p in name
            for p in (
                "без веса",
                "собственн",
                "своим весом",
                "свой вес",
            )
        ):
            return True
        if any(p in equip for p in ("свой вес", "без веса", "bodyweight")):
            return True
        return False

    def real_named(self) -> List[Dict[str, Any]]:
        """Prefer exercises that look like real names, not 'Группа — упражнение N'."""
        out = []
        for ex in self.by_id.values():
            if re.search(r"упражнение\s+\d+", ex["name"], re.I):
                continue
            out.append(ex)
        return out or list(self.by_id.values())

    @classmethod
    async def load(cls) -> "ExerciseDB":
        best: Optional[ExerciseDB] = None
        for title in _SHEET_CANDIDATES:
            ws = await get_worksheet(title)
            if not ws:
                continue
            rows = await get_ws_values(ws)
            if len(rows) < 2:
                continue
            header = [str(c) for c in rows[0]]
            data = [[str(c) for c in row] for row in rows[1:]]
            db = cls(data, header=header)
            # Prefer sheet with more real names
            if best is None or len(db.real_named()) > len(best.real_named()):
                best = db
            if title == "Список упражнений" and len(db.by_id) > 0:
                if len(db.real_named()) >= max(5, len(db.by_id) // 3):
                    return db
        return best or cls([])
