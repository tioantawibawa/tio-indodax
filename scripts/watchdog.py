"""Satpam — run by systemd timer every 5 minutes (deploy/indodax-watchdog.timer).

    python -m scripts.watchdog            # check and alert on Telegram
    python -m scripts.watchdog --dry-run  # print what it would send
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.config import load_secrets, load_settings
from agent.ops.telegram_send import send_text
from agent.ops.watchdog import decide, detect, snapshot

STATE = Path("data/watchdog_state.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    now = datetime.now(timezone.utc)
    snap = snapshot(s, now)
    problems = detect(snap, s.watchdog)
    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except (OSError, ValueError):
        state = {}
    msgs, new_state = decide(problems, state, now, s.watchdog)
    print(f"satpam {now:%Y-%m-%d %H:%M} UTC: mode={snap.mode} service={snap.service_state} "
          f"positions={snap.open_positions} problems={[p.key for p in problems] or 'none'}")
    token, chat = sec.TELEGRAM_BOT_TOKEN.get_secret_value(), sec.TELEGRAM_CHAT_ID
    for m in msgs:
        if args.dry_run or not (token and chat):
            print(m)
        elif not send_text(token, chat, m):
            print("telegram send failed", file=sys.stderr)
            return 1   # keep the old state so the alert is retried next run
    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(new_state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
