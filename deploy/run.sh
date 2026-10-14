#!/bin/bash
# Cron entry point (no root needed), same pattern as the homeserver's hotlist watcher:
#   */2 * * * * flock -n /home/martin/price-watch/run.lock /home/martin/price-watch/deploy/run.sh
# While the watcher runs, flock -n makes this a no-op; after a crash or reboot it starts it again.
# Rotates the log at 20 MB (keeps one old file).
cd "$(dirname "$0")/.." || exit 1
LOG=watch.log
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 20000000 ]; then mv -f "$LOG" "$LOG.1"; fi
echo "$(date '+%F %T') run.sh: starting watch.py" >> "$LOG"
exec .venv/bin/python watch.py >> "$LOG" 2>&1
