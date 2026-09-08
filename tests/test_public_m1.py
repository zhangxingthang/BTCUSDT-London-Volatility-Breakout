import pandas as pd
import pytest

from fx_backtest.public_m1 import validate


def _rows(values):
    index = pd.to_datetime([item[0] for item in values], utc=True, format="mixed")
    prices = [item[1] for item in values]
    return pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices}, index=index)


def test_identical_archive_duplicates_are_audited_and_removed():
    frame = _rows([("2024-01-01", 1.0), ("2024-01-01", 1.0), ("2024-01-01 00:01", 1.1)])
    result = validate(frame, "TEST")
    assert len(result) == 2
    assert result.attrs["identical_duplicate_rows_removed"] == 1


def test_conflicting_archive_duplicates_are_rejected():
    frame = _rows([("2024-01-01", 1.0), ("2024-01-01", 1.1)])
    with pytest.raises(ValueError, match="conflicting"):
        validate(frame, "TEST")

