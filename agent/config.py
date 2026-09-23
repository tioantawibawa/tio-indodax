"""Configuration loading.

Two sources:
- ``config/settings.yaml``: non-secret settings, including the hard risk limits.
- ``.env`` / environment: secrets and the operating mode.

All models are frozen (immutable) and reject unknown keys, so a typo in a
hard-limit name fails loudly at startup instead of silently using a default.
"""

from __future__ import annotations

import os
import re
import stat
from enum import Enum
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISK"

_PAIR_RE = re.compile(r"^[a-z0-9]+_(idr|usdt|btc)$")

Pct = Annotated[float, Field(gt=0, le=100)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExchangeSettings(_Frozen):
    public_base_url: str = "https://indodax.com"
    tapi_url: str = "https://indodax.com/tapi"
    tapi_v2_base_url: str = "https://api.indodax.com"
    public_rate_limit_per_min: int = Field(150, gt=0, le=180)
    request_timeout_s: float = Field(10, gt=0)
    max_retries: int = Field(4, ge=0, le=10)
    backoff_base_s: float = Field(0.5, gt=0)
    backoff_max_s: float = Field(8, gt=0)
    recv_window_ms: int = Field(5000, gt=0, le=60000)
    max_clock_offset_ms: int = Field(500, gt=0)


class MarketSettings(_Frozen):
    whitelist: tuple[str, ...]
    min_volume_24h_idr: float = Field(gt=0)
    max_spread_pct: Pct
    max_slippage_pct: Pct

    @field_validator("whitelist")
    @classmethod
    def _check_pairs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("whitelist must not be empty")
        for p in v:
            if not _PAIR_RE.match(p):
                raise ValueError(f"invalid pair {p!r}; expected format like 'btc_idr'")
        if len(set(v)) != len(v):
            raise ValueError("whitelist contains duplicates")
        return v


class CycleSettings(_Frozen):
    interval_minutes: int = Field(5, ge=1, le=240)


class FeeSettings(_Frozen):
    maker_pct: float = Field(ge=0, lt=5)
    taker_pct: float = Field(ge=0, lt=5)
    tax_pct: float = Field(ge=0, lt=5)
    clearing_pct: float = Field(ge=0, lt=5)
    min_edge_multiple: float = Field(2.0, ge=1)


class RiskLimits(_Frozen):
    """Hard limits. Enforced by risk_manager, never by the LLM."""

    agent_capital_idr: float = Field(gt=0)
    max_position_pct: Pct
    max_open_positions: int = Field(ge=1, le=50)
    max_asset_exposure_pct: Pct
    daily_loss_limit_pct: Pct
    max_drawdown_pct: Pct
    max_orders_per_hour: int = Field(ge=1)
    max_orders_per_day: int = Field(ge=1)
    stoploss_cooldown_minutes: int = Field(ge=0)
    require_stop_loss: bool = True
    emergency_exit_max_slippage_pct: Pct

    @field_validator("require_stop_loss")
    @classmethod
    def _sl_mandatory(cls, v: bool) -> bool:
        if not v:
            raise ValueError("require_stop_loss cannot be disabled")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "RiskLimits":
        if self.max_position_pct > self.max_asset_exposure_pct:
            raise ValueError("max_position_pct must be <= max_asset_exposure_pct")
        if self.daily_loss_limit_pct >= self.max_drawdown_pct:
            raise ValueError("daily_loss_limit_pct must be < max_drawdown_pct")
        if self.max_orders_per_hour > self.max_orders_per_day:
            raise ValueError("max_orders_per_hour must be <= max_orders_per_day")
        return self


class DeadmanSettings(_Frozen):
    enabled: bool = True
    heartbeat_s: int = Field(30, ge=5)
    countdown_ms: int = Field(120000, ge=10000)

    @model_validator(mode="after")
    def _heartbeat_shorter_than_countdown(self) -> "DeadmanSettings":
        # At least 3 heartbeats must fit into one countdown window.
        if self.heartbeat_s * 1000 * 3 > self.countdown_ms:
            raise ValueError("countdown_ms must be >= 3 * heartbeat_s")
        return self


class LiveGateSettings(_Frozen):
    min_paper_days: int = Field(14, ge=0)


class ReportingSettings(_Frozen):
    timezone: str = "Asia/Jakarta"
    daily_report_time: str = "21:00"

    @field_validator("daily_report_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("daily_report_time must be HH:MM")
        return v


class LLMSettings(_Frozen):
    model: str = "claude-sonnet-5"
    timeout_s: float = Field(20, gt=0)


class StorageSettings(_Frozen):
    db_path: str = "data/agent.db"


class LoggingSettings(_Frozen):
    dir: str = "logs"
    level: str = "INFO"


class Settings(_Frozen):
    exchange: ExchangeSettings = ExchangeSettings()
    market: MarketSettings
    cycle: CycleSettings = CycleSettings()
    fees: FeeSettings
    risk: RiskLimits
    deadman: DeadmanSettings = DeadmanSettings()
    live_gate: LiveGateSettings = LiveGateSettings()
    reporting: ReportingSettings = ReportingSettings()
    llm: LLMSettings = LLMSettings()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Secrets(BaseSettings):
    """Secrets and mode from environment / .env. Values are SecretStr so they
    never appear in repr() or logs."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    MODE: Mode = Mode.PAPER
    LIVE_CONFIRM: str = ""
    INDODAX_API_KEY: SecretStr = SecretStr("")
    INDODAX_API_SECRET: SecretStr = SecretStr("")
    INDODAX_V2_API_KEY: SecretStr = SecretStr("")
    INDODAX_V2_API_SECRET: SecretStr = SecretStr("")
    TELEGRAM_BOT_TOKEN: SecretStr = SecretStr("")
    TELEGRAM_CHAT_ID: str = ""
    LLM_ENABLED: bool = False
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def _live_requires_confirmation(self) -> "Secrets":
        if self.MODE == Mode.LIVE:
            if self.LIVE_CONFIRM != LIVE_CONFIRM_PHRASE:
                raise ValueError(
                    f"MODE=live requires LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} in .env"
                )
            if not (self.INDODAX_API_KEY.get_secret_value() and self.INDODAX_API_SECRET.get_secret_value()):
                raise ValueError("MODE=live requires INDODAX_API_KEY and INDODAX_API_SECRET")
        if self.LLM_ENABLED and not self.ANTHROPIC_API_KEY.get_secret_value():
            raise ValueError("LLM_ENABLED=true requires ANTHROPIC_API_KEY")
        return self

    def all_secret_values(self) -> list[str]:
        """Non-empty secret strings, used by the log redactor."""
        vals = [
            self.INDODAX_API_KEY, self.INDODAX_API_SECRET,
            self.INDODAX_V2_API_KEY, self.INDODAX_V2_API_SECRET,
            self.TELEGRAM_BOT_TOKEN, self.ANTHROPIC_API_KEY,
        ]
        return [v.get_secret_value() for v in vals if v.get_secret_value()]


def load_settings(path: str | os.PathLike = "config/settings.yaml") -> Settings:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Settings.model_validate(raw)


def load_secrets(env_file: str | os.PathLike | None = ".env") -> Secrets:
    if env_file is not None and Path(env_file).exists():
        return Secrets(_env_file=env_file)  # type: ignore[call-arg]
    return Secrets()


def env_file_permission_problem(path: str | os.PathLike) -> str | None:
    """Return a description of the problem if ``path`` is readable by group/others."""
    p = Path(path)
    if not p.exists():
        return None
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{p} has permissions {oct(mode)}; expected 0o600 (run: chmod 600 {p})"
    return None
