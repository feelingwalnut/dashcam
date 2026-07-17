#!/usr/bin/env bash

set -euo pipefail

HOST="192.168.1.33"
USER="root"
PASS=""   # no password

BASE_DIR="./dashcam"
LOG_FILE="./dashcam_download.log"

# Overwrite log file at start
: > "$LOG_FILE"

# Redirect all output (stdout + stderr) to log
exec > >(tee -a "$LOG_FILE") 2>&1

echo "========================================"
echo "Dashcam download started: $(date)"
echo "========================================"

# Remote → local mappings
declare -A DIRS=(
  ["DCIM/Movie"]="video"
  ["DCIM/Movie/RO"]="video/RO"
  ["DCIM/Movie/Parking"]="video/Parking"
  ["DCIM/Photo"]="photo"
)

echo "Starting FTP dashcam download..."

lftp -u "$USER","$PASS" "$HOST" <<EOF

set ftp:passive-mode on
set net:max-retries 3
set net:timeout 10
set xfer:clobber on

$(for REMOTE in "${!DIRS[@]}"; do
    LOCAL="${BASE_DIR}/${DIRS[$REMOTE]}"

    cat <<INNER
echo "Processing $REMOTE -> $LOCAL"

mirror \
  --verbose \
  --continue \
  --only-newer \
  --include-glob *.mp4 \
  --include-glob *.MP4 \
  --include-glob *.jpg \
  --include-glob *.JPG \
  --Remove-source-dirs \
  "$REMOTE" "$LOCAL"

INNER
done)

bye
EOF

echo "========================================"
echo "Dashcam download finished: $(date)"
echo "========================================"

echo "Done."
