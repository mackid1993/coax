#!/bin/sh
# Build the coax image locally with its version stamped in (no CI needed).
#
#   scripts/build-image.sh 1.0.6-1          builds coax:1.0.6-1
#   scripts/build-image.sh 1.0.6-1 latest   also tags coax:latest
#   scripts/build-image.sh                  builds coax:dev-<short commit>
set -eu
cd "$(dirname "$0")/.."
v="$(scripts/stamp-version.sh "${1:-}")"
set -- -t "coax:$v" ${2:+-t "coax:$2"}
docker build "$@" --label "org.opencontainers.image.version=$v" .
echo "built coax:$v"
