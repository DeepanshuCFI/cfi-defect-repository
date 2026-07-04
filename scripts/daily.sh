#!/bin/bash
# Daily pipeline run — install with:  crontab -e
#   30 6 * * *  /bin/bash /Users/a39002/Documents/Claude/crashfree-infra-repo/scripts/daily.sh
# States collected: config_settings.json -> ingestion.daily_states (default: Bihar)
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
LOG="logs/daily-$(date +%F).log"
echo "=== daily run $(date) ===" >> "$LOG"
/usr/local/bin/python3 -m pipeline.run daily --days 2 >> "$LOG" 2>&1
STATUS=$?
echo "=== exit $STATUS $(date) ===" >> "$LOG"
# Failures are also visible on the /qa page (pipeline_run.ok = false)
exit $STATUS
