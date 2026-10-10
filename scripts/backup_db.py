"""Daily DB backup — run by systemd timer (deploy/indodax-backup.timer).

    python -m scripts.backup_db               # backup + retention + copy to Telegram (if enabled)
    python -m scripts.backup_db --no-telegram
"""

from __future__ import annotations

import argparse
import sys

from agent.config import load_secrets, load_settings
from agent.ops.backup import backup, restore_hint
from agent.ops.telegram_send import send_document, send_text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args(argv)
    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    w = s.watchdog
    token, chat = sec.TELEGRAM_BOT_TOKEN.get_secret_value(), sec.TELEGRAM_CHAT_ID
    try:
        gz, removed = backup(s.storage.db_path, w.backup_dir, w.backup_keep_days)
    except Exception as e:  # noqa: BLE001
        print(f"backup FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        if token and chat:
            send_text(token, chat, f"🚨 <b>Backup database GAGAL</b>: {type(e).__name__}")
        return 1
    kb = gz.stat().st_size / 1024
    print(f"backup ok: {gz} ({kb:.0f} KB), removed {removed} old")
    if w.backup_to_telegram and not args.no_telegram and token and chat:
        ok = send_document(token, chat, gz, f"💾 Backup database agent ({kb:.0f} KB, integritas OK).\n"
                                            + restore_hint(gz))
        print("sent to telegram" if ok else "telegram upload failed (local backup is fine)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
