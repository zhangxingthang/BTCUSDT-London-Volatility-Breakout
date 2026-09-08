from __future__ import annotations

import math

import numpy as np
import pandas as pd


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def performance_metrics(equity: pd.Series, trades: pd.DataFrame | None = None) -> dict[str, float]:
    equity = equity.dropna().astype(float)
    if len(equity) < 2 or (equity <= 0).any():
        raise ValueError("Metrics require at least two strictly positive equity observations")
    daily = equity.resample("1D").last().dropna().pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).total_seconds() / (365.2425 * 24 * 3600)
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else np.nan
    volatility = daily.std(ddof=1) * math.sqrt(252) if len(daily) > 1 else np.nan
    sharpe = daily.mean() / daily.std(ddof=1) * math.sqrt(252) if len(daily) > 1 and daily.std(ddof=1) else np.nan
    result = {
        "starting_equity": equity.iloc[0], "ending_equity": equity.iloc[-1],
        "total_return": total_return, "cagr": cagr, "annualized_volatility": volatility,
        "sharpe_0rf": sharpe, "max_drawdown": drawdown(equity).min(),
    }
    if trades is not None and not trades.empty:
        pnl = trades["net_pnl"] if "net_pnl" in trades else trades["realised_pnl"] - trades["commission"]
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        result.update({
            "order_count": float(len(trades)), "win_rate_on_realising_orders": float((pnl > 0).mean()),
            "profit_factor": float(wins.sum() / abs(losses.sum())) if losses.sum() else np.nan,
            "average_realised_order_pnl": float(pnl.mean()),
        })
    return {key: float(value) for key, value in result.items()}


def monthly_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("ME").last().pct_change().dropna().rename("return")

