#!/usr/bin/env bash
# Code-server extension installer for hermes VPS
# Usage: ./install-extensions.sh [extensions] (JSON input)

set -euo pipefail

# Load JSON configuration
extensions=()
# shellcheck disable=SC2154
for line in $(jq -r '.recommendations[]')
do
 extensions+=("\"$line\"")
done

echo "Installing code-server extensions:" >&2
for ext in "${extensions[@]}" ;
 do
  code-server --install-extension "$ext"
 done

echo "Extension installation complete." >&2