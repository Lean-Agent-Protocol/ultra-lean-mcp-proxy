"""Tests for delta response helpers."""

from ultra_lean_mcp_proxy.delta import apply_delta, create_delta


def test_create_delta_none_when_no_change():
    payload = {"items": [{"id": 1, "status": "open"}]}
    assert create_delta(payload, payload, min_savings_ratio=0.0) is None


def test_create_and_apply_delta_roundtrip():
    previous = {
        "items": [
            {"id": 1, "status": "open", "title": "alpha"},
            {"id": 2, "status": "open", "title": "beta"},
        ],
        "count": 2,
    }
    current = {
        "items": [
            {"id": 1, "status": "closed", "title": "alpha"},
            {"id": 2, "status": "open", "title": "beta"},
        ],
        "count": 2,
    }
    delta = create_delta(previous, current, min_savings_ratio=0.0)
    assert delta is not None
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current

