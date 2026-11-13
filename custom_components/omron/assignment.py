"""Who a measurement belongs to, for devices that cannot tell people apart (#301).

Pure functions, no Home Assistant imports and no clock: a stored record is read
long after it was taken, so the time of the poll says nothing about it. The
strategies are interchangeable; a device uses one of them for all its users.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from typing import Any

from .const import (
    WEIGHT_FALLBACK_MAX_KG,
    WEIGHT_FALLBACK_TOLERANCE_FACTOR,
    WEIGHT_HALF_LIFE,
    WEIGHT_HISTORY_SIZE,
    WEIGHT_MIN_GAP_KG,
    WEIGHT_TOLERANCE_MAX_KG,
    WEIGHT_TOLERANCE_MIN_KG,
)

# How an assigned measurement was matched.
KIND_MATCHED = "matched"
KIND_NEAREST = "nearest"
KIND_RULE = "rule"

# Why one was not assigned.
REASON_AMBIGUOUS = "ambiguous"
REASON_NO_MATCH = "no_match"
REASON_NO_TIME = "no_time"
REASON_OUT_OF_RANGE = "out_of_range"

_MINUTES_PER_DAY = 24 * 60

Window = tuple[int, int]
"""``(start, end)`` in minutes after midnight. Start is in, end is out; a start
after the end is a window that crosses midnight."""

ValueRange = tuple[float | None, float | None]


@dataclass(frozen=True)
class Assignment:
    """The outcome for one measurement."""

    user_id: str | None
    kind: str | None = None
    reason: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def assigned(self) -> bool:
        return self.user_id is not None


@dataclass(frozen=True)
class WeightUser:
    """A user as the weight-based strategy sees it."""

    user_id: str
    name: str
    expected_kg: float
    # Weights assigned to this user so far, oldest first.
    recent_kg: tuple[float, ...] = ()


@dataclass(frozen=True)
class RuleUser:
    """A user as the rule-based strategy sees it."""

    user_id: str
    name: str
    windows: tuple[Window, ...] = ()
    # Inclusive range per measured field ("weight", "sys", "dia").
    ranges: dict[str, ValueRange] = field(default_factory=dict)

    @property
    def has_rules(self) -> bool:
        return bool(self.windows) or any(
            lo is not None or hi is not None for lo, hi in self.ranges.values()
        )


# --- weight based ------------------------------------------------------------


def reference_weight(user: WeightUser) -> float:
    """Decaying average of the latest assigned weights; the expected weight if none.

    The newest weight counts 1, the one before ``2 ** (-1 / half-life)`` of that,
    and so on, over the latest ``WEIGHT_HISTORY_SIZE`` assignments.
    """
    recent = user.recent_kg[-WEIGHT_HISTORY_SIZE:]
    if not recent:
        return user.expected_kg
    weights = [0.5 ** (age / WEIGHT_HALF_LIFE) for age in range(len(recent))]
    # recent is oldest first; the newest has age 0.
    total = sum(w * kg for w, kg in zip(weights, reversed(recent)))
    return total / sum(weights)


def weight_tolerance(reference_kg: float, percent: float) -> float:
    """A share of the reference weight, held between the floor and the ceiling."""
    return min(
        max(reference_kg * percent / 100.0, WEIGHT_TOLERANCE_MIN_KG),
        WEIGHT_TOLERANCE_MAX_KG,
    )


def remember_weight(recent_kg: list[float], weight_kg: float) -> list[float]:
    """The user's recent weights with a newly assigned one appended."""
    return [*recent_kg, weight_kg][-WEIGHT_HISTORY_SIZE:]


def assign_by_weight(
    weight_kg: float, users: list[WeightUser], tolerance_percent: float
) -> Assignment:
    """Give a weight to the user whose reference it is closest to, if clearly."""
    if not users:
        return Assignment(None, reason=REASON_NO_MATCH)
    rows = []
    for user in users:
        reference = reference_weight(user)
        tolerance = weight_tolerance(reference, tolerance_percent)
        rows.append(
            {
                "user_id": user.user_id,
                "name": user.name,
                "reference_kg": round(reference, 2),
                "tolerance_kg": round(tolerance, 2),
                # Rounded so a gap of exactly the minimum is not lost to float
                # noise (71.0 - 70.0 is not always 1.0).
                "difference_kg": round(abs(weight_kg - reference), 3),
            }
        )
    by_distance = sorted(rows, key=lambda r: r["difference_kg"])
    inside = [r for r in by_distance if r["difference_kg"] <= r["tolerance_kg"]]

    candidates = [dict(r) for r in by_distance]

    if inside:
        best = inside[0]
        kind = KIND_MATCHED
    else:
        best = by_distance[0]
        kind = KIND_NEAREST
        limit = min(
            best["tolerance_kg"] * WEIGHT_FALLBACK_TOLERANCE_FACTOR,
            WEIGHT_FALLBACK_MAX_KG,
        )
        if best["difference_kg"] > limit:
            return Assignment(None, reason=REASON_OUT_OF_RANGE, candidates=candidates)

    others = [r for r in by_distance if r is not best]
    if others and round(others[0]["difference_kg"] - best["difference_kg"], 3) < (
        WEIGHT_MIN_GAP_KG
    ):
        return Assignment(None, reason=REASON_AMBIGUOUS, candidates=candidates)
    return Assignment(best["user_id"], kind=kind, candidates=candidates)


