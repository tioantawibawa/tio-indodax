"""Telegram: notifier (alerts/reports) and command router.

- Only the configured TELEGRAM_CHAT_ID is served; messages from any other
  chat are ignored (and logged without their content).
- Messages use HTML parse mode; every dynamic value must go through
  :func:`esc`. Long messages are split below Telegram's 4096-char limit.
- The router is plain async Python so it can be tested without Telegram.
"""

from __future__ import annotations

import html
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)
TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900


def esc(v: object) -> str:
    return html.escape(str(v), quote=False)


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split on line boundaries; hard-split lines longer than ``limit``."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks or [""]


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class NullNotifier:
    async def send(self, text: str) -> None:
        log.info("notify_disabled", preview=text[:80])


class RecordingNotifier:
    """Test double."""

    def __init__(self):
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.extend(split_message(text))


class TelegramNotifier:
    def __init__(self, bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id

    async def send(self, text: str) -> None:
        for chunk in split_message(text):
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=chunk, parse_mode="HTML",
                                            disable_web_page_preview=True)
            except Exception as e:  # noqa: BLE001 - never let Telegram break trading
                log.warning("telegram_send_failed", error_type=type(e).__name__, error=str(e)[:200])


HELP = (
    "<b>Perintah</b>\n"
    "/status — mode, status, equity, drawdown\n"
    "/positions — posisi terbuka + stop\n"
    "/report — laporan hari ini sekarang\n"
    "/pause — hentikan entry baru\n"
    "/resume — lanjutkan (juga setelah HALTED)\n"
    "/kill CONFIRM — batalkan semua order &amp; HALT"
)


class CommandRouter:
    """Maps chat commands to runner actions. Returns the reply text, or None
    when the message must be ignored (wrong chat)."""

    def __init__(self, runner, allowed_chat_id: int):
        self.runner = runner
        self.allowed = int(allowed_chat_id)

    async def handle(self, chat_id: int, text: str) -> str | None:
        if int(chat_id) != self.allowed:
            log.warning("telegram_unauthorized_chat", chat_id=chat_id)
            return None
        parts = (text or "").strip().split()
        if not parts or not parts[0].startswith("/"):
            return HELP
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return self.runner.status_text()
        if cmd == "/positions":
            return self.runner.positions_text()
        if cmd == "/report":
            return self.runner.report_text()
        if cmd == "/pause":
            return await self.runner.pause()
        if cmd == "/resume":
            return await self.runner.resume()
        if cmd == "/kill":
            if args != ["CONFIRM"]:
                return ("⚠️ /kill membatalkan semua order terbuka dan menghentikan agent (HALTED).\n"
                        "Posisi tetap dipegang dengan stop-loss-nya.\nKirim <code>/kill CONFIRM</code> untuk lanjut.")
            return await self.runner.kill()
        return "Perintah tidak dikenal.\n\n" + HELP


def build_application(token: str, router: CommandRouter):
    """python-telegram-bot Application wired to the router (polling)."""
    from telegram import Update
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    app = Application.builder().token(token).build()

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or chat is None:
            return
        reply = await router.handle(chat.id, msg.text or "")
        if reply is None:
            return
        for chunk in split_message(reply):
            await msg.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)

    app.add_handler(MessageHandler(filters.TEXT, on_message))
    return app
