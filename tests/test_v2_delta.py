"""Tests for delta response helpers (structural JSON diff)."""

import pytest

from ultra_lean_mcp_proxy.delta import (
    apply_delta,
    create_delta,
    stable_hash,
    _diff_values,
    canonicalize,
    _clone_json,
)

# Use a very negative threshold to test ops-level correctness
# regardless of whether the ops are smaller than the full payload.
NO_THRESHOLD = -999.0


def test_create_delta_none_when_no_change():
    payload = {"items": [{"id": 1, "status": "open"}]}
    assert create_delta(payload, payload, min_savings_ratio=NO_THRESHOLD) is None


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
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    assert delta["encoding"] == "lapc-delta-v1"
    assert "ops" in delta
    assert isinstance(delta["ops"], list)
    assert len(delta["ops"]) > 0
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current


def test_delta_ops_format():
    """Verify ops use set/delete with path arrays."""
    previous = {"a": 1, "b": 2, "c": 3}
    current = {"a": 1, "b": 99}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    ops = delta["ops"]
    op_types = {op["op"] for op in ops}
    assert op_types <= {"set", "delete"}
    for op in ops:
        assert isinstance(op["path"], list)


def test_delta_deleted_key():
    previous = {"x": 1, "y": 2, "z": 3}
    current = {"x": 1, "z": 3}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current


def test_delta_added_key():
    previous = {"x": 1}
    current = {"x": 1, "y": 2}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current


def test_delta_nested_change():
    previous = {"a": {"b": {"c": 1}}}
    current = {"a": {"b": {"c": 2}}}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current


def test_delta_array_element_change():
    previous = {"items": [1, 2, 3]}
    current = {"items": [1, 99, 3]}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current


def test_delta_array_length_change_replaces_whole():
    """When array lengths differ, the whole array is replaced."""
    previous = {"items": [1, 2]}
    current = {"items": [1, 2, 3]}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current


def test_delta_min_savings_threshold():
    """Delta is None when savings are below threshold."""
    previous = {"a": 1}
    current = {"a": 2}
    # Very high threshold - should return None
    delta = create_delta(previous, current, min_savings_ratio=0.99)
    assert delta is None


def test_delta_hashes():
    previous = {"foo": "bar"}
    current = {"foo": "baz"}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    assert delta["baselineHash"] == stable_hash(previous)
    assert delta["currentHash"] == stable_hash(current)


def test_delta_bytes_fields():
    previous = {"data": "x" * 100}
    current = {"data": "y" * 100}
    delta = create_delta(previous, current, min_savings_ratio=NO_THRESHOLD)
    assert delta is not None
    assert isinstance(delta["patchBytes"], int)
    assert isinstance(delta["fullBytes"], int)
    assert isinstance(delta["savedBytes"], int)
    assert isinstance(delta["savedRatio"], float)
    assert delta["savedBytes"] == delta["fullBytes"] - delta["patchBytes"]


def test_apply_delta_rejects_bad_encoding():
    with pytest.raises(ValueError, match="Unsupported"):
        apply_delta({}, {"encoding": "unknown"})


def test_apply_delta_rejects_missing_ops():
    with pytest.raises(ValueError, match="missing ops"):
        apply_delta({}, {"encoding": "lapc-delta-v1"})


def test_delta_large_payload_with_real_savings():
    """Realistic payload where structural delta produces actual savings."""
    previous = {
        "results": [{"id": i, "name": f"item_{i}", "value": i * 10} for i in range(50)],
        "total": 50,
        "page": 1,
    }
    current = {
        "results": [
            {"id": i, "name": f"item_{i}", "value": i * 10 if i != 25 else 999}
            for i in range(50)
        ],
        "total": 50,
        "page": 1,
    }
    delta = create_delta(previous, current, min_savings_ratio=0.0)
    assert delta is not None
    assert delta["savedBytes"] > 0
    assert delta["savedRatio"] > 0.0
    reconstructed = apply_delta(previous, delta)
    assert reconstructed == current


def test_diff_values_produces_correct_ops():
    """Low-level test of the diff algorithm."""
    ops = []
    _diff_values(
        canonicalize({"a": 1, "b": 2}),
        canonicalize({"a": 1, "b": 3}),
        [],
        ops,
    )
    assert len(ops) == 1
    assert ops[0]["op"] == "set"
    assert ops[0]["path"] == ["b"]
    assert ops[0]["value"] == 3


def test_diff_values_delete_op():
    ops = []
    _diff_values(
        canonicalize({"a": 1, "b": 2}),
        canonicalize({"a": 1}),
        [],
        ops,
    )
    assert len(ops) == 1
    assert ops[0]["op"] == "delete"
    assert ops[0]["path"] == ["b"]
