"""Python-only London breakout using independent public M1 candles."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from .london_breakout import BreakoutConfig, backtest_symbol, write_symbol_results
from .public_m1 import load_public_symbol


def independent_config(*, risk_fraction: float = 0.0002,
                       symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "BTCUSDT")) -> BreakoutConfig:
    return replace(
        BreakoutConfig(),
        risk_fraction=risk_fraction,
        bar_minutes=1,
        lookback_bars=60,
        symbols=symbols,
        data_source_label="HistData M1 bid candles for FX; Binance spot M1 trade candles for BTCUSDT",
    )


def run(data_root: Path, output: Path, *, risk_fraction: float = 0.0002,
        symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "BTCUSDT")) -> dict[str, dict]:
    config = independent_config(risk_fraction=risk_fraction, symbols=symbols)
    summaries: dict[str, dict] = {}
    output.mkdir(parents=True, exist_ok=True)
    for symbol in config.symbols:
        bars = load_public_symbol(data_root, symbol)
        result = backtest_symbol(bars, symbol, config)
        summaries[symbol] = write_symbol_results(symbol, bars, result, config, output / symbol.lower())
    links = "\n".join(f"- [{symbol}](./{symbol.lower()}/REPORT.md)" for symbol in config.symbols)
    (output / "README.md").write_text(
        "# Python-only independent M1 backtests\n\n" + links +
        "\n\nHistData FX M1 and Binance BTCUSDT M1 archives; no MT5 data or tester was used.\n",
        encoding="utf-8",
    )
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Python-only public M1 London breakout")
    parser.add_argument("--data-root", type=Path, default=Path("data/public_m1_archives"))
    parser.add_argument("--output", type=Path, default=Path("outputs/python_independent_m1"))
    parser.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD", "BTCUSDT"])
    parser.add_argument("--risk-percent", type=float, default=0.02,
                        help="Percent of current equity risked at the initial stop per entry")
    args = parser.parse_args()
    summaries = run(args.data_root, args.output, risk_fraction=args.risk_percent / 100,
                    symbols=tuple(symbol.upper() for symbol in args.symbols))
    for symbol, summary in summaries.items():
        metrics = summary["metrics"]
        print(f"{symbol}: ${metrics['ending_equity_usd']:,.2f}, return {metrics['total_return']:.2%}, "
              f"max DD {metrics['max_drawdown']:.2%}")
    print(f"Reports: {(args.output / 'README.md').resolve()}")


if __name__ == "__main__":
    main()

