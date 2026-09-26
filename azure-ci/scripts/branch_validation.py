"""Shared validation for Azure's selected Hermes deployment branch."""

import re
import subprocess


BRANCH_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9._-])?")


def validate_deploy_branch(value: str, *, full_ref: bool = False) -> str:
    prefix = "refs/heads/"
    if full_ref:
        if not value.startswith(prefix):
            raise ValueError("Invalid deployment branch name")
        branch = value.removeprefix(prefix)
        if branch.startswith("refs/"):
            raise ValueError("Invalid deployment branch name")
    else:
        branch = value
        if branch.startswith("refs/"):
            raise ValueError("Enter a short branch name, not a ref")
    if (not branch or not BRANCH_PATTERN.fullmatch(branch)
            or any(not part or part in {".", ".."} for part in branch.split("/"))):
        raise ValueError("Invalid deployment branch name")
    try:
        subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{branch}"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError(f"Git rejected the branch name (exit {error.returncode})") from None
    except subprocess.TimeoutExpired:
        raise ValueError("Git ref-format check timed out") from None
    except OSError:
        raise ValueError("Git ref-format check could not run") from None
    return branch
