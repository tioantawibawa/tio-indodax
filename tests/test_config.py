import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent.config import (
    LIVE_CONFIRM_PHRASE,
    Mode,
    Secrets,
    Settings,
    env_file_permission_problem,
    load_secrets,
    load_settings,
)

ROOT = Path(__file__).resolve().parent.parent


def raw() -> dict:
    return yaml.safe_load((ROOT / "config/settings.yaml").read_text())


def test_repo_settings_load():
    s = load_settings(ROOT / "config/settings.yaml")
    assert s.market.whitelist == ("btc_idr", "eth_idr", "sol_idr")
    assert s.risk.max_open_positions == 3
    assert s.risk.require_stop_loss is True


def test_hard_limits_are_immutable():
    s = load_settings(ROOT / "config/settings.yaml")
    with pytest.raises(ValidationError):
        s.risk.max_position_pct = 50  # type: ignore[misc]
    with pytest.raises(ValidationError):
        s.market.whitelist = ("doge_idr",)  # type: ignore[misc]


def test_unknown_key_rejected():
    d = raw(); d["risk"]["max_positon_pct"] = 10  # typo
    with pytest.raises(ValidationError):
        Settings.model_validate(d)


@pytest.mark.parametrize("key,value", [
    ("max_position_pct", 0),
    ("max_position_pct", 150),
    ("max_position_pct", 40),        # > max_asset_exposure_pct
    ("daily_loss_limit_pct", 20),    # >= max_drawdown_pct
    ("max_orders_per_hour", 100),    # > per day
    ("require_stop_loss", False),
    ("agent_capital_idr", -1),
])
def test_invalid_risk_limits_rejected(key, value):
    d = raw(); d["risk"][key] = value
    with pytest.raises(ValidationError):
        Settings.model_validate(d)


@pytest.mark.parametrize("wl", [[], ["btcidr"], ["btc_idr", "btc_idr"], ["BTC_IDR"]])
def test_invalid_whitelist_rejected(wl):
    d = raw(); d["market"]["whitelist"] = wl
    with pytest.raises(ValidationError):
        Settings.model_validate(d)


def test_deadman_countdown_must_cover_heartbeats():
    d = raw(); d["deadman"] = {"enabled": True, "heartbeat_s": 60, "countdown_ms": 120000}
    with pytest.raises(ValidationError):
        Settings.model_validate(d)


def test_bad_report_time():
    d = raw(); d["reporting"]["daily_report_time"] = "25:00"
    with pytest.raises(ValidationError):
        Settings.model_validate(d)


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k in Secrets.model_fields:
            monkeypatch.delenv(k)
    return monkeypatch


def test_secrets_default_paper_mode(clean_env):
    s = Secrets()
    assert s.MODE == Mode.PAPER
    assert s.all_secret_values() == []


def test_live_requires_confirm_phrase(clean_env):
    clean_env.setenv("MODE", "live")
    clean_env.setenv("INDODAX_API_KEY", "k" * 10)
    clean_env.setenv("INDODAX_API_SECRET", "s" * 10)
    with pytest.raises(ValidationError, match="LIVE_CONFIRM"):
        Secrets()
    clean_env.setenv("LIVE_CONFIRM", "yes")
    with pytest.raises(ValidationError):
        Secrets()
    clean_env.setenv("LIVE_CONFIRM", LIVE_CONFIRM_PHRASE)
    assert Secrets().MODE == Mode.LIVE


def test_live_requires_keys(clean_env):
    clean_env.setenv("MODE", "live")
    clean_env.setenv("LIVE_CONFIRM", LIVE_CONFIRM_PHRASE)
    with pytest.raises(ValidationError, match="INDODAX_API_KEY"):
        Secrets()


def test_llm_enabled_requires_key(clean_env):
    clean_env.setenv("LLM_ENABLED", "true")
    with pytest.raises(ValidationError):
        Secrets()


def test_secrets_not_in_repr(clean_env):
    clean_env.setenv("INDODAX_API_SECRET", "supersecretvalue123")
    s = Secrets()
    assert "supersecretvalue123" not in repr(s)
    assert "supersecretvalue123" not in str(s.model_dump())
    assert s.all_secret_values() == ["supersecretvalue123"]


def test_load_secrets_from_env_file(clean_env, tmp_path):
    f = tmp_path / ".env"
    f.write_text("MODE=backtest\nTELEGRAM_CHAT_ID=12345\n")
    s = load_secrets(f)
    assert s.MODE == Mode.BACKTEST and s.TELEGRAM_CHAT_ID == "12345"


def test_env_file_permission_check(tmp_path):
    f = tmp_path / ".env"
    f.write_text("X=1")
    f.chmod(0o644)
    assert "chmod 600" in env_file_permission_problem(f)
    f.chmod(0o600)
    assert env_file_permission_problem(f) is None
    assert env_file_permission_problem(tmp_path / "missing") is None
