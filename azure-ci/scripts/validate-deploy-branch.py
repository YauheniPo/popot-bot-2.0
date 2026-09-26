"""Validate a queue-time deployment branch before Azure checks it out."""

import os
import sys

from branch_validation import validate_deploy_branch


def main() -> int:
    branch = os.environ.get("DEPLOY_BRANCH", "")
    validate_deploy_branch(branch)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"ERROR: {error}.", file=sys.stderr)
        raise SystemExit(1) from None
