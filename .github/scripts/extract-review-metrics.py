#!/usr/bin/env python3
"""Repository-facing wrapper for the deployable Hermes review importer."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_SOURCE = Path(__file__).resolve().parents[2] / "hermes" / "ops" / "extract-review-metrics.py"
_SPEC = importlib.util.spec_from_file_location("hermes_extract_review_metrics", _SOURCE)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
globals().update({name: value for name, value in vars(_MODULE).items() if not name.startswith("__")})


if __name__ == "__main__":
    raise SystemExit(main())