# --- rules: time windows and value ranges ------------------------------------


def parse_windows(text: str | None) -> list[Window] | None:
    """``"06:00-09:00, 22:00-02:00"`` as windows; None when any part is malformed.

    Empty text is no windows (an empty list).
    """
    windows: list[Window] = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        start_text, sep, end_text = part.partition("-")
        if not sep:
            return None
        start = _parse_clock(start_text)
        end = _parse_clock(end_text)
        # An empty window (start == end) is more likely a typo than "all day".
        if start is None or end is None or start == end:
            return None
        windows.append((start, end))
    return windows


def format_windows(windows: list[Window] | tuple[Window, ...]) -> str:
    """Windows back to the text ``parse_windows`` reads."""
    return ", ".join(f"{_clock(start)}-{_clock(end)}" for start, end in windows)


def _parse_clock(text: str) -> int | None:
    hours, sep, minutes = text.strip().partition(":")
    if not sep or not hours.isdigit() or not minutes.isdigit():
        return None
    h, m = int(hours), int(minutes)
    if h == 24 and m == 0:
        return _MINUTES_PER_DAY
    if h > 23 or m > 59:
        return None
    return h * 60 + m


def _clock(minutes: int) -> str:
    minutes %= _MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _window_minutes(window: Window) -> set[int]:
    start, end = window
    start %= _MINUTES_PER_DAY
    end %= _MINUTES_PER_DAY
    if start < end:
        return set(range(start, end))
    return {*range(start, _MINUTES_PER_DAY), *range(0, end)}


def in_windows(minute: int, windows: tuple[Window, ...] | list[Window]) -> bool:
    """Whether a minute of the day is in any window (any one is enough)."""
    return any(minute in _window_minutes(window) for window in windows)


def windows_overlap(
    first: tuple[Window, ...] | list[Window], second: tuple[Window, ...] | list[Window]
) -> bool:
    """Whether two users' time windows share a minute."""
    covered: set[int] = set()
    for window in first:
        covered |= _window_minutes(window)
    return any(covered & _window_minutes(window) for window in second)


def minute_of_day(device_time: str | None) -> int | None:
    """Wall-clock minute of an ISO time string, or None when there is none."""
    if not device_time:
        return None
    try:
        moment = dt.datetime.fromisoformat(device_time)
    except (TypeError, ValueError):
        return None
    return moment.hour * 60 + moment.minute


def assign_by_rules(measurement: dict[str, Any], users: list[RuleUser]) -> Assignment:
    """Give a measurement to the one user whose rules all hold.

    ``measurement`` carries ``device_time`` (ISO, naive wall clock) and the
    values the ranges name. A user without time windows is judged on values
    alone, whether or not the record has a time.
    """
    minute = minute_of_day(measurement.get("device_time"))
    candidates: list[dict[str, Any]] = []
    matching: list[RuleUser] = []
    undecided = False
    for user in users:
        passed: list[str] = []
        failed: list[str] = []
        for key, (low, high) in user.ranges.items():
            if low is None and high is None:
                continue
            value = measurement.get(key)
            ok = (
                isinstance(value, (int, float))
                and (low is None or value >= low)
                and (high is None or value <= high)
            )
            (passed if ok else failed).append(key)
        unknown: list[str] = []
        if user.windows:
            if minute is None:
                unknown.append("time")
            else:
                (passed if in_windows(minute, user.windows) else failed).append("time")
        if failed:
            result: bool | None = False
        elif unknown:
            result = None
            undecided = True
        else:
            result = True
            matching.append(user)
        candidates.append(
            {
                "user_id": user.user_id,
                "name": user.name,
                "matched": result,
                "passed": passed,
                "failed": failed,
                "unknown": unknown,
            }
        )
    # A user whose time rule cannot be checked might match as well, so the
    # outcome is unknown whatever the others say.
    if undecided:
        return Assignment(None, reason=REASON_NO_TIME, candidates=candidates)
    if len(matching) == 1:
        return Assignment(matching[0].user_id, kind=KIND_RULE, candidates=candidates)
    return Assignment(
        None,
        reason=REASON_NO_MATCH if not matching else REASON_AMBIGUOUS,
        candidates=candidates,
    )
