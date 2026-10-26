#!/usr/bin/env bash
set -Eeuo pipefail

REQUIRE_TOOLS=false
if [[ "${1:-}" == "--require-tools" ]]; then
  REQUIRE_TOOLS=true
  shift
fi
if (($# > 0)); then
  printf 'Usage: %s [--require-tools]\n' "$0" >&2
  exit 2
fi

HERMES_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly HERMES_DIR
REPOSITORY_ROOT="$(cd -- "$HERMES_DIR/.." && pwd)"
readonly REPOSITORY_ROOT
CHECK_TEMP="$(mktemp -d /tmp/hermes-check.XXXXXX)"
readonly CHECK_TEMP
trap 'rm -rf -- "$CHECK_TEMP"' EXIT

log() {
  printf '[hermes-check] %s\n' "$*"
}

optional_tool() {
  local command_name="$1"
  if command -v "$command_name" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$REQUIRE_TOOLS" == true ]]; then
    printf '[hermes-check] ERROR: required command is missing: %s\n' "$command_name" >&2
    exit 1
  fi
  log "skipping unavailable optional check: $command_name"
  return 1
}

for required_command in git python3 rg; do
  command -v "$required_command" >/dev/null 2>&1 || {
    printf '[hermes-check] ERROR: required command is missing: %s\n' "$required_command" >&2
    exit 1
  }
done

# File lists come from git, so an untracked leftover in a developer's working
# copy (a scratch test, a debug script, a .bak copy) cannot fail a check that is
# meant to validate the committed tree. Outside a git work tree the filesystem
# listing is used instead.
repository_files() {
  local pattern="$1"
  shift
  if git -C "$REPOSITORY_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$REPOSITORY_ROOT" ls-files -- "$@" | grep -E "$pattern" || true
  else
    (cd "$REPOSITORY_ROOT" && rg --files "$@") | grep -E "$pattern" || true
  fi
}

log "checking Bash syntax"
while IFS= read -r script; do
  bash -n "$REPOSITORY_ROOT/$script"
done < <(repository_files '\.sh$' hermes | sort)

if optional_tool shellcheck; then
  log "running shellcheck"
  shellcheck -x \
    -P "$HERMES_DIR" \
    -P "$HERMES_DIR/ops" \
    "$HERMES_DIR/check.sh" \
    "$HERMES_DIR/deploy-hermes.sh" \
    "$HERMES_DIR/ops/install-ops.sh" \
    "$HERMES_DIR/docker/start-local.sh" \
    "$HERMES_DIR/ops/backup.sh" \
    "$HERMES_DIR/ops/health-check.sh" \
    "$HERMES_DIR/ops/install-browser-automation.sh" \
    "$HERMES_DIR/ops/api-retry-loop.sh" \
    "$HERMES_DIR/ops/notify-doctor.sh" \
    "$HERMES_DIR/ops/startup-notify.sh"
fi

log "compiling Python sources"
python_sources=()
while IFS= read -r source; do
  python_sources+=("$REPOSITORY_ROOT/$source")
done < <(repository_files '\.py$' hermes .github/scripts | sort)
PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m py_compile "${python_sources[@]}"

log "running Python tests"
python_tests=()
while IFS= read -r test_file; do
  python_tests+=("$REPOSITORY_ROOT/$test_file")
done < <(repository_files '(^|/)test_[^/]*\.py$' hermes | sort)
PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m unittest "${python_tests[@]}"

# Discovery keeps the repository root as the working directory: these tests read
# paths such as `.github/REVIEWER.md` relative to it, and a tracked-only file
# list cannot be passed to discovery, so the directory is scanned as before.
PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m unittest discover \
  -s "$REPOSITORY_ROOT/.github/scripts" \
  -p 'test_*.py'

if python3 -c 'import pytest' >/dev/null 2>&1; then
  log "running pytest suite"
  PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m pytest -q \
    --no-header --disable-warnings \
    "$REPOSITORY_ROOT/hermes" "$REPOSITORY_ROOT/.github/scripts"
elif [[ "$REQUIRE_TOOLS" == true ]]; then
  printf '[hermes-check] ERROR: required Python module is missing: pytest\n' >&2
  exit 1
else
  # A `pytest` executable in PATH can belong to a different interpreter than the
  # `python3` used here, so the module is probed in this interpreter instead of
  # letting `-m pytest` fail and look like a failing test suite.
  log "skipping pytest suite: pytest is not installed for $(python3 -c 'import sys; print(sys.executable)')"
fi

if optional_tool promtool; then
  log "checking Prometheus rules"
  promtool check rules "$HERMES_DIR"/observability/rules/*.yml
fi

if optional_tool ansible-playbook; then
  log "checking Ansible syntax"
  mkdir -p "$CHECK_TEMP/ansible-local" "$CHECK_TEMP/ansible-remote"
  ANSIBLE_CONFIG="$HERMES_DIR/ansible/ansible.cfg" \
  ANSIBLE_LOCAL_TEMP="$CHECK_TEMP/ansible-local" \
    ANSIBLE_REMOTE_TEMP="$CHECK_TEMP/ansible-remote" \
    ansible-playbook \
      --syntax-check \
      -i "$HERMES_DIR/ansible/inventory.ini" \
      "$HERMES_DIR/ansible/playbook.yml"
fi

log "checking patch whitespace"
git -C "$REPOSITORY_ROOT" diff --check -- hermes .github/workflows .github/scripts .github/REVIEWER.md
log "all Hermes checks passed"
