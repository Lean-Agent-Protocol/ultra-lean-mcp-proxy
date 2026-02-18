"""Ultra Lean MCP Proxy package."""

__version__ = "0.3.1"

from .config import ProxyConfig, load_proxy_config
from .delta import apply_delta, canonicalize, create_delta, stable_hash
from .proxy import run_proxy
from .result_compression import (
    TokenCounter,
    compress_result,
    decompress_result,
    estimate_compressibility,
    token_savings,
)

__all__ = [
    "ProxyConfig",
    "load_proxy_config",
    "compress_result",
    "decompress_result",
    "estimate_compressibility",
    "token_savings",
    "TokenCounter",
    "create_delta",
    "apply_delta",
    "canonicalize",
    "stable_hash",
    "run_proxy",
]
