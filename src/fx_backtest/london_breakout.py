"""Five-year London-session dynamic breakout research backtest.

The engine intentionally uses only completed 5-minute OHLC bars.  It never guesses
the path inside a candle: a candle that touches both pending entries is classified
as ambiguous and the day is skipped.  Stop checks occur before a completed bar can
tighten the trailing stop, which is a conservative no-look-ahead convention.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .metrics import drawdown


LONDON = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class BreakoutConfig:
    start: str = "2021-01-01"
    end: str = "2026-01-01"
    initial_equity_usd: float = 150_000.0
    risk_fraction: float = 0.0002  # 0.02%
    bar_minutes: int = 5
    lookback_bars: int = 12        # rolling one hour on 5-minute bars
    entry_start_hour: int = 8
    entry_end_hour: int = 11
    liquidation_hour: int = 16
    fx_stop_distance: float = 0.0005  # 5 pips for EURUSD and GBPUSD
    btc_stop_fraction: float = 0.005  # 0.5% of entry price
    symbols: tuple[str, str, str] = ("EURUSD", "GBPUSD", "BTCUSD")
    data_source_label: str = "Local MT5 broker-terminal historical 5-minute OHLC bars"


def load_bars(path: Path, start: str, end: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    frame = frame[list(required)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.set_index("timestamp").sort_index()
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    if frame.index.has_duplicates:
        raise ValueError(f"{path} contains duplicate timestamps")
    if not frame.index.is_monotonic_increasing:
        raise ValueError(f"{path} is not time ordered")
    invalid = ((frame["high"] < frame[["open", "close"]].max(axis=1)) |
               (frame["low"] > frame[["open", "close"]].min(axis=1)) |
               (frame["high"] < frame["low"]) | (frame <= 0).any(axis=1))
    if invalid.any():
        raise ValueError(f"{path} contains {int(invalid.sum())} invalid OHLC rows")
    begin, finish = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    frame = frame.loc[(frame.index >= begin) & (frame.index < finish)]
    if frame.empty:
        raise ValueError(f"{path} has no rows in the requested period")
    return frame


def stop_distance(symbol: str, entry: float, config: BreakoutConfig) -> float:
    return entry * config.btc_stop_fraction if symbol.startswith("BTC") else config.fx_stop_distance


def position_units(equity: float, distance: float, risk_fraction: float) -> float:
    """Exact fractional units whose initial stop loss equals the risk budget."""
    if equity <= 0 or distance <= 0 or not 0 < risk_fraction < 1:
        raise ValueError("Equity, stop distance and risk fraction must be positive")
    return equity * risk_fraction / distance


def _fill_at_stop(open_price: float, stop_price: float, side: int, entry: bool) -> float:
    """Apply adverse gap logic to a stop entry or stop loss."""
    if entry:
        return max(open_price, stop_price) if side == 1 else min(open_price, stop_price)
    return min(open_price, stop_price) if side == 1 else max(open_price, stop_price)


def _day_status(day: pd.DataFrame, config: BreakoutConfig) -> tuple[bool, str]:
    """Require the whole 07:00-16:00 London research window at five-minute spacing."""
    if day.empty:
        return False, "no_bars"
    local = day.index.tz_convert(LONDON)
    expected = pd.date_range(local[0].normalize() + pd.Timedelta(hours=7),
                             local[0].normalize() + pd.Timedelta(hours=16),
                             freq=f"{config.bar_minutes}min", inclusive="left")
    actual = local[(local >= expected[0]) &
                   (local < expected[-1] + pd.Timedelta(minutes=config.bar_minutes))]
    if len(actual) != len(expected) or not actual.equals(expected):
        return False, "incomplete_session"
    return True, "complete"


def backtest_symbol(bars: pd.DataFrame, symbol: str, config: BreakoutConfig) -> dict:
    local = bars.copy()
    local["london_date"] = local.index.tz_convert(LONDON).date
    equity = config.initial_equity_usd
    trades: list[dict] = []
    skipped: list[dict] = []
    equity_rows: list[dict] = []

    for trading_date, raw_day in local.groupby("london_date", sort=True):
        day = raw_day.drop(columns="london_date")
        midnight = pd.Timestamp(trading_date, tz=LONDON)
        if midnight.weekday() >= 5:
            continue
        complete, reason = _day_status(day, config)
        if not complete:
            skipped.append({"date": str(trading_date), "reason": reason})
            continue

        london_index = day.index.tz_convert(LONDON)
        entry_mask = ((london_index >= midnight + pd.Timedelta(hours=config.entry_start_hour)) &
                      (london_index < midnight + pd.Timedelta(hours=config.entry_end_hour)))
        trade_mask = ((london_index >= midnight + pd.Timedelta(hours=config.entry_start_hour)) &
                      (london_index < midnight + pd.Timedelta(hours=config.liquidation_hour)))
        rolling_high = day["high"].rolling(config.lookback_bars).max().shift(1)
        rolling_low = day["low"].rolling(config.lookback_bars).min().shift(1)

        position = 0
        entry_price = units = current_stop = favourable = np.nan
        entry_time = None
        initial_risk = np.nan
        exit_price = exit_time = exit_reason = None

        for timestamp, bar in day.loc[trade_mask].iterrows():
            if position == 0:
                if not entry_mask[day.index.get_loc(timestamp)]:
                    continue
                buy_stop, sell_stop = rolling_high.loc[timestamp], rolling_low.loc[timestamp]
                if pd.isna(buy_stop) or pd.isna(sell_stop):
                    continue
                hit_buy = bar["high"] >= buy_stop
                hit_sell = bar["low"] <= sell_stop
                if hit_buy and hit_sell:
                    skipped.append({"date": str(trading_date), "reason": "ambiguous_dual_entry",
                                    "timestamp": timestamp.isoformat()})
                    position = 99
                    break
                if not hit_buy and not hit_sell:
                    continue
                position = 1 if hit_buy else -1
                trigger = buy_stop if position == 1 else sell_stop
                entry_price = _fill_at_stop(float(bar["open"]), float(trigger), position, entry=True)
                distance = stop_distance(symbol, entry_price, config)
                units = position_units(equity, distance, config.risk_fraction)
                initial_risk = equity * config.risk_fraction
                current_stop = entry_price - position * distance
                favourable = entry_price
                entry_time = timestamp
                # Conservative same-bar rule: assume entry happened before an adverse stop touch.
                stop_hit = bar["low"] <= current_stop if position == 1 else bar["high"] >= current_stop
                if stop_hit:
                    exit_price = _fill_at_stop(float(bar["open"]), current_stop, position, entry=False)
                    exit_time, exit_reason = timestamp, "initial_stop_same_bar"
                    break
                favourable = max(favourable, float(bar["high"])) if position == 1 else min(favourable, float(bar["low"]))
                continue

            stop_hit = bar["low"] <= current_stop if position == 1 else bar["high"] >= current_stop
            if stop_hit:
                exit_price = _fill_at_stop(float(bar["open"]), current_stop, position, entry=False)
                exit_time, exit_reason = timestamp, "trailing_stop"
                break
            favourable = max(favourable, float(bar["high"])) if position == 1 else min(favourable, float(bar["low"]))
            distance = stop_distance(symbol, entry_price, config)
            candidate = favourable - position * distance
            current_stop = max(current_stop, candidate) if position == 1 else min(current_stop, candidate)

        if position in (1, -1) and exit_price is None:
            session = day.loc[trade_mask]
            exit_price = float(session.iloc[-1]["close"])
            exit_time, exit_reason = session.index[-1], "session_close"

        if position in (1, -1):
            pnl = position * units * (float(exit_price) - entry_price)
            equity += pnl
            trades.append({
                "symbol": symbol, "trading_date_london": str(trading_date),
                "entry_time_utc": entry_time.isoformat(), "exit_time_utc": exit_time.isoformat(),
                "side": "long" if position == 1 else "short", "entry_price": entry_price,
                "exit_price": float(exit_price), "units": units,
                "standard_lots": units / 100_000 if not symbol.startswith("BTC") else np.nan,
                "initial_risk_usd": initial_risk, "pnl_usd": pnl,
                "return_on_starting_day_equity": pnl / (equity - pnl),
                "exit_reason": exit_reason, "ending_equity_usd": equity,
            })
        equity_rows.append({"date": pd.Timestamp(trading_date), "equity_usd": equity})

    trades_frame = pd.DataFrame(trades)
    skipped_frame = pd.DataFrame(skipped)
    if equity_rows:
        daily = pd.DataFrame(equity_rows).drop_duplicates("date", keep="last").set_index("date")
        daily.index = pd.DatetimeIndex(daily.index).tz_localize("UTC")
    else:
        daily = pd.DataFrame(columns=["equity_usd"],
                             index=pd.DatetimeIndex([], tz="UTC", name="date"))
    # Preserve the full declared study window even where a broker session is
    # missing: no trade means unchanged cash, not a shortened backtest.
    calendar = pd.date_range(config.start, pd.Timestamp(config.end) - pd.Timedelta(days=1),
                             freq="B", tz="UTC")
    daily = daily.reindex(calendar).ffill().fillna(config.initial_equity_usd)
    return {"trades": trades_frame, "skipped": skipped_frame, "daily_equity": daily["equity_usd"]}


def data_quality(bars: pd.DataFrame) -> dict:
    deltas = bars.index.to_series().diff()
    unexpected = deltas[(deltas > pd.Timedelta(minutes=5)) & (deltas < pd.Timedelta(days=2))]
    return {
        "rows": int(len(bars)), "first_timestamp_utc": bars.index[0].isoformat(),
        "last_timestamp_utc": bars.index[-1].isoformat(), "duplicate_timestamps": int(bars.index.duplicated().sum()),
        "non_weekend_gaps_over_5m_under_2d": int(len(unexpected)),
        "largest_gap_hours": float(deltas.max().total_seconds() / 3600),
        "identical_duplicate_rows_removed": int(bars.attrs.get("identical_duplicate_rows_removed", 0)),
    }


def calculate_metrics(result: dict, config: BreakoutConfig) -> dict:
    trades = result["trades"]
    equity = result["daily_equity"]
    returns = equity.pct_change().fillna(0.0)
    years = (pd.Timestamp(config.end) - pd.Timestamp(config.start)).days / 365.2425
    total = equity.iloc[-1] / config.initial_equity_usd - 1
    std = returns.std(ddof=1)
    wins, losses = trades.loc[trades.pnl_usd > 0, "pnl_usd"], trades.loc[trades.pnl_usd < 0, "pnl_usd"]
    return {
        "starting_equity_usd": config.initial_equity_usd, "ending_equity_usd": float(equity.iloc[-1]),
        "total_return": float(total), "cagr": float((equity.iloc[-1] / config.initial_equity_usd) ** (1 / years) - 1),
        "annualized_volatility": float(std * math.sqrt(252)),
        "sharpe_0rf": float(returns.mean() / std * math.sqrt(252)) if std > 0 else None,
        "max_drawdown": float(drawdown(equity).min()), "trade_count": int(len(trades)),
        "win_rate": float((trades.pnl_usd > 0).mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if losses.sum() < 0 else None,
        "average_trade_usd": float(trades.pnl_usd.mean()), "median_trade_usd": float(trades.pnl_usd.median()),
        "largest_win_usd": float(trades.pnl_usd.max()), "largest_loss_usd": float(trades.pnl_usd.min()),
        "session_close_exits": int((trades.exit_reason == "session_close").sum()),
    }


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def write_symbol_results(symbol: str, bars: pd.DataFrame, result: dict, config: BreakoutConfig, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    trades, skipped, equity = result["trades"], result["skipped"], result["daily_equity"]
    stats = calculate_metrics(result, config)
    quality = data_quality(bars)
    annual = trades.assign(year=pd.to_datetime(trades.trading_date_london).dt.year).groupby("year").agg(
        trades=("pnl_usd", "size"), pnl_usd=("pnl_usd", "sum"), win_rate=("pnl_usd", lambda x: (x > 0).mean()))
    annual["return_on_initial_equity"] = annual.pnl_usd / config.initial_equity_usd
    trades.to_csv(output / "trades.csv", index=False)
    skipped.to_csv(output / "skipped_days.csv", index=False)
    equity.rename("equity_usd").to_csv(output / "daily_equity.csv")
    annual.to_csv(output / "yearly_results.csv")

    dd = drawdown(equity) * 100
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1])
    axes[0].plot(equity.index, equity, color="#0b7285", linewidth=1.4)
    axes[0].set_ylabel("Equity (USD)"); axes[0].grid(alpha=.2)
    axes[1].fill_between(dd.index, dd.values, 0, color="#d1495b", alpha=.8)
    axes[1].set_ylabel("Drawdown %"); axes[1].set_xlabel("Date"); axes[1].grid(alpha=.2)
    fig.suptitle(f"{symbol} London Dynamic Breakout — research backtest")
    fig.tight_layout(); fig.savefig(output / "equity_drawdown.png", dpi=170, bbox_inches="tight"); plt.close(fig)

    if not trades.empty:
        scaling = trades.copy()
        scaling["entry_number"] = np.arange(1, len(scaling) + 1)
        fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
        axes[0].plot(scaling["entry_number"], scaling["initial_risk_usd"], color="#7b2cbf", linewidth=1.2)
        axes[0].set_ylabel("Risk budget (USD)"); axes[0].grid(alpha=.2)
        axes[0].set_title(f"{config.risk_fraction:.3%} of current equity risked on every entry")
        axes[1].plot(scaling["entry_number"], scaling["units"], color="#e67700", linewidth=1.0)
        axes[1].set_ylabel("Position quantity"); axes[1].set_xlabel("Trade number"); axes[1].grid(alpha=.2)
        fig.suptitle(f"{symbol} Compounding and Position Sizing")
        fig.tight_layout(); fig.savefig(output / "risk_position_scaling.png", dpi=170, bbox_inches="tight"); plt.close(fig)

        annual_plot = annual.copy()
        fig, ax = plt.subplots(figsize=(9, 4.8))
        colors = np.where(annual_plot["pnl_usd"] >= 0, "#2b8a3e", "#c92a2a")
        ax.bar(annual_plot.index.astype(str), annual_plot["pnl_usd"], color=colors)
        ax.axhline(0, color="#333333", linewidth=.8)
        ax.set_ylabel("Net P&L (USD)"); ax.set_xlabel("Year"); ax.grid(axis="y", alpha=.2)
        ax.set_title(f"{symbol} Yearly Performance")
        fig.tight_layout(); fig.savefig(output / "yearly_performance.png", dpi=170, bbox_inches="tight"); plt.close(fig)

    summary = {"symbol": symbol, "config": asdict(config), "metrics": stats, "data_quality": quality,
               "skipped_days": int(len(skipped)), "data_source": config.data_source_label,
               "costs": "Spread, commission, slippage, swap and financing set to zero at user's request."}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    sharpe_text = "n/a" if stats["sharpe_0rf"] is None else f"{stats['sharpe_0rf']:.2f}"
    pf_text = "n/a" if stats["profit_factor"] is None else f"{stats['profit_factor']:.2f}"
    last_candle_minute = 60 - config.bar_minutes
    report = f"""# {symbol} London Dynamic Breakout Backtest

