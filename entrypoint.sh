#!/bin/bash
set -e

# Get user/group IDs from environment (default to 1000)
PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "Starting File Downloader Service..."
echo "UID: $PUID, GID: $PGID"

# Update appuser UID/GID if different from default
if [ "$PUID" != "1000" ] || [ "$PGID" != "1000" ]; then
    echo "Updating appuser UID/GID..."
    groupmod -g "$PGID" appgroup 2>/dev/null || true
    usermod -u "$PUID" -g "$PGID" appuser 2>/dev/null || true
fi

# Ensure data directory exists
if [ ! -d "/data" ]; then
    echo "Creating /data directory..."
    mkdir -p /data
fi

# Ensure downloads directory exists
if [ ! -d "/downloads" ]; then
    echo "Creating /downloads directory..."
    mkdir -p /downloads
fi

# Fix ownership of directories
echo "Setting directory ownership..."
chown -R appuser:appgroup /data /downloads /app 2>/dev/null || true

# Verify write access
echo "Verifying write permissions..."

if ! su-exec appuser touch /data/.write_test 2>/dev/null; then
    if ! gosu appuser touch /data/.write_test 2>/dev/null; then
        echo "WARNING: appuser cannot write to /data, attempting chmod..."
        chmod 777 /data 2>/dev/null || true
    fi
fi
rm -f /data/.write_test 2>/dev/null || true

if ! su-exec appuser touch /downloads/.write_test 2>/dev/null; then
    if ! gosu appuser touch /downloads/.write_test 2>/dev/null; then
        echo "WARNING: appuser cannot write to /downloads, attempting chmod..."
        chmod 777 /downloads 2>/dev/null || true
    fi
fi
rm -f /downloads/.write_test 2>/dev/null || true

echo "Directory permissions configured"

# Execute the main command as appuser
echo "Starting application as appuser..."
exec gosu appuser "$@"
