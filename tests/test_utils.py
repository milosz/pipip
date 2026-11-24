from __future__ import annotations

import pytest

from argus.core.utils import parse_iso8601


@pytest.mark.parametrize(
    "timestamp",
    ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"],
)
def test_parse_iso8601_rejects_timestamps_outside_utc_range(timestamp: str) -> None:
    assert parse_iso8601(timestamp) is None
