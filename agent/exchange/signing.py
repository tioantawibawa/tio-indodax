"""Request signing for Indodax private endpoints.

- Legacy private API (``/tapi``), Deadman Switch, private-WS token:
  ``Sign = hex(HMAC-SHA512(secret, body))``, API key in header ``Key``.
- Trade API 2.0: ``Sign = hex(HMAC-SHA256(secret, query_or_body))``,
  API key in header ``X-APIKEY``.

The signed string must be byte-for-byte what is sent, so callers must build
the body once with :func:`encode_params` and send exactly that string.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode


def encode_params(params: Mapping[str, Any]) -> str:
    """URL-encode params preserving insertion order."""
    return urlencode([(k, _fmt(v)) for k, v in params.items()])


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def sign_sha512(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512).hexdigest()


def sign_sha256(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class Clock:
    """Millisecond timestamps corrected by the measured server offset.

    ``offset_ms = server_time - local_time``. Indodax rejects a request whose
    timestamp is >= server_time + 1000 ms, so a fast local clock is the
    dangerous case.
    """

    def __init__(self, offset_ms: int = 0, now=time.time):
        self.offset_ms = offset_ms
        self._now = now

    def timestamp_ms(self) -> int:
        return int(self._now() * 1000) + self.offset_ms


class NonceGenerator:
    """Strictly increasing integer nonce (for endpoints used with ``nonce``)."""

    def __init__(self, clock: Clock | None = None):
        self._clock = clock or Clock()
        self._last = 0

    def next(self) -> int:
        n = max(self._clock.timestamp_ms(), self._last + 1)
        self._last = n
        return n
