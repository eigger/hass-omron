"""Who a measurement belongs to: the weight-based and rule-based strategies (#301)."""

from __future__ import annotations

import pytest

from custom_components.omron import assignment as a
from custom_components.omron.assignment import (
    Assignment,
    RuleUser,
    WeightUser,
    assign_by_rules,
    assign_by_weight,
)
from custom_components.omron.const import (
    WEIGHT_FALLBACK_MAX_KG,
    WEIGHT_HISTORY_SIZE,
    WEIGHT_MIN_GAP_KG,
    WEIGHT_TOLERANCE_MAX_KG,
    WEIGHT_TOLERANCE_MIN_KG,
    WEIGHT_TOLERANCE_PCT_DEFAULT,
)

PCT = WEIGHT_TOLERANCE_PCT_DEFAULT


def _u(user_id: str, kg: float, *recent: float) -> WeightUser:
    return WeightUser(user_id, user_id.upper(), kg, tuple(recent))


# --- the reference weight ----------------------------------------------------


def test_the_expected_weight_is_the_reference_until_something_is_assigned() -> None:
    assert a.reference_weight(_u("a", 70.0)) == 70.0


def test_a_first_assignment_replaces_the_expected_weight_entirely() -> None:
    assert a.reference_weight(_u("a", 70.0, 80.0)) == 80.0


def test_the_newest_assigned_weights_count_most() -> None:
    # Weights 1, 0.71, 0.5, 0.35, 0.25 for the newest back to the oldest.
    user = _u("a", 0.0, 60.0, 61.0, 62.0, 63.0, 64.0)
    weights = [1, 2**-0.5, 0.5, 2**-1.5, 0.25]
    expected = sum(w * kg for w, kg in zip(weights, [64, 63, 62, 61, 60])) / sum(
        weights
    )
    assert a.reference_weight(user) == pytest.approx(expected)
    # The newest is about a third of the reference, not all of it.
    assert 1 / sum(weights) == pytest.approx(0.356, abs=0.001)


def test_only_the_latest_five_assignments_are_used() -> None:
    long = _u("a", 0.0, 10.0, 60.0, 61.0, 62.0, 63.0, 64.0)
    short = _u("a", 0.0, 60.0, 61.0, 62.0, 63.0, 64.0)
    assert a.reference_weight(long) == a.reference_weight(short)
    assert a.remember_weight([1.0] * WEIGHT_HISTORY_SIZE, 2.0) == [
        *[1.0] * (WEIGHT_HISTORY_SIZE - 1),
        2.0,
    ]


@pytest.mark.parametrize(
    ("reference", "percent", "tolerance"),
    [
        (75.0, 3.0, 2.25),
        (20.0, 3.0, WEIGHT_TOLERANCE_MIN_KG),
        (150.0, 3.0, WEIGHT_TOLERANCE_MAX_KG),
        (75.0, 5.0, 3.75),
        (75.0, 1.0, WEIGHT_TOLERANCE_MIN_KG),
        (120.0, 5.0, WEIGHT_TOLERANCE_MAX_KG),
    ],
)
def test_the_tolerance_is_a_share_held_between_the_floor_and_the_ceiling(
    reference: float, percent: float, tolerance: float
) -> None:
    assert a.weight_tolerance(reference, percent) == pytest.approx(tolerance)


# --- weight based --------------------------------------------------------------


def test_no_users_matches_nobody() -> None:
    assert assign_by_weight(70.0, [], PCT).reason == a.REASON_NO_MATCH


def test_a_weight_inside_the_tolerance_is_matched() -> None:
    result = assign_by_weight(71.0, [_u("a", 70.0)], PCT)
    assert (result.user_id, result.kind, result.reason) == ("a", a.KIND_MATCHED, None)
    row = result.candidates[0]
    assert row["reference_kg"] == 70.0
    assert row["difference_kg"] == 1.0
    assert row["tolerance_kg"] == 2.1


def test_the_tolerance_edge_is_inside() -> None:
    assert assign_by_weight(72.1, [_u("a", 70.0)], PCT).kind == a.KIND_MATCHED


