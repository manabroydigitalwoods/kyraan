#!/bin/zsh
# The deploy, in one place (2026-09-04): suite gated, eval gated, commit
# from a message file, push, and restart BOTH services — the panel is its
# own process and a bot-only restart left it serving stale code twice.
#
#   scripts/deploy.sh <commit-message-file> [--no-eval]
set -e
cd "$(dirname "$0")/.."
MSG="$1"; shift || true
[ -f "$MSG" ] || { echo "usage: scripts/deploy.sh <commit-message-file> [--no-eval]"; exit 2; }
.venv/bin/python -m pytest -q > /tmp/kyraan_suite.txt 2>&1 || true
tail -1 /tmp/kyraan_suite.txt
if ! grep -qE "^[0-9]+ passed" /tmp/kyraan_suite.txt || grep -qE "[0-9]+ failed" /tmp/kyraan_suite.txt; then
  grep -E "^FAILED" /tmp/kyraan_suite.txt; echo "SUITE NOT GREEN — not deploying"; exit 1
fi
if [ "$1" != "--no-eval" ]; then
  .venv/bin/python scripts/eval.py > /tmp/kyraan_eval.txt 2>&1 || true
  grep -E "^(HARD|SOFT):" /tmp/kyraan_eval.txt | head -1
  if ! grep -q "^HARD: 21/21" /tmp/kyraan_eval.txt; then
    grep -A3 "^failures" /tmp/kyraan_eval.txt; echo "EVAL NOT GREEN — not deploying"; exit 1
  fi
fi
git add -A
git commit --quiet --no-verify -F "$MSG"
git push origin main 2>&1 | tail -1
launchctl kickstart -k "gui/$(id -u)/ai.kyraan"
launchctl kickstart -k "gui/$(id -u)/ai.kyraan.panel"
sleep 4
launchctl list | grep -E "ai.kyraan(.panel)?$"
