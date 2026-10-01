#!/bin/bash
# Reproduce the vendored NegPy reference: a pinned clone of upstream (GPL-3.0).
# We reference NegPy, we do not redistribute it — this fetches it from source.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=NEGPY_PIN
. "$HERE/NEGPY_PIN"
DIR="$HERE/NegPy"
if [ -d "$DIR/.git" ]; then
  echo "vendor/NegPy exists; fetching + checking out pin $NEGPY_COMMIT"
  git -C "$DIR" fetch --quiet --tags origin
else
  echo "cloning NegPy into vendor/NegPy"
  git clone --quiet https://github.com/marcinz606/NegPy "$DIR"
fi
git -C "$DIR" checkout --quiet "$NEGPY_COMMIT"
echo "vendor/NegPy pinned at $(git -C "$DIR" rev-parse --short HEAD) ($(cat "$DIR/VERSION"), expected $NEGPY_VERSION)"
