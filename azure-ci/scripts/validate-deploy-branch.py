"""Validate a queue-time deployment branch before Azure checks it out."""

import os
import re
import subprocess
import sys


def main() -> int:
    branch = os.environ.get("DEPLOY_BRANCH", "")
    if (not branch or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9._-])?", branch)
            or branch.startswith("refs/")
            or any(part in {".", ".."} for part in branch.split("/"))):
        raise ValueError("Invalid deployment branch name")
    subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError):
        print("ERROR: Deployment branch validation failed.", file=sys.stderr)
        raise SystemExit(1) from None
