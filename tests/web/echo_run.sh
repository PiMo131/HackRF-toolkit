#!/bin/sh
# Run all Echo tests. Node unit/integration tests always; the headless-browser test only if playwright is present.
set -e
cd "$(dirname "$0")/../.."
for f in profiles dsp capture scheduler storage lab_tx; do
  echo "=== echo_$f ==="
  node "tests/web/echo_$f.js"
done
if node -e "require('playwright')" 2>/dev/null; then
  echo "=== echo_browser ==="
  node tests/web/echo_browser.js
else
  echo "=== echo_browser (skipped: playwright not installed) ==="
fi
echo "all echo tests passed"
