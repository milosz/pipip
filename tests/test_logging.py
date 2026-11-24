from __future__ import annotations

import json
import logging

from pipip.logging import JsonFormatter


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.LogRecord(
        "pipip.test",
        logging.INFO,
        __file__,
        1,
        "hello %s",
        ("world",),
        None,
    )
    record.created = 1.25
    record.request_id = "abc123"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["ts"] == "1970-01-01T00:00:01.250000+00:00"
    assert "created" not in payload
    assert payload["level"] == "INFO"
    assert payload["logger"] == "pipip.test"
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "abc123"
