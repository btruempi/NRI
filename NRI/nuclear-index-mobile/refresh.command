#!/usr/bin/env bash
# Double-click me to pull fresh prices and rebuild the site.
# macOS / Linux. Requires python3 (preinstalled on macOS).
set -euo pipefail

cd "$(dirname "$0")"

# Pick the first python we can find.
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "ERROR: python3 not found on PATH."
  echo "Install it from https://www.python.org/downloads/ and try again."
  read -n 1 -s -r -p "Press any key to close..." || true
  echo
  exit 1
fi

echo "==> Rebuilding Nuclear Renaissance Index with today's prices"
echo "    using $("$PY" --version 2>&1)"
echo

if "$PY" build_static_site.py; then
  echo
  echo "==> Done. Rebuilt files:"
  [ -f nri.html ] && echo "    - $(pwd)/nri.html"
  [ -f Nuclear-Renaissance-Index.html ] && echo "    - $(pwd)/Nuclear-Renaissance-Index.html"
  echo
  echo "Open either file in your browser. The 'Baked' badge should show 'just now'."
else
  echo
  echo "!! Build failed. Scroll up to see the error."
  echo "   Tip: if network is unavailable, you can still rebuild with a synthetic"
  echo "   baseline:  $PY build_static_site.py --offline"
fi

echo
read -n 1 -s -r -p "Press any key to close this window..." || true
echo
