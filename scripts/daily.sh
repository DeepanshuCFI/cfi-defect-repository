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

# If a Vercel token is configured, redeploy the dashboard with the fresh data.
VERCEL_TOKEN=$(grep '^VERCEL_TOKEN=' .env 2>/dev/null | cut -d= -f2-)
if [ $STATUS -eq 0 ] && [ -n "$VERCEL_TOKEN" ] && [ "$VERCEL_TOKEN" != "REPLACE_ME" ]; then
  echo "--- deploying to vercel ---" >> "$LOG"
  (cd web && npx -y vercel deploy --prod --yes --token "$VERCEL_TOKEN") >> "$LOG" 2>&1 \
    || echo "WARN vercel deploy failed (site keeps yesterday's data)" >> "$LOG"
fi

echo "=== exit $STATUS $(date) ===" >> "$LOG"
# Failures are also visible on the /qa page (pipeline_run.ok = false)
exit $STATUS
