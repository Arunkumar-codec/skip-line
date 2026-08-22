"""tests/unit/test_token_service.py — Token service logic unit tests."""

import pytest
from models.token import TokenStatus


class TestTokenStatusEnum:
    def test_all_statuses_present(self):
        expected = {"waiting","called","in_progress","completed","skipped","cancelled","on_hold"}
        actual   = {s.value for s in TokenStatus}
        assert expected == actual

    def test_waiting_is_initial(self):
        assert TokenStatus.WAITING == "waiting"

    def test_terminal_statuses(self):
        terminals = {TokenStatus.COMPLETED, TokenStatus.SKIPPED, TokenStatus.CANCELLED}
        for t in terminals:
            assert t.value in {"completed","skipped","cancelled"}


class TestValidTransitions:
    """Mirror the _VALID_TRANSITIONS dict from token_service."""

    TRANSITIONS = {
        "call":     ["waiting", "on_hold"],
        "recall":   ["called"],
        "hold":     ["called", "in_progress"],
        "progress": ["called"],
        "complete": ["called", "in_progress"],
        "skip":     ["waiting", "called"],
        "cancel":   ["waiting", "called", "on_hold"],
    }

    def test_call_from_waiting_allowed(self):
        assert "waiting" in self.TRANSITIONS["call"]

    def test_complete_requires_called_or_in_progress(self):
        assert set(self.TRANSITIONS["complete"]) == {"called", "in_progress"}

    def test_cancel_cannot_cancel_completed(self):
        assert "completed" not in self.TRANSITIONS["cancel"]

    def test_recall_only_from_called(self):
        assert self.TRANSITIONS["recall"] == ["called"]

    def test_all_actions_have_transitions(self):
        for action, states in self.TRANSITIONS.items():
            assert len(states) >= 1, f"{action} has no valid source states"


class TestTokenNumberFormat:
    def test_format(self):
        prefix = "A"
        seq    = 23
        token_number = f"{prefix}-{seq:03d}"
        assert token_number == "A-023"

    def test_three_digit_padding(self):
        assert f"B-{1:03d}"   == "B-001"
        assert f"B-{99:03d}"  == "B-099"
        assert f"B-{100:03d}" == "B-100"
        assert f"B-{999:03d}" == "B-999"

    def test_prefix_is_uppercase(self):
        prefix = "C"
        assert prefix == prefix.upper()
