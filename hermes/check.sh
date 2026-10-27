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

# Check existing tracked files in the working tree, including staged additions.
# Ignore deleted index entries and untracked scratch files. Outside a git work
# tree the filesystem listing is used instead.
repository_files() {
  local pattern="$1"
  shift
  if git -C "$REPOSITORY_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$REPOSITORY_ROOT" ls-files -- "$@"
  else
    (cd "$REPOSITORY_ROOT" && rg --files "$@")
  fi | grep -E "$pattern" | while IFS= read -r file; do
    if [[ -f "$REPOSITORY_ROOT/$file" ]]; then
      printf '%s\n' "$file"
    fi
  done || true
}

log "checking Bash syntax"
while IFS= read -r script; do
  bash -n "$REPOSITORY_ROOT/$script"
done < <(repository_files '\.sh$' hermes | sort)

if optional_tool shellcheck; then
  log "running shellcheck"
  # Only existing targets: a script removed from the working tree must not abort
  # the run, same as the tracked-file lists above.
  shellcheck_sources=()
  for script in \
    check.sh deploy-hermes.sh ops/install-ops.sh docker/start-local.sh \
    ops/backup.sh ops/health-check.sh ops/install-browser-automation.sh \
    ops/api-retry-loop.sh ops/notify-doctor.sh ops/startup-notify.sh; do
    if [[ -f "$HERMES_DIR/$script" ]]; then
      shellcheck_sources+=("$HERMES_DIR/$script")
    fi
  done
  if ((${#shellcheck_sources[@]} > 0)); then
    shellcheck -x -P "$HERMES_DIR" -P "$HERMES_DIR/ops" "${shellcheck_sources[@]}"
  fi
fi

log "compiling Python sources"
python_sources=()
pytest_tests=()
while IFS= read -r source; do
  python_sources+=("$REPOSITORY_ROOT/$source")
  if [[ "${source##*/}" == test_*.py || "${source##*/}" == *_test.py ]]; then
    pytest_tests+=("$REPOSITORY_ROOT/$source")
  fi
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
    "${pytest_tests[@]}"
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
