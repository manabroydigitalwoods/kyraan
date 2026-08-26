#!/bin/zsh
# Kyraan liveness watchdog (G-07): every 5 minutes, checks that launchd
# has a live PID for the bot; if not, alerts the owner on Telegram
# directly via the Bot API (which works even with the bot process dead)
# — at most once per hour. A dead assistant must announce itself.
REPO="/Users/owneruser/workspace/kyraan2.0"
MARK="/tmp/kyraan_watchdog_alerted"
export $(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_OWNER_ID)=' "$REPO/.env" | xargs)
pid=$(launchctl list | awk '$3 == "io.digitalwoods.kyraan" {print $1}')
if [[ "$pid" == "-" || -z "$pid" ]]; then
  now=$(date +%s)
  last=0
  [[ -f "$MARK" ]] && last=$(stat -f %m "$MARK")
  if (( now - last > 3600 )); then
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d chat_id="${TELEGRAM_OWNER_ID}" \
      -d text="⚠️ Kyraan watchdog: the bot process is not running. Restart with: launchctl kickstart -k gui/501/io.digitalwoods.kyraan" >/dev/null
    touch "$MARK"
  fi
else
  rm -f "$MARK"
fi
