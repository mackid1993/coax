#!/bin/sh
# coax container entrypoint.
#
# Why this exists: when ./data does not exist yet, Docker creates it on the host owned by root, and coax
# (uid 1000) could not write its config. So the container starts as root, fixes the ownership of /data
# once, then drops to uid/gid 1000 with setpriv (util-linux, part of every Debian base) and execs coax.
# coax itself never runs as root.
#
# Supplementary groups kept after the drop:
#   * the coax user's own groups from /etc/group (video),
#   * every group the container was started with (compose group_add, e.g. the host's render GID),
#   * the group that owns each /dev/dri node, so VAAPI works even without group_add,
#   * the group that owns the Channels DVR Imports folder mounted at /imports, so coax can write its Stream Files
#     there when that folder is group-writable.
#
# If the container is started as a non-root user (compose "user:"), nothing is changed and coax runs as is.
set -eu

COAX_UID=1000
COAX_GID=1000
DATA=/data

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

mkdir -p "$DATA"
# chown only when something under /data is not ours, so restarts stay fast on large caches.
if [ -n "$(find "$DATA" \( ! -user "$COAX_UID" -o ! -group "$COAX_GID" \) -print -quit 2>/dev/null)" ]; then
    echo "coax: setting the owner of $DATA to $COAX_UID:$COAX_GID" >&2
    chown -R "$COAX_UID:$COAX_GID" "$DATA" || echo "coax: warning: could not chown $DATA; coax may not be able to write it" >&2
fi

groups="$(id -G coax 2>/dev/null || true) $(id -G)"
for node in /dev/dri/renderD* /dev/dri/card*; do
    [ -e "$node" ] && groups="$groups $(stat -c %g "$node")"
done
[ -d /imports ] && groups="$groups $(stat -c %g /imports)"
# Drop root's own group 0, remove duplicates, join with commas.
groups="$(printf '%s\n' $groups | grep -vx 0 | sort -un | paste -sd, -)"

if [ -n "$groups" ]; then
    exec setpriv --reuid="$COAX_UID" --regid="$COAX_GID" --groups="$groups" -- "$@"
fi
exec setpriv --reuid="$COAX_UID" --regid="$COAX_GID" --clear-groups -- "$@"
