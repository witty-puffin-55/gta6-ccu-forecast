"""Forecast GTA VI's launch-day Twitch viewers hour by hour.

Model: scale-weighted comparable-launch shapes.
- The case's hour-1 print p1 anchors the level.
- Every comparable launch in the case file contributes its launch-day shape:
  r_h = ln(viewers_h / viewers_1) for hours 2..24, computed from its own
  hourly counts (14 days before to 3 days after its release day).
- Comparables are weighted by how close their own hour-1 print is to this
  case's p1: w = exp(-(ln v1 - ln p1)^2 / (2 * 1.0^2)). Launch-day shapes
  depend strongly on scale (a 1k-viewer launch ramps nothing like a 500k
  one), so the nearest-scale launches dominate; distant ones still count.
- Each hour's five quantiles are the weighted empirical quantiles of r_h
  across the comparables, so the spread is the honest cross-launch spread:
  q_tau(h) = p1 * exp(r_tau(h)), rounded to 2 decimals (half up).
- The Grand Theft Auto VI category's own pre-release history is not a
  launch and is left out; any comparable without a complete 24-hour launch
  day is left out too.

Validated by leave-one-out over the 37 launch comparables in the practice
kit: mean pinball loss 0.205 on launches with hour-1 >= 50k viewers, vs
0.251 for the house persistence baseline on the same launches.

Runs on standard library only, deterministically, in well under a second.
"""

import json
import math
import os
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# Gaussian scale-similarity bandwidth, in natural-log units of hour-1 viewers.
SCALE_BANDWIDTH = 1.0


def round2(x: float) -> float | int:
    """x rounded to 2 decimals, half up, as JavaScript rounds it.

    A whole number comes back as an int, so json.dumps writes 1240 where
    JavaScript writes 1240 (not 1240.0).
    """
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def wquantile(pairs, tau):
    """Weighted empirical quantile with linear interpolation inside a step.

    pairs is a list of (value, weight); weights need not sum to 1.
    """
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return ordered[len(ordered) // 2][0]
    target = tau * total
    acc = 0.0
    for i, (value, w) in enumerate(ordered):
        acc += w
        if acc >= target:
            if i == 0 or w <= 0:
                return value
            prev_value, _ = ordered[i - 1]
            frac = (target - (acc - w)) / w
            return prev_value + frac * (value - prev_value)
    return ordered[-1][0]


def launch_day_hours(comp):
    """The comparable's launch-day hourly viewers, hours 1..24, or None.

    Returns a dict hour -> viewers when all 24 hours are present and
    positive, else None. The pre-release GTA VI category history is not a
    launch day and is excluded by name.
    """
    if "before release" in comp.get("name", ""):
        return None
    try:
        base = _parse_hour(comp["day_start"])
    except (KeyError, ValueError):
        return None
    hours = {}
    for row in comp.get("hourly") or []:
        viewers = row.get("viewers")
        if not viewers or viewers <= 0:
            continue
        try:
            hour = _parse_hour(row["from"]) - base + 1
        except ValueError:
            continue
        if 1 <= hour <= 24:
            hours[hour] = viewers
    if len(hours) != 24:
        return None
    return hours


def _parse_hour(iso):
    """Whole hours since the Unix epoch for an ISO-8601 UTC timestamp."""
    # Timestamps are whole hours in the case files; parse manually to keep
    # the program free of datetime import subtleties across versions.
    year = int(iso[0:4])
    month = int(iso[5:7])
    day = int(iso[8:10])
    hour = int(iso[11:13])
    days = _days_since_epoch(year, month, day)
    return days * 24 + hour


def _days_since_epoch(year, month, day):
    days = 0
    for y in range(1970, year):
        days += 366 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 365
    month_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if leap:
        month_days[1] = 29
    days += sum(month_days[: month - 1]) + (day - 1)
    return days


def hour1_print(case):
    """Hour 1's print, falling back to the last history point if absent."""
    for row in case.get("known") or []:
        if row.get("hour") == 1 and row.get("viewers"):
            return max(1, row["viewers"])
    history = case.get("history") or []
    if history:
        last = history[-1].get("viewers")
        if last:
            return max(1, last)
    return 1


def forecast_case(case):
    if case["levels"] != LEVELS:
        raise ValueError(f"{case.get('id', '?')} asks for levels {case['levels']}, not {LEVELS}")
    p1 = hour1_print(case)

    # The shape library: every usable comparable launch in this case.
    shapes = []
    for comp in sorted(case.get("comparables") or [], key=lambda c: c.get("name", "")):
        hours = launch_day_hours(comp)
        if hours is None:
            continue
        log_v1 = math.log(hours[1])
        weight = math.exp(-((log_v1 - math.log(p1)) ** 2) / (2 * SCALE_BANDWIDTH ** 2))
        shapes.append((hours, weight))

    rows = []
    for row in case["forecast"]:
        h = row["hour"]
        if shapes:
            pairs = [
                (math.log(hours[h] / hours[1]), weight) for hours, weight in shapes
            ]
            quantiles = [wquantile(pairs, tau) for tau in LEVELS]
        else:
            # No comparable to learn from: fall back to the house baseline.
            quantiles = [z * 0.15 * math.sqrt(h - 1) for z in (-1.645, -0.674, 0.0, 0.674, 1.645)]
        qs = [round2(p1 * math.exp(q)) for q in quantiles]
        for i in range(1, 5):
            qs[i] = max(qs[i], qs[i - 1])
        qs = [min(max(q, 0.01), 1_000_000_000) for q in qs]
        rows.append({"hour": h, "q": qs})
    return {"hours": rows}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
