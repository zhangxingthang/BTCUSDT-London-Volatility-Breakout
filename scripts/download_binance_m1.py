"""Download and checksum-verify Binance BTCUSDT one-minute archives for 2021–2025."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time

import requests


ROOT = Path(__file__).resolve().parents[1] / "data" / "public_m1_archives" / "binance"


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "BTCUSDT research backtest/1.0"
    for year in range(2021, 2026):
        for month in range(1, 13):
            name = f"BTCUSDT-1m-{year}-{month:02d}.zip"
            path = ROOT / name
            url = f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/{name}"
            if path.exists() and path.stat().st_size > 10_000:
                print(f"SKIP {name}")
                continue
            response = session.get(url, timeout=120)
            response.raise_for_status()
            checksum = session.get(url + ".CHECKSUM", timeout=30)
            checksum.raise_for_status()
            expected = checksum.text.split()[0].lower()
            actual = hashlib.sha256(response.content).hexdigest()
            if actual != expected:
                raise RuntimeError(f"Checksum mismatch for {name}")
            path.write_bytes(response.content)
            print(f"GET  {name}")
            time.sleep(0.25)
    print(f"Archives ready: {ROOT}")


if __name__ == "__main__":
    main()