def test_a_slow_drift_outside_the_tolerance_still_goes_to_the_nearest() -> None:
    # 3 kg off 70 kg: past the 2.1 kg tolerance, inside twice that.
    result = assign_by_weight(73.0, [_u("a", 70.0)], PCT)
    assert (result.user_id, result.kind) == ("a", a.KIND_NEAREST)


def test_past_twice_the_tolerance_is_out_of_range() -> None:
    result = assign_by_weight(75.0, [_u("a", 70.0)], PCT)
    assert (result.user_id, result.reason) == (None, a.REASON_OUT_OF_RANGE)
    assert result.candidates[0]["difference_kg"] == 5.0


def test_the_fallback_never_reaches_past_the_absolute_limit() -> None:
    # 100 kg: tolerance 3 kg, twice that is 6 kg, but 5 kg is the ceiling.
    assert assign_by_weight(105.0, [_u("a", 100.0)], PCT).kind == a.KIND_NEAREST
    over = assign_by_weight(100.0 + WEIGHT_FALLBACK_MAX_KG + 0.1, [_u("a", 100.0)], PCT)
    assert over.reason == a.REASON_OUT_OF_RANGE


def test_the_closest_of_two_wins_when_clearly_closer() -> None:
    users = [_u("a", 70.0), _u("b", 82.0)]
    result = assign_by_weight(70.5, users, PCT)
    assert (result.user_id, result.kind) == ("a", a.KIND_MATCHED)
    assert [c["user_id"] for c in result.candidates] == ["a", "b"]


def test_two_users_the_weight_sits_between_are_ambiguous() -> None:
    users = [_u("a", 70.0), _u("b", 73.0)]
    result = assign_by_weight(71.5, users, PCT)
    assert (result.user_id, result.reason) == (None, a.REASON_AMBIGUOUS)


def test_the_gap_to_the_runner_up_may_be_exactly_the_minimum() -> None:
    users = [_u("a", 70.0), _u("b", 72.0)]
    assert WEIGHT_MIN_GAP_KG == 1.0
    # 0.5 kg from a, 1.5 kg from b: exactly 1.0 kg closer.
    assert assign_by_weight(70.5, users, PCT).user_id == "a"
    # 0.6 and 1.4: 0.8 kg closer, too close to call.
    assert assign_by_weight(70.6, users, PCT).reason == a.REASON_AMBIGUOUS


def test_a_runner_up_outside_its_tolerance_still_counts_for_the_gap() -> None:
    users = [_u("a", 70.0), _u("b", 74.0)]
    # a is 2.2 kg away (tolerance 2.1), b 1.8 kg away (tolerance 2.22).
    result = assign_by_weight(72.2, users, PCT)
    assert (result.user_id, result.reason) == (None, a.REASON_AMBIGUOUS)


def test_nearest_needs_the_gap_too() -> None:
    users = [_u("a", 70.0), _u("b", 77.0)]
    # 73.4: a is 3.4 off (past 2.1, inside the fallback), b is 3.6 off.
    result = assign_by_weight(73.4, users, PCT)
    assert (result.user_id, result.reason) == (None, a.REASON_AMBIGUOUS)
    # 73.0: a is 3.0 off, b 4.0 off: a clear nearest.
    assert assign_by_weight(73.0, users, PCT).kind == a.KIND_NEAREST


def test_a_guest_far_from_everyone_is_out_of_range() -> None:
    users = [_u("a", 70.0), _u("b", 55.0)]
    result = assign_by_weight(95.0, users, PCT)
    assert (result.user_id, result.reason) == (None, a.REASON_OUT_OF_RANGE)
    assert {c["user_id"] for c in result.candidates} == {"a", "b"}


def test_a_larger_percentage_widens_the_tolerance() -> None:
    assert assign_by_weight(72.5, [_u("a", 70.0)], 3.0).kind == a.KIND_NEAREST
    assert assign_by_weight(72.5, [_u("a", 70.0)], 5.0).kind == a.KIND_MATCHED


def test_the_assigned_history_moves_the_reference() -> None:
    # Expected 70, but this user has been weighing about 80.
    users = [_u("a", 70.0, 80.0, 80.0), _u("b", 55.0)]
    assert assign_by_weight(80.5, users, PCT).user_id == "a"


