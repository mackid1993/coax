#!/bin/sh
# Stamp the coax version into the build. Writes app/VERSION, which the image copies with app/ and the web UI
# shows in the sidebar. CI runs it with the version it publishes; run it yourself before a local build
# (scripts/build-image.sh does both).
#
#   scripts/stamp-version.sh 1.0.6-1     stamps 1.0.6-1
#   scripts/stamp-version.sh             stamps dev-<short commit>
set -eu
cd "$(dirname "$0")/.."
v="${1:-}"
if [ -z "$v" ]; then
  v="dev-$(git rev-parse --short HEAD 2>/dev/null || echo local)"
fi
case "$v" in
  *[!0-9A-Za-z.+_-]*) echo "stamp-version: '$v' is not a version (letters, digits, . + _ - only)" >&2; exit 1 ;;
esac
printf '%s\n' "$v" > app/VERSION
echo "$v"
