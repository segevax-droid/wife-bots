"""Грубая оценка ккал сессии: MET × вес_кг × часы; коэффициенты по умолчанию без пульсометра."""


def estimate_session_kcal(
    body_weight_kg: float,
    duration_min: float,
    total_sets: int,
    pain: bool,
) -> int:
    # MET ~5 для силовой смеси (усреднённо); коррекция по объёму сетов
    met = 5.0 + min(total_sets, 40) * 0.02
    if pain:
        met *= 0.9
    hours = max(duration_min, 10) / 60.0
    kcal = met * max(body_weight_kg, 40) * hours
    return int(round(kcal))