> Educational portfolio research. This is a bar-based historical simulation, not live trading and not a forecast.

## Fixed rules

- Test window: 1 January 2021 to 1 January 2026 (end exclusive)
- Starting equity: ${config.initial_equity_usd:,.0f}; this symbol has its own independent account curve
- London clock: Europe/London, so UK daylight-saving changes are handled automatically
- Dynamic entries: buy stop at the previous {config.lookback_bars} completed bars' high; sell stop at their low
- Entry window: 08:00–11:00 London; orders recalculate every {config.bar_minutes} minute(s)
- Maximum one trade per London day; opposite order cancels after entry
- Risk: {config.risk_fraction:.3%} of current equity on every entry, with exact fractional position sizing
- Initial/trailing stop: {"0.5% of entry price" if symbol.startswith("BTC") else "5 pips"}; no take-profit
- Exit: trailing stop or forced flat using the final 15:{last_candle_minute:02d} candle close
- Data source: {config.data_source_label}
- Costs: zero
- Ambiguous candle rule: if both entries touch in one candle, skip the day

## Results

| Metric | Result |
|---|---:|
| Ending equity | ${stats['ending_equity_usd']:,.2f} |
| Total return | {_percent(stats['total_return'])} |
| CAGR | {_percent(stats['cagr'])} |
| Maximum drawdown | {_percent(stats['max_drawdown'])} |
| Sharpe ratio (0% RF) | {sharpe_text} |
| Trades | {stats['trade_count']:,} |
| Win rate | {_percent(stats['win_rate'])} |
| Profit factor | {pf_text} |
| Average trade | ${stats['average_trade_usd']:,.2f} |
| Largest loss | ${stats['largest_loss_usd']:,.2f} |
| Skipped/incomplete or ambiguous days | {len(skipped):,} |

