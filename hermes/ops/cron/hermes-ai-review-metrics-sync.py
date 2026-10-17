#!/usr/bin/env python3
"""Run the installed importer from the native cron script sandbox."""

import os
import sys

_IMPORTER = "/usr/local/lib/hermes-ops/sync-ai-review-metrics.py"


if __name__ == "__main__":
    # The scheduled job passes no arguments: never forward caller-supplied
    # values into the sandboxed importer.
    os.execv(sys.executable, [sys.executable, _IMPORTER])
