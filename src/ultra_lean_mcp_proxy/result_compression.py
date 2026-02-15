"""Generic structured JSON result compression for Ultra Lean MCP Proxy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, MutableMapping


@dataclass
class CompressionOptions:
    mode: str = "balanced"  # off | balanced | aggressive
    strip_nulls: bool = False
    strip_defaults: bool = False
    min_payload_bytes: int = 512
    enable_columnar: bool = True
    columnar_min_rows: int = 8
    columnar_min_fields: int = 2


def _json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _stable_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


class TokenCounter:
    """Best-effort token estimator with optional tiktoken backend."""

    def __init__(self, encoding_name: str = "cl100k_base", *, strict: bool = False):
        self._enc = None
        self.encoding_name = encoding_name
        self.backend = "heuristic"
        self.reason = "tiktoken_unavailable_or_encoding_missing"
        try:
            import tiktoken  # type: ignore
        except ImportError as exc:
            if strict:
                raise ValueError(
                    "tiktoken is required for strict token counting but is not installed"
                ) from exc
            tiktoken = None  # type: ignore
        if tiktoken:
            try:
                self._enc = tiktoken.get_encoding(encoding_name)
                self.backend = "tiktoken"
                self.reason = "ok"
            except Exception as exc:
                if strict:
                    raise ValueError(
                        f"Requested tokenizer encoding '{encoding_name}' is unavailable"
                    ) from exc
                self._enc = None

    def count(self, value: Any) -> int:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if self._enc is not None:
            return len(self._enc.encode(text))
        # Deterministic fallback approximation.
        return max(1, len(text) // 4)


def _collect_key_frequency(node: Any, counter: dict[str, int]):
    if isinstance(node, dict):
        for key, value in node.items():
            counter[str(key)] = counter.get(str(key), 0) + 1
            _collect_key_frequency(value, counter)
    elif isinstance(node, list):
        for item in node:
            _collect_key_frequency(item, counter)


def _build_key_aliases(counter: dict[str, int], mode: str) -> dict[str, str]:
    if mode == "off":
        return {}
    min_freq = 1 if mode == "aggressive" else 2
    candidates = [
        (key, freq)
        for key, freq in counter.items()
        if freq >= min_freq and len(key) > 2
    ]
    # Rank by frequency then length: prioritize repetitive long keys.
    candidates.sort(key=lambda x: (x[1], len(x[0])), reverse=True)

    aliases: dict[str, str] = {}
    for idx, (key, _) in enumerate(candidates):
        alias = f"k{idx}"
        if len(alias) < len(key):
            aliases[key] = alias
    return aliases


def _is_defaultish(value: Any) -> bool:
    return value in (None, "", 0, False, [], {})


def _can_columnar(items: list[Any], opts: CompressionOptions) -> tuple[bool, list[str]]:
    if not opts.enable_columnar:
        return False, []
    if len(items) < opts.columnar_min_rows:
        return False, []
    if not items or not all(isinstance(item, dict) for item in items):
        return False, []

    first_keys = list(items[0].keys())
    if len(first_keys) < opts.columnar_min_fields:
        return False, []

    first_set = set(first_keys)
    for item in items[1:]:
        if set(item.keys()) != first_set:
            return False, []
    return True, first_keys


def _encode(node: Any, key_alias: dict[str, str], opts: CompressionOptions) -> Any:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if opts.strip_nulls and value is None:
                continue
            if opts.strip_defaults and str(key).lower() in {"default", "defaults"} and _is_defaultish(value):
                continue
            encoded_key = key_alias.get(str(key), str(key))
            out[encoded_key] = _encode(value, key_alias, opts)
        return out

    if isinstance(node, list):
        can_col, columns = _can_columnar(node, opts)
        if can_col:
            encoded_columns = [key_alias.get(str(col), str(col)) for col in columns]
            encoded_rows = []
            for item in node:
                row = [_encode(item[col], key_alias, opts) for col in columns]
                encoded_rows.append(row)
            return {"~t": {"c": encoded_columns, "r": encoded_rows}}
        return [_encode(item, key_alias, opts) for item in node]

    return node


def _decode(node: Any, alias_to_key: dict[str, str]) -> Any:
    if isinstance(node, dict):
        if "~t" in node and isinstance(node["~t"], dict):
            meta = node["~t"]
            columns = meta.get("c", [])
            rows = meta.get("r", [])
            if isinstance(columns, list) and isinstance(rows, list):
                decoded_items = []
                decoded_cols = [alias_to_key.get(str(col), str(col)) for col in columns]
                for row in rows:
                    if not isinstance(row, list):
                        continue
                    obj = {}
                    for idx, col in enumerate(decoded_cols):
                        if idx < len(row):
                            obj[col] = _decode(row[idx], alias_to_key)
                    decoded_items.append(obj)
                return decoded_items

        out = {}
        for key, value in node.items():
            decoded_key = alias_to_key.get(str(key), str(key))
            out[decoded_key] = _decode(value, alias_to_key)
        return out

    if isinstance(node, list):
        return [_decode(item, alias_to_key) for item in node]

    return node


def _key_ref(alias_to_key: dict[str, str]) -> str:
    digest = hashlib.sha256(_stable_json(alias_to_key).encode("utf-8")).hexdigest()[:12]
    return f"kdict-{digest}"


def compress_result(
    input_data: Any,
    options: CompressionOptions | None = None,
    *,
    key_registry: MutableMapping[str, dict[str, str]] | None = None,
    registry_counter: MutableMapping[str, int] | None = None,
    reuse_keys: bool = False,
    key_bootstrap_interval: int = 8,
) -> dict[str, Any]:
    """Compress structured JSON result with generic reversible transforms."""
    opts = options or CompressionOptions()
    original_bytes = _json_size(input_data)
    if original_bytes < opts.min_payload_bytes:
        return {
            "encoding": "lapc-json-v1",
            "compressed": False,
            "originalBytes": original_bytes,
            "compressedBytes": original_bytes,
            "savedBytes": 0,
            "savedRatio": 0.0,
            "data": input_data,
            "keys": {},
        }

    key_counter: dict[str, int] = {}
    _collect_key_frequency(input_data, key_counter)
    key_alias = _build_key_aliases(key_counter, opts.mode)
    encoded = _encode(input_data, key_alias, opts)
    alias_to_key = {alias: key for key, alias in key_alias.items()}

    envelope: dict[str, Any] = {
        "encoding": "lapc-json-v1",
        "compressed": True,
        "mode": opts.mode,
        "originalBytes": original_bytes,
        "data": encoded,
        "keys": alias_to_key,
    }

    if reuse_keys and key_registry is not None:
        ref = _key_ref(alias_to_key)
        include_keys = True
        previous = key_registry.get(ref)
        if previous == alias_to_key:
            include_keys = False
            if registry_counter is not None:
                count = registry_counter.get(ref, 0) + 1
                registry_counter[ref] = count
                if key_bootstrap_interval > 0 and (count % key_bootstrap_interval) == 0:
                    include_keys = True
        else:
            key_registry[ref] = dict(alias_to_key)
            if registry_counter is not None:
                registry_counter[ref] = 1

        envelope["keysRef"] = ref
        if not include_keys:
            envelope.pop("keys", None)

    compressed_bytes = _json_size(envelope)
    saved = original_bytes - compressed_bytes
    envelope["compressedBytes"] = compressed_bytes
    envelope["savedBytes"] = saved
    envelope["savedRatio"] = (saved / original_bytes) if original_bytes else 0.0

    if saved <= 0:
        envelope["compressed"] = False
        envelope["data"] = input_data
        envelope["keys"] = {}
        envelope.pop("keysRef", None)
        envelope["compressedBytes"] = original_bytes
        envelope["savedBytes"] = 0
        envelope["savedRatio"] = 0.0
    return envelope


def decompress_result(
    envelope: dict[str, Any],
    *,
    key_registry: MutableMapping[str, dict[str, str]] | None = None,
) -> Any:
    """Decompress a result previously produced by ``compress_result``."""
    if not isinstance(envelope, dict) or envelope.get("encoding") != "lapc-json-v1":
        raise ValueError("Unsupported compression envelope")
    data = envelope.get("data")
    if not envelope.get("compressed"):
        return data

    keys = envelope.get("keys")
    if not isinstance(keys, dict):
        keys = None

    keys_ref = envelope.get("keysRef")
    if keys is None and isinstance(keys_ref, str) and key_registry is not None:
        keys = key_registry.get(keys_ref)

    if not isinstance(keys, dict):
        raise ValueError("Invalid or missing key dictionary in envelope")

    return _decode(data, keys)


def token_savings(original: Any, candidate: Any, counter: TokenCounter | None = None) -> int:
    """Return positive value when candidate uses fewer tokens than original."""
    c = counter or TokenCounter()
    return c.count(original) - c.count(candidate)


def estimate_compressibility(value: Any) -> float:
    """Estimate whether a payload is likely to benefit from structural compression.

    Returns a score in `[0, 1]` based on key repetition, duplicate scalar values,
    and homogeneous list-of-object shapes.
    """
    key_counter: dict[str, int] = {}
    scalar_counter: dict[str, int] = {}
    homogeneous_lists = 0
    total_lists = 0

    def walk(node: Any):
        nonlocal homogeneous_lists, total_lists
        if isinstance(node, dict):
            for key, child in node.items():
                k = str(key)
                key_counter[k] = key_counter.get(k, 0) + 1
                walk(child)
        elif isinstance(node, list):
            total_lists += 1
            if node and all(isinstance(item, dict) for item in node):
                keysets = [tuple(sorted(item.keys())) for item in node]
                if len(set(keysets)) == 1:
                    homogeneous_lists += 1
            for child in node:
                walk(child)
        else:
            if isinstance(node, (str, int, float, bool)) or node is None:
                marker = json.dumps(node, ensure_ascii=False)
                scalar_counter[marker] = scalar_counter.get(marker, 0) + 1

    walk(value)

    total_keys = sum(key_counter.values())
    duplicate_keys = max(0, total_keys - len(key_counter))
    key_repeat_ratio = (duplicate_keys / total_keys) if total_keys else 0.0

    total_scalars = sum(scalar_counter.values())
    duplicate_scalars = max(0, total_scalars - len(scalar_counter))
    scalar_repeat_ratio = (duplicate_scalars / total_scalars) if total_scalars else 0.0

    homogeneous_ratio = (homogeneous_lists / total_lists) if total_lists else 0.0

    score = 0.5 * key_repeat_ratio + 0.25 * scalar_repeat_ratio + 0.25 * homogeneous_ratio
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return score

