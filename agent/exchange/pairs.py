"""Pair-name conversions.

Indodax uses different spellings of the same market depending on endpoint
(see docs/indodax_api_notes.md §2). The agent's canonical form is the
``ticker_id``: lower-case ``base_quote`` (e.g. ``btc_idr``).
"""

from __future__ import annotations

KNOWN_QUOTES = ("idr", "usdt", "btc")


def _split(pair: str) -> tuple[str, str]:
    p = pair.strip().lower().replace("/", "_").replace("-", "_")
    if "_" in p:
        base, _, quote = p.partition("_")
        if base and quote in KNOWN_QUOTES:
            return base, quote
        raise ValueError(f"unrecognised pair {pair!r}")
    for quote in KNOWN_QUOTES:
        if p.endswith(quote) and len(p) > len(quote):
            return p[: -len(quote)], quote
    raise ValueError(f"unrecognised pair {pair!r}")


def to_ticker_id(pair: str) -> str:
    """``BTCIDR`` / ``btcidr`` / ``btc_idr`` -> ``btc_idr`` (canonical)."""
    base, quote = _split(pair)
    return f"{base}_{quote}"


def to_pair_id(pair: str) -> str:
    """-> ``btcidr`` (used by /api/ticker, /api/depth, /api/trades, WS channels)."""
    base, quote = _split(pair)
    return f"{base}{quote}"


def to_symbol(pair: str) -> str:
    """-> ``BTCIDR`` (used by OHLC history and Trade API 2.0)."""
    return to_pair_id(pair).upper()


def base_asset(pair: str) -> str:
    return _split(pair)[0]


def quote_asset(pair: str) -> str:
    return _split(pair)[1]
