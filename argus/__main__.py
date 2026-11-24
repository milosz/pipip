"""Start Argus using the configuration in ARGUS_CONFIG or config.yaml."""

from __future__ import annotations

import logging

import uvicorn

from argus.config import ConfigError, load_config
from argus.logging import setup_logging
from argus.outputs.app import create_app
from argus.validation import validate_settings, validate_warnings

logger = logging.getLogger("argus")


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
