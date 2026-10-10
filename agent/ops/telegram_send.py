"""Minimal Telegram sender for out-of-process jobs (Satpam, backup). No polling, never logs the token."""

from __future__ import annotations

from pathlib import Path

import httpx

from agent.reporting.telegram_bot import split_message

API = "https://api.telegram.org/bot{token}/{method}"


def send_text(token: str, chat_id: str, text: str, timeout: float = 15) -> bool:
    ok = True
    for chunk in split_message(text):
        try:
            r = httpx.post(API.format(token=token, method="sendMessage"), timeout=timeout,
                           data={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                                 "disable_web_page_preview": "true"})
            ok &= r.status_code == 200
        except httpx.HTTPError:
            ok = False
    return ok


def send_document(token: str, chat_id: str, path: Path, caption: str, timeout: float = 60) -> bool:
    try:
        with path.open("rb") as f:
            r = httpx.post(API.format(token=token, method="sendDocument"), timeout=timeout,
                           data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                           files={"document": (path.name, f, "application/gzip")})
        return r.status_code == 200
    except httpx.HTTPError:
        return False
