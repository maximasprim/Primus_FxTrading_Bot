"""Tests for app/execution/approval_service.py's command parsing
(notification-feature spec §10)."""
from app.execution.approval_service import parse_approval_command


def test_parses_approve_with_instrument_and_signal_id():
    cmd = parse_approval_command("APPROVE EURUSD EURUSD-20260727-5M-LIQUIDITYSWE-MSS")
    assert cmd is not None
    assert cmd.action == "APPROVE"
    assert cmd.instrument == "EURUSD"
    assert cmd.signal_id == "EURUSD-20260727-5M-LIQUIDITYSWE-MSS"


def test_parses_reject_with_signal_id_only():
    cmd = parse_approval_command("REJECT EURUSD-20260727-5M-LIQUIDITYSWE-MSS")
    assert cmd is not None
    assert cmd.action == "REJECT"
    assert cmd.signal_id == "EURUSD-20260727-5M-LIQUIDITYSWE-MSS"


def test_case_insensitive_action():
    cmd = parse_approval_command("approve EURUSD SIGNAL-12345")
    assert cmd is not None
    assert cmd.action == "APPROVE"


def test_rejects_unrecognized_text():
    assert parse_approval_command("hey what's up") is None
    assert parse_approval_command("") is None
    assert parse_approval_command("MAYBE EURUSD SIGNAL-1") is None
