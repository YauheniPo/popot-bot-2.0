#!/usr/bin/env python3
"""Run the installed importer from the native cron script sandbox."""

import os
import sys


if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable,
                            "/usr/local/lib/hermes-ops/sync-ai-review-metrics.py", *sys.argv[1:]])
