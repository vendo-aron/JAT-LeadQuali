"""Run database migrations, once, from one place.

`alembic upgrade head` is a deploy step, which is why alembic is a runtime dependency
(#15). It runs here — invoked by the deploy pipeline before traffic — rather than on
worker cold start, because a worker that migrates on cold start races N containers for
the same DDL lock, and the first burst after a deploy is exactly when N is largest.
Postgres would serialise them on the lock, so the failure is not corruption but a fleet
of workers each waiting on a migration before qualifying its first lead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from alembic import command
from alembic.config import Config

from leadquali.config import Settings, get_settings
from leadquali.observability.logs import configure_logging

configure_logging()

LOGGER: Final = logging.getLogger(__name__)

#: Repo-root-relative location of alembic.ini, resolved from this file so the handler
#: works the same in a Lambda bundle as it does from a checkout.
ALEMBIC_INI: Final[Path] = Path(__file__).resolve().parents[3] / "alembic.ini"


def build_config(settings: Settings | None = None, *, ini_path: Path | None = None) -> Config:
    """An alembic config with the URL injected from settings, never from the ini file."""
    resolved = settings or get_settings()
    config = Config(str(ini_path or ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", resolved.require_database_url())
    return config


def upgrade_to_head(settings: Settings | None = None) -> None:
    """Apply every pending migration."""
    command.upgrade(build_config(settings), "head")


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, str]:
    """Entrypoint named by `infra/template.yaml`. Idempotent: a no-op when up to date."""
    LOGGER.info("migrate.start", extra={"event": "migrate.start"})
    upgrade_to_head()
    LOGGER.info("migrate.done", extra={"event": "migrate.done"})
    return {"status": "ok"}
