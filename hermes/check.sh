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

log "checking Bash syntax"
while IFS= read -r script; do
  bash -n "$REPOSITORY_ROOT/$script"
done < <(cd "$REPOSITORY_ROOT" && rg --files hermes -g '*.sh' | sort)

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
    "$HERMES_DIR/ops/startup-notify.sh"
fi

log "compiling Python sources"
python_sources=()
while IFS= read -r source; do
  python_sources+=("$REPOSITORY_ROOT/$source")
done < <(cd "$REPOSITORY_ROOT" && rg --files hermes .github/scripts -g '*.py' | sort)
PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m py_compile "${python_sources[@]}"

log "running Python tests"
python_tests=()
while IFS= read -r test_file; do
  python_tests+=("$REPOSITORY_ROOT/$test_file")
done < <(cd "$REPOSITORY_ROOT" && rg --files hermes -g 'test_*.py' | sort)
PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m unittest "${python_tests[@]}"
PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m unittest discover \
  -s "$REPOSITORY_ROOT/.github/scripts" \
  -p 'test_*.py'

if optional_tool pytest; then
  log "running pytest suite"
  PYTHONPYCACHEPREFIX="$CHECK_TEMP/pycache" python3 -m pytest -q \
    --no-header --disable-warnings \
    "$REPOSITORY_ROOT/hermes" "$REPOSITORY_ROOT/.github/scripts"
fi

if optional_tool ansible-playbook; then
  log "checking Ansible syntax"
  mkdir -p "$CHECK_TEMP/ansible-local" "$CHECK_TEMP/ansible-remote"
  ANSIBLE_LOCAL_TEMP="$CHECK_TEMP/ansible-local" \
    ANSIBLE_REMOTE_TEMP="$CHECK_TEMP/ansible-remote" \
    ansible-playbook \
      --syntax-check \
      -i "$HERMES_DIR/ansible/inventory.example.ini" \
      "$HERMES_DIR/ansible/playbook.yml"
fi

log "checking patch whitespace"
git -C "$REPOSITORY_ROOT" diff --check -- hermes .github/workflows .github/scripts .github/REVIEWER.md
log "all Hermes checks passed"
