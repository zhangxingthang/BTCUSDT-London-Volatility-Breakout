"""BTCUSDT London volatility-breakout backtest.

The engine processes one-minute candles in time order. Entry levels use only
completed candles. Stops are checked before the trailing stop is updated.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo
import zipfile

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml


UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class Config:
    symbol: str
    start: str
    end: str
    timezone: str
    data_directory: Path
    lookback_minutes: int
    entry_start: str
    entry_end: str
    close_time: str
    weekdays_only: bool
    starting_equity_usd: float
    risk_per_trade: float
    stop_distance_fraction: float
    commission_bps: float
    slippage_bps: float
    synthetic_short_sales: bool


def load_config(path: str | Path = "config/params.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Config(
        symbol=raw["symbol"],
        start=str(raw["start"]),
        end=str(raw["end"]),
        timezone=raw["timezone"],
        data_directory=Path(raw["data"]["directory"]),
        lookback_minutes=int(raw["strategy"]["lookback_minutes"]),
        entry_start=raw["strategy"]["entry_start"],
        entry_end=raw["strategy"]["entry_end"],
        close_time=raw["strategy"]["close_time"],
        weekdays_only=bool(raw["strategy"]["weekdays_only"]),
        starting_equity_usd=float(raw["risk"]["starting_equity_usd"]),
        risk_per_trade=float(raw["risk"]["risk_per_trade"]),
        stop_distance_fraction=float(raw["risk"]["stop_distance_fraction"]),
        commission_bps=float(raw["costs"]["commission_bps"]),
        slippage_bps=float(raw["costs"]["slippage_bps"]),
        synthetic_short_sales=bool(raw["execution"]["synthetic_short_sales"]),
    )


def load_binance_minutes(directory: str | Path, start: str, end: str) -> pd.DataFrame:
    """Load Binance monthly kline ZIPs and return validated UTC OHLC bars."""
    columns = ["open_time", "open", "high", "low", "close", "volume", "close_time",
               "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
    parts: list[pd.DataFrame] = []
    for path in sorted(Path(directory).glob("BTCUSDT-1m-*.zip")):
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"{path}: expected one CSV, found {len(names)}")
            with archive.open(names[0]) as stream:
                frame = pd.read_csv(stream, header=None, names=columns)
        unit = "us" if int(frame.open_time.iloc[0]) > 10**14 else "ms"
        frame.index = pd.to_datetime(frame.pop("open_time"), unit=unit, utc=True)
        parts.append(frame[["open", "high", "low", "close"]].astype(float))
    if not parts:
        raise FileNotFoundError(f"No BTCUSDT monthly ZIP files found in {directory}")
    bars = pd.concat(parts).sort_index()
    begin, finish = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    return validate_bars(bars.loc[(bars.index >= begin) & (bars.index < finish)])


def validate_bars(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        raise ValueError("No bars in the requested period")
    if not bars.index.is_monotonic_increasing:
        raise ValueError("Timestamps are not ordered")
    if bars.index.has_duplicates:
        duplicated = bars[bars.index.duplicated(keep=False)]
        conflicts = duplicated.groupby(level=0).nunique().max(axis=1).gt(1)
        if conflicts.any():
            raise ValueError("Conflicting duplicate timestamps")
        bars = bars.loc[~bars.index.duplicated(keep="first")].copy()
    invalid = ((bars.high < bars[["open", "close"]].max(axis=1)) |
               (bars.low > bars[["open", "close"]].min(axis=1)) |
               (bars.high < bars.low) | (bars <= 0).any(axis=1))
    if invalid.any():
        raise ValueError(f"Invalid OHLC rows: {int(invalid.sum())}")
    return bars


def position_quantity(equity: float, entry_price: float, config: Config) -> float:
    """BTC quantity whose initial stop equals the configured equity risk."""
    stop_distance = entry_price * config.stop_distance_fraction
    return equity * config.risk_per_trade / stop_distance


def _stop_fill(open_price: float, stop_price: float, side: int, entry: bool) -> float:
    if entry:
        return max(open_price, stop_price) if side == 1 else min(open_price, stop_price)
    return min(open_price, stop_price) if side == 1 else max(open_price, stop_price)


def run_backtest(bars: pd.DataFrame, config: Config) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Run the fixed strategy and return trades, daily equity and skipped days."""
    london = ZoneInfo(config.timezone)
    local = bars.copy()
    local["date"] = local.index.tz_convert(london).date
    equity = config.starting_equity_usd
    trades: list[dict] = []
    skipped: list[dict] = []
    daily: list[dict] = []

    for date, raw_day in local.groupby("date", sort=True):
        if config.weekdays_only and pd.Timestamp(date).weekday() >= 5:
            continue
        day = raw_day.drop(columns="date")
        midnight = pd.Timestamp(date, tz=london)
        start = midnight + pd.Timedelta(hours=int(config.entry_start[:2]))
        entry_end = midnight + pd.Timedelta(hours=int(config.entry_end[:2]))
        close = midnight + pd.Timedelta(hours=int(config.close_time[:2]))
        local_index = day.index.tz_convert(london)
        required = pd.date_range(midnight + pd.Timedelta(hours=7), close,
                                 freq="1min", inclusive="left")
        actual = local_index[(local_index >= required[0]) & (local_index < close)]
        if not actual.equals(required):
            skipped.append({"date": str(date), "reason": "incomplete_session"})
            continue

        rolling_high = day.high.rolling(config.lookback_minutes).max().shift(1)
        rolling_low = day.low.rolling(config.lookback_minutes).min().shift(1)
        session = day[(local_index >= start) & (local_index < close)]
        position = 0
        entry_price = quantity = trailing_stop = favourable = np.nan
        entry_time = None
        exit_price = exit_time = exit_reason = None

        for timestamp, bar in session.iterrows():
            local_time = timestamp.tz_convert(london)
            if position == 0:
                if local_time >= entry_end:
                    continue
                buy_stop, sell_stop = rolling_high.loc[timestamp], rolling_low.loc[timestamp]
                hit_buy = bar.high >= buy_stop
                hit_sell = bar.low <= sell_stop
                if hit_buy and hit_sell:
                    skipped.append({"date": str(date), "reason": "dual_entry", "time": timestamp.isoformat()})
                    position = 99
                    break
                if not hit_buy and not hit_sell:
                    continue
                position = 1 if hit_buy else -1
                trigger = buy_stop if position == 1 else sell_stop
                entry_price = _stop_fill(float(bar.open), float(trigger), position, entry=True)
                distance = entry_price * config.stop_distance_fraction
                quantity = position_quantity(equity, entry_price, config)
                trailing_stop = entry_price - position * distance
                favourable = entry_price
                entry_time = timestamp
                stop_hit = bar.low <= trailing_stop if position == 1 else bar.high >= trailing_stop
                if stop_hit:
                    exit_price = _stop_fill(float(bar.open), trailing_stop, position, entry=False)
                    exit_time, exit_reason = timestamp, "initial_stop"
                    break
                favourable = max(favourable, float(bar.high)) if position == 1 else min(favourable, float(bar.low))
                continue

            stop_hit = bar.low <= trailing_stop if position == 1 else bar.high >= trailing_stop
            if stop_hit:
                exit_price = _stop_fill(float(bar.open), trailing_stop, position, entry=False)
                exit_time, exit_reason = timestamp, "trailing_stop"
                break
            favourable = max(favourable, float(bar.high)) if position == 1 else min(favourable, float(bar.low))
            distance = entry_price * config.stop_distance_fraction
            candidate = favourable - position * distance
            trailing_stop = max(trailing_stop, candidate) if position == 1 else min(trailing_stop, candidate)

        if position in (1, -1) and exit_price is None:
            exit_price, exit_time, exit_reason = float(session.close.iloc[-1]), session.index[-1], "session_close"

        if position in (1, -1):
            gross_pnl = position * quantity * (float(exit_price) - entry_price)
            turnover = quantity * (entry_price + float(exit_price))
            costs = turnover * (config.commission_bps + config.slippage_bps) / 10_000
            net_pnl = gross_pnl - costs
            starting_equity = equity
            equity += net_pnl
            trades.append({
                "date_london": str(date), "side": "long" if position == 1 else "short",
                "entry_time_utc": entry_time.isoformat(), "exit_time_utc": exit_time.isoformat(),
                "entry_price": entry_price, "exit_price": float(exit_price), "units": quantity,
                "initial_risk_usd": starting_equity * config.risk_per_trade,
                "gross_pnl_usd": gross_pnl, "costs_usd": costs, "pnl_usd": net_pnl,
                "ending_equity_usd": equity, "exit_reason": exit_reason,
            })
        daily.append({"date": pd.Timestamp(date, tz="UTC"), "equity_usd": equity})

    trades_frame = pd.DataFrame(trades)
    skipped_frame = pd.DataFrame(skipped)
    equity_series = pd.DataFrame(daily).drop_duplicates("date").set_index("date").equity_usd
    calendar = pd.date_range(config.start, pd.Timestamp(config.end) - pd.Timedelta(days=1), freq="B", tz="UTC")
    equity_series = equity_series.reindex(calendar).ffill().fillna(config.starting_equity_usd)
    return trades_frame, equity_series, skipped_frame


