#!/usr/bin/env bash
# Run the Foreman test suite. Every test is a standalone python script that
# exits nonzero on failure. No external dependencies, no network, no models.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$TESTS_DIR")"

echo "== syntax checks =="
bash -n "$REPO_DIR/scripts/foreman.sh" || exit 1
python3 -m py_compile "$REPO_DIR/scripts/foreman-runner.py" || exit 1
echo "OK"

failures=0
for test in "$TESTS_DIR"/test_*.py; do
  echo ""
  echo "== $(basename "$test") =="
  if ! python3 "$test"; then
    echo "FAILED: $(basename "$test")"
    failures=$((failures + 1))
  fi
done

for test in "$TESTS_DIR"/test_*.sh; do
  [ -e "$test" ] || continue
  echo ""
  echo "== $(basename "$test") =="
  if ! bash "$test"; then
    echo "FAILED: $(basename "$test")"
    failures=$((failures + 1))
  fi
done

echo ""
if [ "$failures" -gt 0 ]; then
  echo "RESULT: $failures test file(s) failed"
  exit 1
fi
echo "RESULT: all tests passed"
