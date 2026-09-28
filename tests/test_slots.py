"""Statistic periods → half-hour slots: all the maths the backend no longer does."""

from datetime import UTC, datetime, timedelta

from custom_components.halfhour.slots import Period, build_slots, slot_floor

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def five(i: int, mean=None, total=None) -> Period:
    return Period(start=T0 + timedelta(minutes=5 * i), seconds=300, mean=mean, sum=total)


def test_slot_floor():
    assert slot_floor(T0 + timedelta(minutes=47, seconds=3)) == T0 + timedelta(minutes=30)


def test_power_mean_is_coverage_weighted_and_partial_slots_say_so():
    periods = [five(i, mean=400.0) for i in range(6)] + [five(6, mean=100.0), five(7, mean=300.0)]
    out = build_slots("house_load_w", "power", periods, invert=False, prev_sum=None)
    assert out == [
        {"slot": "2026-09-28T10:00:00+00:00", "role": "house_load_w", "value": 400.0, "coverage_s": 1800, "resolution_s": 300},
        {"slot": "2026-09-28T10:30:00+00:00", "role": "house_load_w", "value": 200.0, "coverage_s": 600, "resolution_s": 300},
    ]


def test_energy_delta_becomes_average_watts():
    # 0.05 kWh in each 5 minutes = 600 W.
    periods = [five(i, total=10.0 + 0.05 * (i + 1)) for i in range(6)]
    out = build_slots("house_load_w", "energy", periods, invert=False, prev_sum=10.0)
    assert out[0]["value"] == 600.0 and out[0]["coverage_s"] == 1800


def test_energy_without_a_previous_sum_skips_the_first_period():
    periods = [five(i, total=10.0 + 0.05 * i) for i in range(6)]
    out = build_slots("house_load_w", "energy", periods, invert=False, prev_sum=None)
    assert out[0]["coverage_s"] == 1500 and out[0]["value"] == 600.0


def test_counter_reset_drops_that_period_not_a_spike():
    totals = [10.05, 10.10, 0.05, 0.10, 0.15, 0.20]  # meter replaced after period 1
    periods = [five(i, total=v) for i, v in enumerate(totals)]
    out = build_slots("house_load_w", "energy", periods, invert=False, prev_sum=10.0)
    assert out[0]["coverage_s"] == 1500 and out[0]["value"] == 600.0


def test_gaps_are_missing_slots_never_zero():
    periods = [five(i, mean=400.0) for i in range(6)] + [Period(T0 + timedelta(hours=2), 300, 500.0, None)]
    out = build_slots("house_load_w", "power", periods, invert=False, prev_sum=None)
    assert [s["slot"] for s in out] == ["2026-09-28T10:00:00+00:00", "2026-09-28T12:00:00+00:00"]


def test_none_means_are_skipped():
    out = build_slots("house_load_w", "power", [five(0, mean=None), five(1, mean=300.0)], invert=False, prev_sum=None)
    assert out[0]["coverage_s"] == 300


def test_hourly_period_fills_both_halves_as_coarse():
    out = build_slots("solar_w", "power", [Period(T0, 3600, 900.0, None)], invert=False, prev_sum=None)
    assert [(s["slot"][11:16], s["value"], s["coverage_s"], s["resolution_s"]) for s in out] == [("10:00", 900.0, 1800, 3600), ("10:30", 900.0, 1800, 3600)]


def test_hourly_energy():
    out = build_slots("house_load_w", "energy", [Period(T0, 3600, None, 101.2)], invert=False, prev_sum=100.0)
    assert out[0]["value"] == 1200.0 and out[1]["value"] == 1200.0


def test_invert_flips_power_and_energy_but_percent_ignores_it():
    assert build_slots("battery_power_w", "power", [five(0, mean=-335.0)], invert=True, prev_sum=None)[0]["value"] == 335.0
    assert build_slots("battery_soc_pct", "percent", [five(0, mean=81.0)], invert=True, prev_sum=None)[0]["value"] == 81.0


def test_energy_after_a_gap_is_dropped_not_a_spike():
    # Six contiguous 5-minute periods at 0.05 kWh each (600 W) from 10:00 with prev_sum=10.0
    periods = [five(i, total=10.0 + 0.05 * (i + 1)) for i in range(6)]
    # Then a 2-hour gap (12:30 and 12:35)
    # Counter rose 2.0 kWh during the gap: sum at 12:30 = last + 2.0 + 0.05, at 12:35 = that + 0.05
    gap_periods = [
        Period(T0 + timedelta(hours=2, minutes=30), 300, None, 10.0 + 0.05 * 6 + 2.0 + 0.05),  # 12:30
        Period(T0 + timedelta(hours=2, minutes=35), 300, None, 10.0 + 0.05 * 6 + 2.0 + 0.10),  # 12:35
    ]
    out = build_slots("house_load_w", "energy", periods + gap_periods, invert=False, prev_sum=10.0)
    # Expect: 10:00 slot 600 W coverage 1800; 12:30 slot value 600.0 with coverage_s 300 (the 12:30 period dropped, 12:35 kept); no 11:xx slots
    assert out[0]["slot"] == "2026-09-28T10:00:00+00:00" and out[0]["value"] == 600.0 and out[0]["coverage_s"] == 1800
    assert out[1]["slot"] == "2026-09-28T12:30:00+00:00" and out[1]["value"] == 600.0 and out[1]["coverage_s"] == 300
    assert len(out) == 2


def test_hourly_period_misaligned_floors_to_slot_boundary():
    # Hourly period starting at 10:00:30 should emit slots at 10:00 and 10:30, not 10:00:30 and 10:30:30
    out = build_slots("solar_w", "power", [Period(T0 + timedelta(seconds=30), 3600, 900.0, None)], invert=False, prev_sum=None)
    assert [s["slot"][11:16] for s in out] == ["10:00", "10:30"]


def test_energy_row_without_a_sum_breaks_the_chain_not_a_double_spike():
    # 600 W throughout; the recorder lost period 2's sum. Period 3's delta
    # from period 1 spans two periods: it must be dropped, not read as 1200 W.
    totals = [10.05, 10.10, None, 10.20, 10.25, 10.30]
    periods = [five(i, total=v) for i, v in enumerate(totals)]
    out = build_slots("house_load_w", "energy", periods, invert=False, prev_sum=10.0)
    assert out == [{"slot": "2026-09-28T10:00:00+00:00", "role": "house_load_w", "value": 600.0, "coverage_s": 1200, "resolution_s": 300}]


def test_energy_first_row_without_a_sum_does_not_reuse_the_earlier_baseline():
    totals = [None, 10.10, 10.15]
    periods = [five(i, total=v) for i, v in enumerate(totals)]
    out = build_slots("house_load_w", "energy", periods, invert=False, prev_sum=10.0)
    assert out[0]["value"] == 600.0 and out[0]["coverage_s"] == 300
