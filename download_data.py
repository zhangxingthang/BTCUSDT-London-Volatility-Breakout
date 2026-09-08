"""Download and verify BTCUSDT one-minute Binance archives for 2021–2025."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time

import requests


OUTPUT = Path("data/binance")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "BTCUSDT research backtest/1.0"

    for year in range(2021, 2026):
        for month in range(1, 13):
            name = f"BTCUSDT-1m-{year}-{month:02d}.zip"
            target = OUTPUT / name
            url = f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/{name}"

            if target.exists():
                print(f"skip {name}")
                continue

            response = session.get(url, timeout=120)
            response.raise_for_status()
            checksum = session.get(url + ".CHECKSUM", timeout=30)
            checksum.raise_for_status()

            expected = checksum.text.split()[0].lower()
            actual = hashlib.sha256(response.content).hexdigest()
            if actual != expected:
                raise RuntimeError(f"Checksum failed: {name}")

            target.write_bytes(response.content)
            print(f"downloaded {name}")
            time.sleep(0.25)


if __name__ == "__main__":
    main()