def performance(trades: pd.DataFrame, equity: pd.Series, config: Config) -> dict:
    daily_returns = equity.pct_change().fillna(0)
    years = (pd.Timestamp(config.end) - pd.Timestamp(config.start)).days / 365.2425
    drawdown = equity / equity.cummax() - 1
    wins = trades.loc[trades.pnl_usd > 0, "pnl_usd"]
    losses = trades.loc[trades.pnl_usd < 0, "pnl_usd"]
    volatility = daily_returns.std(ddof=1)
    return {
        "ending_equity_usd": float(equity.iloc[-1]),
        "total_return": float(equity.iloc[-1] / config.starting_equity_usd - 1),
        "cagr": float((equity.iloc[-1] / config.starting_equity_usd) ** (1 / years) - 1),
        "max_drawdown": float(drawdown.min()),
        "sharpe_0rf": float(daily_returns.mean() / volatility * math.sqrt(252)),
        "trades": int(len(trades)),
        "win_rate": float((trades.pnl_usd > 0).mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())),
    }


def save_results(trades: pd.DataFrame, equity: pd.Series, skipped: pd.DataFrame,
                 metrics: dict, output: str | Path = "results") -> None:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    trades.to_csv(output / "trades.csv", index=False)
    equity.rename("equity_usd").to_csv(output / "daily_equity.csv")
    skipped.to_csv(output / "skipped_days.csv", index=False)
    (output / "summary.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    dd = (equity / equity.cummax() - 1) * 100
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1])
    axes[0].plot(equity.index, equity, color="#0b7285")
    axes[0].set_ylabel("Equity (USD)")
    axes[1].fill_between(dd.index, dd.values, 0, color="#d1495b", alpha=.8)
    axes[1].set_ylabel("Drawdown %")
    axes[1].set_xlabel("Date")
    for axis in axes:
        axis.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(output / "equity_drawdown.png", dpi=170)
    plt.close(fig)


def main() -> None:
    config = load_config()
    bars = load_binance_minutes(config.data_directory, config.start, config.end)
    trades, equity, skipped = run_backtest(bars, config)
    metrics = performance(trades, equity, config)
    save_results(trades, equity, skipped, metrics)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()

