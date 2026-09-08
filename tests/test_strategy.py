from pathlib import Path

import pandas as pd

from strategy import Config, position_quantity, validate_bars


def config() -> Config:
    return Config(
        symbol="BTCUSDT", start="2021-01-01", end="2026-01-01",
        timezone="Europe/London", data_directory=Path("data"),
        lookback_minutes=60, entry_start="08:00", entry_end="11:00", close_time="16:00",
        weekdays_only=True, starting_equity_usd=150_000, risk_per_trade=.0005,
        stop_distance_fraction=.005, commission_bps=0, slippage_bps=0,
        synthetic_short_sales=True,
    )


def test_position_size_scales_with_equity():
    base = config()
    assert position_quantity(150_000, 60_000, base) == .25
    assert position_quantity(180_000, 60_000, base) == .30


def test_position_size_falls_when_stop_distance_rises():
    base = config()
    assert position_quantity(150_000, 100_000, base) < position_quantity(150_000, 50_000, base)


def test_validation_rejects_invalid_ohlc():
    index = pd.DatetimeIndex(["2024-01-01T00:00:00Z"])
    bars = pd.DataFrame({"open": [10], "high": [9], "low": [8], "close": [10]}, index=index)
    try:
        validate_bars(bars)
    except ValueError as error:
        assert "Invalid OHLC" in str(error)
    else:
        raise AssertionError("Invalid bar was accepted")

