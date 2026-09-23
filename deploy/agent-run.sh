#!/usr/bin/env bash
# Run an agent module as the isolated 'indodax' user inside /opt/indodax-agent.
# Usage (from anywhere):  sudo bash deploy/agent-run.sh scripts.smoke_public
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
[ $# -ge 1 ] || { echo "usage: sudo bash deploy/agent-run.sh <module> [args...]"; exit 1; }
MOD="$1"; shift
case "$MOD" in
  scripts.*|agent.*) ;;
  *) echo "only scripts.* or agent.* modules"; exit 1 ;;
esac
cd /opt/indodax-agent
exec sudo -u indodax -H env PYTHONDONTWRITEBYTECODE=1 /opt/indodax-agent/.venv/bin/python -m "$MOD" "$@"
