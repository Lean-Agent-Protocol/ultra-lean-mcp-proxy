"""Test package bootstrap for local src layouts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY_SRC = ROOT / "src"
CORE_SRC = ROOT.parent / "ultra-lean-mcp-core" / "src"

for path in (PROXY_SRC, CORE_SRC):
    text = str(path)
    if path.exists() and text not in sys.path:
        sys.path.insert(0, text)
