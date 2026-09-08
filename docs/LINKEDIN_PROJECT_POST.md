# LinkedIn Project Post

I recently completed a Python research project testing a dynamic BTCUSDT volatility-breakout strategy over five years of one-minute Binance data.

The research question was simple: can a rolling one-hour breakout capture directional volatility during the London morning while keeping risk consistent as account equity changes?

I built an event-driven backtest that:

- recalculates dynamic entry levels every minute;
- handles London daylight-saving time;
- sizes every position from 0.05% of current equity;
- models initial and trailing stops without future-data leakage;
- rejects ambiguous intraminute entry paths;
- validates data and produces reproducible performance reports.

Across 1,301 simulated trades from 2021–2025, the zero-cost research baseline returned 2.63%, with a 1.34% maximum drawdown and a 1.11 profit factor. Performance was weak in 2021, nearly flat in 2023, and strongest in 2024–2025.

The most useful lesson was that scaling risk changes the magnitude of both return and drawdown—it does not create a stronger trading edge. The modest result and exclusion of trading costs mean this is a research baseline, not a production-ready system.

Project: https://github.com/zhangxingthang97-lang/BTCUSDT-London-Volatility-Breakout

#Python #QuantitativeFinance #AlgorithmicTrading #Backtesting #Bitcoin

