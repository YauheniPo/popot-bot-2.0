"""Freeze a fetched branch for deployment after approval; never execute its code."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], text=True, stderr=subprocess.PIPE, timeout=120,
    ).strip()


def main() -> int:
    branch = os.environ.get("DEPLOY_BRANCH", "").removeprefix("refs/heads/")
    mode = os.environ.get("DEPLOY_MODE", "")
    if mode not in {"full", "config-only", "runtime-only"}:
        raise ValueError("Invalid deployment mode")
    # Limit input to ordinary branch names, not tags, SHAs, revision expressions
    # or strings interpreted by Azure logging / shell / Markdown syntax.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or branch.startswith("refs/"):
        raise ValueError("Invalid deployment branch name")
    git("check-ref-format", f"refs/heads/{branch}")
    # checkout:self with fetchDepth:0 fetches remote branches through the
    # existing service connection. No PAT or persisted checkout token is needed.
    commit = git("rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}")
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
              "Check deployBranch, deployMode and full checkout of remote branches.", file=sys.stderr)
        raise SystemExit(1) from error
