import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from app.sheets import get_worksheet, get_ws_values


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


class ExerciseDB:
    def __init__(self, rows: List[List[str]]):
        self.rows = rows
        self.by_id: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if len(r) < 2:
                continue
            eid = str(r[0]).strip()
            if not eid:
                continue
            self.by_id[eid] = {
                "id": eid,
                "name": str(r[1]).strip(),
                "synonyms": str(r[2]).strip() if len(r) > 2 else "",
                "group": str(r[3]).strip() if len(r) > 3 else "",
                "equipment": str(r[4]).strip() if len(r) > 4 else "",
                "type": str(r[5]).strip() if len(r) > 5 else "",
                "technique": str(r[6]).strip() if len(r) > 6 else "",
                "tips": [str(r[i]).strip() for i in range(7, 11) if i < len(r) and str(r[i]).strip()],
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
        return pool[: max(20, limit * 6)]

    @classmethod
    async def load(cls) -> "ExerciseDB":
        ws = await get_worksheet("Упражнения")
        rows = await get_ws_values(ws)
        data = rows[1:] if len(rows) > 1 else []
        return cls([[str(c) for c in row] for row in data])