## Charts

![Equity curve and drawdown](equity_drawdown.png)

![Risk and position-size scaling](risk_position_scaling.png)

![Yearly performance](yearly_performance.png)

## Interpretation and limitations

The rules were fixed before this run. {config.bar_minutes}-minute candles cannot reveal the sequence of prices inside a candle, so the engine uses conservative stop handling and skips dual-entry ambiguity. FX uses HistData bid candles; BTCUSDT uses Binance spot trade candles. The test deliberately excludes spread, commission, slippage, swap, financing, latency and broker lot rounding. Therefore this is appropriate as a transparent Python portfolio project, but it is not evidence that the same result is executable live. Parameter optimization should use a separate training period and untouched out-of-sample period.
"""
    (output / "REPORT.md").write_text(report, encoding="utf-8")
    return summary


def run(data_dir: Path, output: Path, config: BreakoutConfig | None = None) -> dict[str, dict]:
    config = config or BreakoutConfig()
    summaries: dict[str, dict] = {}
    for symbol in config.symbols:
        path = data_dir / f"{symbol.lower()}_m5_bid.csv"
        bars = load_bars(path, config.start, config.end)
        result = backtest_symbol(bars, symbol, config)
        summaries[symbol] = write_symbol_results(symbol, bars, result, config, output / symbol.lower())
    index = "# London Dynamic Breakout — Individual Reports\n\n" + "\n".join(
        f"- [{symbol} report]({symbol.lower()}/REPORT.md)" for symbol in config.symbols)
    index += "\n\nEach market is tested separately from USD 150,000. No portfolio result is combined.\n"
    output.mkdir(parents=True, exist_ok=True)
    (output / "README.md").write_text(index, encoding="utf-8")
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Five-year London dynamic breakout research backtest")
    parser.add_argument("--data-dir", type=Path, default=Path("data/london_breakout"))
    parser.add_argument("--output", type=Path, default=Path("outputs/london_breakout"))
    args = parser.parse_args()
    summaries = run(args.data_dir, args.output)
    for symbol, summary in summaries.items():
        print(f"{symbol}: ${summary['metrics']['ending_equity_usd']:,.2f}, "
              f"return {summary['metrics']['total_return']:.2%}, "
              f"max DD {summary['metrics']['max_drawdown']:.2%}")
    print(f"Reports: {(args.output / 'README.md').resolve()}")
    print("Historical research only; no live orders were sent.")


if __name__ == "__main__":
    main()

