"""Plan model: parsing, newest-wins, covering slot, staleness, Store round trip."""

import json

import pytest

from custom_components.halfhour.plan import (
    _PLAN_FIELDS,
    Plan,
    PlanError,
    PlanSlot,
    covering_slot,
    from_store,
    is_stale,
    newer,
    parse_plan,
    to_store,
)

VALID_PAYLOAD = {
    "v": 1,
    "id": "plan-1",
    "account": "acct-1",
    "made_at": "2026-09-28T12:00:00Z",
    "slot_minutes": 30,
    "slots": [
        {
            "start": "2026-09-28T12:00:00Z",
            "import_p": 24.5,
            "export_p": 12.0,
            "load_w": 400.0,
            "pv_w": 0.0,
            "battery_w": None,
            "grid_w": None,
            "soc": None,
        },
        {
            "start": "2026-09-28T12:30:00Z",
            "import_p": 20.0,
            "export_p": 10.0,
            "load_w": None,
            "pv_w": 100.0,
            "battery_w": -50.0,
            "grid_w": 350.0,
            "soc": 42.5,
        },
    ],
}


def _payload(**overrides):
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload.update(overrides)
    return payload


class TestParsePlan:
    def test_parses_valid_payload_with_null_fields(self):
        plan = parse_plan(json.dumps(VALID_PAYLOAD))
        assert plan.id == "plan-1"
        assert plan.account == "acct-1"
        assert plan.slot_minutes == 30
        assert len(plan.slots) == 2
        first, second = plan.slots
        assert first.load_w == 400.0
        assert first.battery_w is None
        assert first.grid_w is None
        assert first.soc is None
        assert second.load_w is None
        assert second.battery_w == -50.0
        assert second.soc == 42.5

    def test_accepts_bytes_payload(self):
        plan = parse_plan(json.dumps(VALID_PAYLOAD).encode("utf-8"))
        assert plan.id == "plan-1"

    def test_computes_slot_end_from_slot_minutes(self):
        plan = parse_plan(json.dumps(VALID_PAYLOAD))
        first = plan.slots[0]
        assert first.end == first.start.replace(minute=30)

    def test_bad_json_raises(self):
        with pytest.raises(PlanError):
            parse_plan("{not json")

    def test_non_object_json_raises(self):
        with pytest.raises(PlanError):
            parse_plan("[1, 2, 3]")

    def test_wrong_version_raises(self):
        with pytest.raises(PlanError):
            parse_plan(json.dumps(_payload(v=2)))

    def test_missing_top_level_field_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        del payload["account"]
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_missing_slot_field_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        del payload["slots"][0]["import_p"]
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_naive_made_at_raises(self):
        with pytest.raises(PlanError):
            parse_plan(json.dumps(_payload(made_at="2026-09-28T12:00:00")))

    def test_non_string_made_at_raises(self):
        with pytest.raises(PlanError):
            parse_plan(json.dumps(_payload(made_at=12345)))

    def test_malformed_made_at_raises(self):
        with pytest.raises(PlanError):
            parse_plan(json.dumps(_payload(made_at="not-a-timestamp")))

    def test_non_string_id_or_account_raises(self):
        with pytest.raises(PlanError):
            parse_plan(json.dumps(_payload(id=1)))

    def test_slots_not_a_list_raises(self):
        with pytest.raises(PlanError):
            parse_plan(json.dumps(_payload(slots={"start": "x"})))

    def test_slot_not_an_object_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["slots"][0] = "not-an-object"
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_non_numeric_slot_field_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["slots"][0]["import_p"] = "not-a-number"
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_bool_slot_field_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["slots"][0]["import_p"] = True
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_null_for_non_nullable_field_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["slots"][0]["pv_w"] = None
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_nan_required_field_raises(self):
        # json.dumps can't emit NaN by default in valid JSON, so build the
        # payload text directly the way a non-Python sender might.
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        text = json.dumps(payload).replace('"import_p": 24.5', '"import_p": NaN')
        with pytest.raises(PlanError):
            parse_plan(text)

    def test_infinity_nullable_field_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        text = json.dumps(payload).replace('"grid_w": 350.0', '"grid_w": Infinity')
        with pytest.raises(PlanError):
            parse_plan(text)

    def test_negative_infinity_field_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        text = json.dumps(payload).replace('"battery_w": -50.0', '"battery_w": -Infinity')
        with pytest.raises(PlanError):
            parse_plan(text)

    def test_overlapping_slots_raise(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        # Second slot starts before the first (30-minute) slot ends.
        payload["slots"][1]["start"] = "2026-09-28T12:15:00Z"
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_duplicate_slot_starts_raise(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["slots"][1]["start"] = payload["slots"][0]["start"]
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_unsorted_slots_raise(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["slots"] = list(reversed(payload["slots"]))
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    def test_contiguous_slots_with_a_gap_are_accepted(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        # A gap (start >= previous end, not ==) is allowed: only overlap,
        # duplication and unsorted order are rejected.
        payload["slots"][1]["start"] = "2026-09-28T13:00:00Z"
        plan = parse_plan(json.dumps(payload))
        assert len(plan.slots) == 2

    def test_naive_slot_start_raises(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["slots"][0]["start"] = "2026-09-28T12:00:00"
        with pytest.raises(PlanError):
            parse_plan(json.dumps(payload))

    @pytest.mark.parametrize("bad", [1, 2, 3, 7, 45, 90, 0, -30])
    def test_invalid_slot_minutes_raises(self, bad):
        with pytest.raises(PlanError):
            parse_plan(json.dumps(_payload(slot_minutes=bad)))

    @pytest.mark.parametrize("good", [5, 15, 30, 60])
    def test_valid_slot_minutes_accepted(self, good):
        # Single slot so varying slot_minutes can't make the fixture's two
        # fixed slot starts overlap.
        payload = _payload(slot_minutes=good)
        payload["slots"] = payload["slots"][:1]
        plan = parse_plan(json.dumps(payload))
        assert plan.slot_minutes == good


class TestNewer:
    def _plan(self, made_at: str) -> Plan:
        return parse_plan(json.dumps(_payload(made_at=made_at)))

    def test_none_current_is_always_newer(self):
        incoming = self._plan("2026-09-28T12:00:00Z")
        assert newer(None, incoming) is True

    def test_older_incoming_is_not_newer(self):
        current = self._plan("2026-09-28T12:00:00Z")
        incoming = self._plan("2026-09-28T11:00:00Z")
        assert newer(current, incoming) is False

    def test_equal_made_at_is_not_newer(self):
        current = self._plan("2026-09-28T12:00:00Z")
        incoming = self._plan("2026-09-28T12:00:00Z")
        assert newer(current, incoming) is False

    def test_newer_incoming_is_newer(self):
        current = self._plan("2026-09-28T12:00:00Z")
        incoming = self._plan("2026-09-28T13:00:00Z")
        assert newer(current, incoming) is True


class TestCoveringSlot:
    def setup_method(self):
        self.plan = parse_plan(json.dumps(VALID_PAYLOAD))
        self.first = self.plan.slots[0]
        self.second = self.plan.slots[1]

    def test_exactly_at_start_is_included(self):
        assert covering_slot(self.plan, self.first.start) == self.first

    def test_exactly_at_end_falls_into_next_slot(self):
        assert covering_slot(self.plan, self.first.end) == self.second

    def test_before_horizon_is_none(self):
        from datetime import timedelta

        before = self.first.start - timedelta(minutes=1)
        assert covering_slot(self.plan, before) is None

    def test_after_horizon_is_none(self):
        after = self.second.end
        assert covering_slot(self.plan, after) is None

    def test_mid_slot_is_included(self):
        mid = self.first.start.replace(minute=10)
        assert covering_slot(self.plan, mid) == self.first


class TestIsStale:
    def setup_method(self):
        self.plan = parse_plan(json.dumps(VALID_PAYLOAD))
        self.made_at = self.plan.made_at
        self.first = self.plan.slots[0]
        self.second = self.plan.slots[1]

    def test_none_plan_is_stale(self):
        assert is_stale(None, self.made_at, 3600) is True

    def test_fresh_plan_within_horizon_is_not_stale(self):
        now = self.first.start
        assert is_stale(self.plan, now, 3600) is False

    def test_too_old_is_stale(self):
        from datetime import timedelta

        # Still within the plan's horizon (first slot), but the plan itself
        # is older than stale_after_s, so it's the age check that fires.
        now = self.made_at + timedelta(seconds=901)
        assert is_stale(self.plan, now, 900) is True

    def test_boundary_at_exactly_stale_after_s_is_not_stale(self):
        from datetime import timedelta

        # Exactly stale_after_s old, and still covered by the second slot.
        now = self.made_at + timedelta(seconds=1800)
        assert is_stale(self.plan, now, 1800) is False

    def test_fresh_but_past_horizon_is_stale(self):
        now = self.second.end
        assert is_stale(self.plan, now, 3600) is True


class TestStoreRoundTrip:
    def test_round_trip(self):
        plan = parse_plan(json.dumps(VALID_PAYLOAD))
        stored = to_store(plan)
        restored = from_store(stored)
        assert restored == plan

    def test_store_is_json_serialisable(self):
        plan = parse_plan(json.dumps(VALID_PAYLOAD))
        stored = to_store(plan)
        json.dumps(stored)

    def test_store_shape_matches_wire_shape(self):
        plan = parse_plan(json.dumps(VALID_PAYLOAD))
        stored = to_store(plan)
        assert stored["v"] == 1
        assert set(stored) == set(_PLAN_FIELDS)


class TestFromStoreValidation:
    def _stored(self) -> dict:
        return to_store(parse_plan(json.dumps(VALID_PAYLOAD)))

    def test_missing_key_raises_plan_error(self):
        stored = self._stored()
        del stored["account"]
        with pytest.raises(PlanError):
            from_store(stored)

    def test_missing_slot_key_raises_plan_error(self):
        stored = self._stored()
        del stored["slots"][0]["import_p"]
        with pytest.raises(PlanError):
            from_store(stored)

    def test_wrong_type_raises_plan_error(self):
        stored = self._stored()
        stored["slot_minutes"] = "thirty"
        with pytest.raises(PlanError):
            from_store(stored)

    def test_naive_datetime_raises_plan_error(self):
        stored = self._stored()
        stored["made_at"] = "2026-09-28T12:00:00"
        with pytest.raises(PlanError):
            from_store(stored)

    def test_non_finite_number_raises_plan_error(self):
        stored = self._stored()
        stored["slots"][0]["import_p"] = float("nan")
        with pytest.raises(PlanError):
            from_store(stored)

    def test_wrong_version_raises_plan_error(self):
        stored = self._stored()
        stored["v"] = 2
        with pytest.raises(PlanError):
            from_store(stored)