# --- time windows -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "windows"),
    [
        ("06:00-09:00", [(360, 540)]),
        ("06:00-09:00, 18:30-21:00", [(360, 540), (1110, 1260)]),
        (" 6:00 - 9:00 ;22:00-02:00", [(360, 540), (1320, 120)]),
        ("00:00-24:00", [(0, 1440)]),
        ("", []),
        (None, []),
    ],
)
def test_time_windows_are_parsed(text, windows) -> None:
    assert a.parse_windows(text) == windows


@pytest.mark.parametrize(
    "text",
    ["06:00", "06:00-", "25:00-26:00", "06:60-07:00", "ab-cd", "09:00-09:00"],
)
def test_malformed_time_windows_are_rejected(text) -> None:
    assert a.parse_windows(text) is None


def test_windows_round_trip_to_text() -> None:
    assert a.format_windows([(360, 540), (1320, 120)]) == "06:00-09:00, 22:00-02:00"


def test_a_window_includes_its_start_and_excludes_its_end() -> None:
    window = [(360, 540)]
    assert a.in_windows(360, window)
    assert a.in_windows(539, window)
    assert not a.in_windows(540, window)
    assert not a.in_windows(359, window)


def test_a_window_may_cross_midnight() -> None:
    window = [(22 * 60, 2 * 60)]
    assert a.in_windows(23 * 60, window)
    assert a.in_windows(0, window)
    assert a.in_windows(119, window)
    assert not a.in_windows(120, window)
    assert not a.in_windows(12 * 60, window)


def test_any_one_window_is_enough() -> None:
    assert a.in_windows(19 * 60, [(360, 540), (18 * 60, 21 * 60)])


def test_overlapping_windows_are_found() -> None:
    assert a.windows_overlap([(360, 540)], [(500, 600)])
    assert a.windows_overlap([(22 * 60, 2 * 60)], [(60, 180)])
    # Touching is not overlapping: the end is excluded.
    assert not a.windows_overlap([(360, 540)], [(540, 600)])
    assert not a.windows_overlap([], [(540, 600)])


def test_the_minute_comes_from_the_devices_wall_clock() -> None:
    assert a.minute_of_day("2026-09-20T07:30:15") == 450
    assert a.minute_of_day("2026-09-20T07:30:15+09:00") == 450
    assert a.minute_of_day(None) is None
    assert a.minute_of_day("not a time") is None


# --- rule based -------------------------------------------------------------------


def _rule(user_id: str, windows="", **ranges) -> RuleUser:
    return RuleUser(
        user_id,
        user_id.upper(),
        tuple(a.parse_windows(windows) or ()),
        {key: bounds for key, bounds in ranges.items()},
    )


def _bp(time: str | None, sys: int = 120, dia: int = 80) -> dict:
    return {"device_time": time, "sys": sys, "dia": dia, "bpm": 60}


MORNING = "2026-09-20T07:30:00"
EVENING = "2026-09-20T19:30:00"


def test_the_one_user_whose_window_holds_gets_it() -> None:
    users = [_rule("a", "06:00-09:00"), _rule("b", "18:00-21:00")]
    result = assign_by_rules(_bp(MORNING), users)
    assert (result.user_id, result.kind) == ("a", a.KIND_RULE)
    assert assign_by_rules(_bp(EVENING), users).user_id == "b"


def test_no_rule_holding_is_no_match() -> None:
    users = [_rule("a", "06:00-09:00"), _rule("b", "18:00-21:00")]
    result = assign_by_rules(_bp("2026-09-20T13:00:00"), users)
    assert (result.user_id, result.reason) == (None, a.REASON_NO_MATCH)


def test_two_users_holding_is_ambiguous() -> None:
    users = [_rule("a", "06:00-09:00"), _rule("b", "07:00-10:00")]
    result = assign_by_rules(_bp(MORNING), users)
    assert (result.user_id, result.reason) == (None, a.REASON_AMBIGUOUS)


