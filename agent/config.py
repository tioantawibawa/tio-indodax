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
from datetime import date
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
    environment: str = Field("production", pattern="^(production|demo)$")
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

    @model_validator(mode="after")
    def _environment_matches_urls(self) -> "ExchangeSettings":
        urls = (self.public_base_url, self.tapi_url)
        on_demo = all("demo-indodax.com" in u for u in urls)
        if self.environment == "demo" and not on_demo:
            raise ValueError("environment=demo requires demo-indodax.com URLs")
        if self.environment == "production" and any("demo" in u for u in urls):
            raise ValueError("environment=production must not point at a demo host")
        return self


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
    # paper mode: from this date, send /status + a go-live readiness check to Telegram at
    # go_live_review_time (repeated daily until ready). None = disabled.
    go_live_review_date: date | None = None
    go_live_review_time: str = "09:00"

    @field_validator("daily_report_time", "go_live_review_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM")
        return v


class StrategySettings(_Frozen):
    """Tunable strategy parameters. NOT hard limits: the risk manager caps
    whatever the strategy proposes. Values pre-registered in
    docs/backtest_phase3.md before testing."""

    timeframes: tuple[str, ...] = ("1D",)        # first = signal/timeline timeframe
    lookback_bars: int = Field(300, ge=60, le=1000)
    # trend_follow
    breakout_bars: int = Field(20, ge=2)
    trend_ema: int = Field(100, ge=2)
    atr_period: int = Field(20, ge=2)
    chandelier_bars: int = Field(22, ge=2)
    chandelier_atr_mult: float = Field(3.0, gt=0)
    target_atr_mult: float = Field(4.0, gt=0)     # expected-move reference for the cost check only
    risk_per_trade_pct: float = Field(1.0, gt=0, le=5)  # of agent capital, lost if SL hits
    min_confidence: float = Field(0.55, ge=0, le=1)
    # regime classification (reporting / LLM context)
    ema_fast: int = Field(20, ge=2)
    ema_slow: int = Field(50, ge=3)
    rsi_period: int = Field(14, ge=2)
    adx_period: int = Field(14, ge=2)
    adx_trend: float = Field(25, gt=0)
    adx_range: float = Field(20, gt=0)
    high_vol_atr_ratio: float = Field(2.0, gt=1)

    @model_validator(mode="after")
    def _ordering(self) -> "StrategySettings":
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")
        if self.adx_range > self.adx_trend:
            raise ValueError("adx_range must be <= adx_trend")
        if not self.timeframes:
            raise ValueError("at least one timeframe required")
        for tf in self.timeframes:
            if tf not in ("1", "15", "30", "60", "240", "1D"):
                raise ValueError(f"unsupported timeframe {tf}")
        need = max(self.trend_ema, self.breakout_bars, self.chandelier_bars, self.ema_slow) + 10
        if self.lookback_bars < need:
            raise ValueError(f"lookback_bars must be >= {need} for the configured indicators")
        return self


class LLMSettings(_Frozen):
    model: str = "claude-opus-5"
    effort: str = Field("low", pattern="^(low|medium|high|xhigh|max)$")
    timeout_s: float = Field(30, gt=0)
    max_confidence_adjust: float = Field(0.2, ge=0, le=0.5)  # LLM can move confidence by at most this
    veto_confidence: float = Field(0.7, gt=0, le=1)          # bearish with >= this confidence vetoes a buy


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
    strategy: StrategySettings = StrategySettings()
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
    AGENT_SETTINGS: str = "config/settings.yaml"   # config/settings.demo.yaml for the demo account
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
