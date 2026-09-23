"""Find your TELEGRAM_CHAT_ID.

1. Put TELEGRAM_BOT_TOKEN in .env
2. Send any message (e.g. /start) to your bot from your own Telegram account
3. Run: python -m scripts.telegram_chat_id
Prints the chat id(s) that recently messaged the bot. Copy yours into .env.
"""

from __future__ import annotations

import sys

import httpx

from agent.config import load_secrets


def main() -> int:
    token = load_secrets(".env").TELEGRAM_BOT_TOKEN.get_secret_value()
    if not token:
        print("TELEGRAM_BOT_TOKEN kosong di .env")
        return 1
    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
    seen = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat:
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not seen:
        print("Belum ada pesan. Kirim /start ke bot Anda lalu jalankan ulang.")
        return 1
    for cid, name in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