def test_the_time_and_the_values_must_both_hold() -> None:
    users = [
        _rule("a", "06:00-09:00", sys=(130, None)),
        _rule("b", "06:00-09:00", sys=(None, 129)),
    ]
    assert assign_by_rules(_bp(MORNING, sys=120), users).user_id == "b"
    assert assign_by_rules(_bp(MORNING, sys=140), users).user_id == "a"
    # In the time window of both, but a's value rule fails and so does b's.
    users = [
        _rule("a", "06:00-09:00", sys=(130, 150)),
        _rule("b", "06:00-09:00", sys=(100, 110)),
    ]
    assert assign_by_rules(_bp(MORNING, sys=120), users).reason == a.REASON_NO_MATCH


def test_value_ranges_include_both_ends() -> None:
    users = [_rule("a", sys=(110, 120), dia=(70, 80))]
    assert assign_by_rules(_bp(None, 110, 70), users).user_id == "a"
    assert assign_by_rules(_bp(None, 120, 80), users).user_id == "a"
    assert assign_by_rules(_bp(None, 121, 80), users).reason == a.REASON_NO_MATCH


def test_a_user_without_time_windows_is_judged_on_values_without_a_time() -> None:
    users = [_rule("a", sys=(110, 130)), _rule("b", sys=(140, 170))]
    result = assign_by_rules(_bp(None, sys=120), users)
    assert (result.user_id, result.kind) == ("a", a.KIND_RULE)


def test_a_time_window_with_no_time_cannot_be_decided() -> None:
    users = [_rule("a", "06:00-09:00"), _rule("b", "18:00-21:00")]
    result = assign_by_rules(_bp(None), users)
    assert (result.user_id, result.reason) == (None, a.REASON_NO_TIME)
    assert result.candidates[0]["unknown"] == ["time"]


def test_a_failed_value_rule_decides_even_without_a_time() -> None:
    # a's value rule fails, so its missing time no longer matters.
    users = [_rule("a", "06:00-09:00", sys=(150, 170)), _rule("b", sys=(100, 130))]
    assert assign_by_rules(_bp(None, sys=120), users).user_id == "b"


def test_an_undecidable_user_blocks_an_otherwise_certain_match() -> None:
    # b would match on values; a might match too once its time were known.
    users = [_rule("a", "06:00-09:00"), _rule("b", sys=(100, 130))]
    assert assign_by_rules(_bp(None, sys=120), users).reason == a.REASON_NO_TIME


def test_a_user_with_no_rules_matches_everything() -> None:
    assert assign_by_rules(_bp(None), [_rule("a")]).user_id == "a"
    result = assign_by_rules(_bp(MORNING), [_rule("a"), _rule("b", "06:00-09:00")])
    assert result.reason == a.REASON_AMBIGUOUS


def test_a_single_user_still_has_to_satisfy_the_rules() -> None:
    result = assign_by_rules(_bp(EVENING), [_rule("a", "06:00-09:00")])
    assert (result.user_id, result.reason) == (None, a.REASON_NO_MATCH)


def test_a_missing_value_fails_a_rule_on_it() -> None:
    record = {"device_time": MORNING, "sys": None, "dia": 80}
    assert assign_by_rules(record, [_rule("a", sys=(100, 130))]).reason == (
        a.REASON_NO_MATCH
    )


def test_the_candidates_name_the_rules_passed_and_failed() -> None:
    users = [_rule("a", "06:00-09:00", sys=(130, 150)), _rule("b", "18:00-21:00")]
    result = assign_by_rules(_bp(MORNING, sys=120), users)
    first, second = result.candidates
    assert first["matched"] is False
    assert first["passed"] == ["time"] and first["failed"] == ["sys"]
    assert second["failed"] == ["time"]


def test_scale_weight_ranges_work_the_same_way() -> None:
    users = [_rule("a", weight=(40, 60)), _rule("b", weight=(61, 100))]
    assert assign_by_rules({"device_time": None, "weight": 72.5}, users).user_id == "b"


def test_an_assignment_knows_whether_it_is_assigned() -> None:
    assert Assignment("a", a.KIND_RULE).assigned
    assert not Assignment(None, reason=a.REASON_NO_MATCH).assigned
