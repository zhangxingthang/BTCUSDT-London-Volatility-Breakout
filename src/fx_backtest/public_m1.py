"""Normalize HistData FX and Binance BTCUSDT one-minute public archives."""
from __future__ import annotations

from pathlib import Path
import zipfile

import pandas as pd


UTC_MINUS_FIVE = "-05:00"  # HistData publishes EST without DST adjustments.


def load_histdata(root: Path, symbol: str) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for path in sorted((root / "histdata").glob(f"DAT_ASCII_{symbol}_M1_*.zip")):
        with zipfile.ZipFile(path) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(csv_names) != 1:
                raise ValueError(f"Expected one CSV in {path}, found {len(csv_names)}")
            with archive.open(csv_names[0]) as stream:
                frame = pd.read_csv(stream, sep=";", header=None,
                                    names=["date", "open", "high", "low", "close", "volume"])
        timestamp = pd.to_datetime(frame.pop("date"), format="%Y%m%d %H%M%S", errors="raise")
        frame.index = timestamp.dt.tz_localize(UTC_MINUS_FIVE).dt.tz_convert("UTC")
        parts.append(frame[["open", "high", "low", "close"]])
    if len(parts) != 5:
        raise ValueError(f"Expected five HistData annual archives for {symbol}, found {len(parts)}")
    return pd.concat(parts).sort_index()


def load_binance(root: Path) -> pd.DataFrame:
    columns = ["open_time", "open", "high", "low", "close", "volume", "close_time",
               "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
    parts: list[pd.DataFrame] = []
    paths = sorted((root / "binance").glob("BTCUSDT-1m-*.zip"))
    if len(paths) != 60:
        raise ValueError(f"Expected 60 Binance monthly archives, found {len(paths)}")
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(csv_names) != 1:
                raise ValueError(f"Expected one CSV in {path}, found {len(csv_names)}")
            with archive.open(csv_names[0]) as stream:
                frame = pd.read_csv(stream, header=None, names=columns)
        unit = "us" if int(frame.open_time.iloc[0]) > 10**14 else "ms"
        frame.index = pd.to_datetime(frame.pop("open_time"), unit=unit, utc=True)
        parts.append(frame[["open", "high", "low", "close"]].astype(float))
    return pd.concat(parts).sort_index()


def validate(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if frame.empty or not frame.index.is_monotonic_increasing:
        raise ValueError(f"{symbol} timestamps are empty or unordered")
    duplicate_rows_removed = 0
    if frame.index.has_duplicates:
        duplicated = frame[frame.index.duplicated(keep=False)]
        conflicts = duplicated.groupby(level=0).nunique().max(axis=1).gt(1)
        if conflicts.any():
            raise ValueError(f"{symbol} has {int(conflicts.sum())} conflicting duplicate timestamps")
        duplicate_rows_removed = int(frame.index.duplicated().sum())
        frame = frame.loc[~frame.index.duplicated(keep="first")].copy()
    invalid = ((frame.high < frame[["open", "close"]].max(axis=1)) |
               (frame.low > frame[["open", "close"]].min(axis=1)) |
               (frame.high < frame.low) | (frame <= 0).any(axis=1))
    if invalid.any():
        raise ValueError(f"{symbol} has {int(invalid.sum())} invalid OHLC rows")
    frame.attrs["identical_duplicate_rows_removed"] = duplicate_rows_removed
    return frame


def load_public_symbol(root: Path, symbol: str) -> pd.DataFrame:
    frame = load_binance(root) if symbol == "BTCUSDT" else load_histdata(root, symbol)
    return validate(frame, symbol)

