"""Start pi-pip! using the configuration in PIPIP_CONFIG or config.yaml."""

from __future__ import annotations

import logging

import uvicorn

from pipip.config import ConfigError, load_config
from pipip.logging import setup_logging
from pipip.outputs.app import create_app
from pipip.validation import validate_settings, validate_warnings

logger = logging.getLogger("pipip")


def main() -> None:
    setup_logging("INFO")
    try:
        settings = load_config()
    except ConfigError as error:
        raise SystemExit(str(error)) from error
    errors = validate_settings(settings)
    if errors:
        raise SystemExit("\n".join(errors))
    for warning in validate_warnings(settings):
        logger.warning("%s", warning)
    uvicorn.run(
        create_app(settings),
        host=settings.http.host,
        port=settings.http.port,
        access_log=settings.http.access_log,
        workers=1,
    )


if __name__ == "__main__":
    main()
