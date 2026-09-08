import math

import pandas as pd

from fx_backtest.london_breakout import BreakoutConfig, _day_status, backtest_symbol, position_units, stop_distance


def _session(date="2024-06-03", base=1.1000):
    local = pd.date_range(f"{date} 07:00", f"{date} 15:55", freq="5min", tz="Europe/London")
    utc = local.tz_convert("UTC")
    return pd.DataFrame({"open": base, "high": base + .0001, "low": base - .0001, "close": base}, index=utc)


def test_risk_sizing_matches_point_zero_two_percent():
    units = position_units(150_000, .0005, .0002)
    assert units == 60_000
    assert math.isclose(units * .0005, 30.0)


def test_risk_sizing_scales_with_current_equity_at_point_zero_five_percent():
    assert position_units(150_000, 300, 0.0005) == 0.25
    assert position_units(180_000, 300, 0.0005) == 0.30


def test_btc_stop_is_half_percent_and_fx_is_five_pips():
    config = BreakoutConfig()
    assert stop_distance("EURUSD", 1.2, config) == .0005
    assert stop_distance("BTCUSD", 60_000, config) == 300
    assert stop_distance("BTCUSDT", 60_000, config) == 300


def test_session_validation_handles_london_dst():
    config = BreakoutConfig()
    summer = _session("2024-06-03")
    winter = _session("2024-01-08")
    assert summer.index[0].hour == 6
    assert winter.index[0].hour == 7
    assert _day_status(summer, config)[0]
    assert _day_status(winter, config)[0]


def test_one_minute_session_and_previous_hour_are_supported():
    config = BreakoutConfig(bar_minutes=1, lookback_bars=60)
    local = pd.date_range("2024-06-03 07:00", "2024-06-03 15:59", freq="1min", tz="Europe/London")
    bars = pd.DataFrame({"open": 1.1, "high": 1.1001, "low": 1.0999, "close": 1.1},
                        index=local.tz_convert("UTC"))
    assert _day_status(bars, config)[0]


def test_one_trade_per_day_and_no_future_bar_in_entry_level():
    config = BreakoutConfig(start="2024-06-03", end="2024-06-04")
    bars = _session()
    trigger_time = pd.Timestamp("2024-06-03 08:00", tz="Europe/London").tz_convert("UTC")
    bars.loc[trigger_time, ["open", "high", "low", "close"]] = [1.1000, 1.1003, 1.1000, 1.1002]
    result = backtest_symbol(bars, "EURUSD", config)
    assert len(result["trades"]) == 1
    assert result["trades"].iloc[0].entry_price == 1.1001


def test_dual_entry_candle_is_skipped_not_guessed():
    config = BreakoutConfig(start="2024-06-03", end="2024-06-04")
    bars = _session()
    trigger_time = pd.Timestamp("2024-06-03 08:00", tz="Europe/London").tz_convert("UTC")
    bars.loc[trigger_time, ["high", "low"]] = [1.1003, 1.0997]
    result = backtest_symbol(bars, "EURUSD", config)
    assert result["trades"].empty
    assert "ambiguous_dual_entry" in set(result["skipped"].reason)


def test_declared_calendar_is_preserved_when_session_is_missing():
    config = BreakoutConfig(start="2024-06-03", end="2024-06-05")
    incomplete = _session().drop(_session().index[20])
    result = backtest_symbol(incomplete, "EURUSD", config)
    assert list(result["daily_equity"]) == [150_000, 150_000]

