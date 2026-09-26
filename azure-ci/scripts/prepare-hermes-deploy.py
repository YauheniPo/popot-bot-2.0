"""Freeze Azure's selected repository version; never execute source checkout code."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from branch_validation import validate_deploy_branch


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], text=True, stderr=subprocess.PIPE, timeout=120,
    ).strip()


def main() -> int:
    branch_ref = os.environ.get("DEPLOY_BRANCH", "")
    branch = validate_deploy_branch(branch_ref, full_ref=True)
    commit = os.environ.get("DEPLOY_COMMIT", "")
    mode = os.environ.get("DEPLOY_MODE", "")
    if mode not in {"full", "config-only", "runtime-only"}:
        raise ValueError("Invalid deployment mode")
    # Limit input to ordinary branch names, not tags, SHAs, revision expressions
    # or strings interpreted by Azure logging / shell / Markdown syntax.
    # Azure resolves the resource version before checkout. Never resolve the
    # branch again: it may have moved since the user selected this version.
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid selected resource commit")
    if git("rev-parse", "--verify", "HEAD^{commit}") != commit:
        raise ValueError("Source checkout does not match selected resource commit")
    git("cat-file", "-e", f"{commit}:hermes/ansible/playbook.yml")
    output = Path(os.environ["DEPLOY_ARTIFACT_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    git("archive", "--format=tar", f"--output={output / 'source.tar'}", commit)
    (output / "deployment.json").write_text(
        json.dumps({"branch": branch, "commit": commit, "mode": mode}) + "\n",
        encoding="utf-8",
    )
    summary = output / "deployment.md"
    summary.write_text(
        f"## Hermes production deployment\n\n"
        f"- Source branch: `{branch}`\n- Commit: `{commit}`\n- Mode: `{mode}`\n\n"
        "Approve this exact commit, not just the branch name. Its Ansible code "
        "will run with production credentials after approval. Later branch pushes "
        "do not change this archive.\n",
        encoding="utf-8",
    )
    print(f"Deployment source: {branch} at {commit}; mode: {mode}")
    print(f"##vso[task.uploadsummary]{summary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        # Never dump git config, credentials or raw subprocess output.
        print(f"ERROR: Could not prepare deployment: {error}. "
              "Check Resources > deploySource, deployMode and the selected commit checkout.", file=sys.stderr)
        raise SystemExit(1) from error
