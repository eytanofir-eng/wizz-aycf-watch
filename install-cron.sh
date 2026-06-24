#!/bin/bash
# Installs (or refreshes) the every-5-minute cron entry for the Wizz AYCF watcher.
# Safe to re-run: it removes any previous wizz-aycf-watch line before adding one.
set -euo pipefail

DIR="/Users/eytanofir/Claude/wizz-aycf-watch"
LINE="*/5 * * * * cd $DIR && $DIR/.venv/bin/python check_aycf.py >> $DIR/aycf.log 2>&1 # wizz-aycf-watch"

# Keep existing crontab, drop any old wizz-aycf-watch line, append the fresh one.
{ crontab -l 2>/dev/null | grep -v 'wizz-aycf-watch' || true; echo "$LINE"; } | crontab -

echo "Installed. Current crontab:"
crontab -l
