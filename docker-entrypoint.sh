#!/bin/sh
set -e

# Map container user to host UID/GID — lets the container write into NAS
# volumes owned by your Synology user without manual chown gymnastics.
PUID=${PUID:-1000}
PGID=${PGID:-1000}

CUR_UID=$(id -u app)
CUR_GID=$(id -g app)

if [ "$CUR_GID" != "$PGID" ]; then
    groupmod -o -g "$PGID" app
fi
if [ "$CUR_UID" != "$PUID" ]; then
    usermod -o -u "$PUID" app
fi

# Make sure mount points are writable by the (possibly remapped) user.
# Suppress errors — if the volume is huge, recursive chown is skipped on
# subsequent boots once the top-level dir is already owned correctly.
if [ "$(stat -c '%u' /state)" != "$PUID" ]; then
    chown -R "$PUID:$PGID" /state 2>/dev/null || true
fi
if [ "$(stat -c '%u' /data)" != "$PUID" ]; then
    chown "$PUID:$PGID" /data 2>/dev/null || true
fi

exec gosu app "$@"
