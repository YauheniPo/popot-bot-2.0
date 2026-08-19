#!/usr/bin/env python3
"""Apply a small, version-checked reliability fix to Hermes Edge TTS.

The upstream tool treats a transient Edge ``NoAudioReceived`` response as a
final failure. This helper retries that exact exception twice and leaves every
other provider and exception unchanged. It is safe to run on every deploy.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


DEFAULT_TARGET = "/opt/hermes/tools/tts_tool.py"
MARKER = "# Local Hermes: retry transient Edge NoAudioReceived failures"
OLD = """    communicate = _edge_tts.Communicate(text, **kwargs)\n    await communicate.save(output_path)\n    return output_path\n"""
NEW = f'''    {MARKER}
    # Edge occasionally closes a valid synthesis stream without audio. A new
    # Communicate instance is required for each retry; retry no other errors.
    for attempt in range(3):
        try:
            communicate = _edge_tts.Communicate(text, **kwargs)
            await communicate.save(output_path)
            return output_path
        except Exception as exc:
            if exc.__class__.__name__ != "NoAudioReceived" or attempt == 2:
                raise
            try:
                Path(output_path).unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning(
                "Edge TTS returned no audio; retrying synthesis (%d/3)",
                attempt + 2,
            )
            await asyncio.sleep(float(attempt + 1))
'''


def main() -> int:
    target_arg = sys.argv[1] if len(sys.argv) == 2 else None
    if len(sys.argv) > 2:
        print(f"Usage: {Path(sys.argv[0]).name} [PATH_TO_TTS_TOOL]", file=sys.stderr)
        return 2
    target = Path(target_arg or os.environ.get("HERMES_TTS_TOOL_PATH", DEFAULT_TARGET))

    if not target.is_file():
        print(f"[hermes] Edge TTS retry skipped: {target} is missing", file=sys.stderr)
        return 0

    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        print("[hermes] Edge TTS transient retry already installed")
        return 0
    if OLD not in source:
        print(
            "[hermes] Edge TTS retry skipped: upstream implementation changed",
            file=sys.stderr,
        )
        return 0

    target.write_text(source.replace(OLD, NEW, 1), encoding="utf-8")
    print("[hermes] installed Edge TTS transient retry")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
