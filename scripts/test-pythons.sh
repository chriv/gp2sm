#!/usr/bin/env bash
# Run the tests under every supported Python found on this machine (macOS or Linux), each in its own venv
# under .venvs/. CI pushes test only some OS/Python pairs; this covers the rest locally before pushing.
# Usage: scripts/test-pythons.sh [pytest args...]
set -u
cd "$(dirname "$0")/.."
versions=(3.11 3.12 3.14)
failed=()
ran=0
for v in "${versions[@]}"; do
  py=$(command -v "python$v" || true)
  if [ -z "$py" ]; then
    echo "== python$v: not installed, skipped"
    continue
  fi
  venv=".venvs/py$v"
  if [ ! -x "$venv/bin/python" ]; then
    "$py" -m venv "$venv" || { failed+=("$v (venv)"); continue; }
  fi
  "$venv/bin/python" -m pip install -q -e ".[dev]" || { failed+=("$v (install)"); continue; }
  echo "== python$v ($("$venv/bin/python" --version 2>&1))"
  "$venv/bin/python" -m pytest -q "$@" | tail -1
  status=${PIPESTATUS[0]}
  ran=$((ran + 1))
  [ "$status" -eq 0 ] || failed+=("$v")
done
if [ "$ran" -eq 0 ]; then
  echo "no supported Python found (${versions[*]})"
  exit 1
fi
if [ "${#failed[@]}" -gt 0 ]; then
  echo "FAILED: ${failed[*]}"
  exit 1
fi
echo "all passed"
