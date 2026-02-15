"""Tests for v2 generic result compression."""

from ultra_lean_mcp_proxy.result_compression import (
    CompressionOptions,
    compress_result,
    decompress_result,
    estimate_compressibility,
)


def test_compress_and_decompress_roundtrip():
    data = {
        "repositories": [
            {
                "repository_name": "alpha",
                "repository_description": "Primary repository",
                "repository_owner": "team-a",
            },
            {
                "repository_name": "beta",
                "repository_description": "Secondary repository",
                "repository_owner": "team-b",
            },
            {
                "repository_name": "gamma",
                "repository_description": "Tertiary repository",
                "repository_owner": "team-c",
            },
        ]
    }
    envelope = compress_result(data, CompressionOptions(mode="aggressive", min_payload_bytes=0))
    reconstructed = decompress_result(envelope)
    assert reconstructed == data


def test_small_payload_returns_uncompressed():
    payload = {"a": 1}
    envelope = compress_result(payload, CompressionOptions(min_payload_bytes=1024))
    assert envelope["compressed"] is False
    assert envelope["data"] == payload
    assert decompress_result(envelope) == payload


def test_aggressive_mode_can_save_bytes_for_repetitive_structures():
    data = {
        "items": [
            {
                "very_long_common_key_name": i,
                "another_repeated_field_name": i * 2,
                "third_repeated_property_name": str(i),
            }
            for i in range(30)
        ]
    }
    envelope = compress_result(data, CompressionOptions(mode="aggressive", min_payload_bytes=0))
    assert envelope["compressed"] is True
    assert envelope["savedBytes"] >= 0


def test_compressibility_score_higher_for_repetitive_payloads():
    repetitive = {
        "items": [
            {"service": "api", "region": "us-east-1", "status": "ok"}
            for _ in range(30)
        ]
    }
    diverse = {
        "items": [
            {"id": i, "name": f"n{i}", "value": i * 13}
            for i in range(30)
        ]
    }
    assert estimate_compressibility(repetitive) > estimate_compressibility(diverse)

