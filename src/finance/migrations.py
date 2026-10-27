"""Finance schema versions and upgrades.

Odysseus has no migration framework (``core.database`` creates tables and
runs idempotent ``_migrate_*`` helpers), so finance keeps its own small one:

- ``finance_meta.schema_version`` records which steps have run.
- A database with finance tables but no ``finance_meta`` is version 1 (v1).
- Before any step runs on an existing finance database, the whole database
  is copied to ``FINANCE_BACKUP_DIR`` with SQLite's online backup API. If the
  copy fails, nothing is migrated.
- Every step is idempotent (``create_all`` skips existing tables; column
  additions check ``PRAGMA table_info`` first) and only ever adds tables or
  nullable columns, so v1 rows are never rewritten.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from core.database import Base

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2


def _create(bind, tables) -> None:
    Base.metadata.create_all(bind=bind, tables=tables, checkfirst=True)


def _step_1(bind) -> None:
    from src.finance.models import V1_TABLES

    _create(bind, V1_TABLES)


def _step_2(bind) -> None:
    from src.finance.models import V2_TABLES

    _create(bind, V2_TABLES)


STEPS = {1: _step_1, 2: _step_2}


def _current_version(bind) -> int:
    tables = set(inspect(bind).get_table_names())
    if "finance_meta" in tables:
        with bind.connect() as conn:
            row = conn.execute(text("SELECT value FROM finance_meta WHERE key = 'schema_version'")).fetchone()
        if row is not None:
            return int(row[0])
    return 1 if "finance_accounts" in tables else 0


def _sqlite_path(bind):
    if bind.url.get_backend_name() != "sqlite":
        return None
    database = bind.url.database
    if not database or database == ":memory:" or database.startswith("file::memory:"):
        return None
    return database


def backup_database(bind, *, from_version: int, to_version: int, backup_dir=None) -> str | None:
    """Copy the whole SQLite database before migrating. Returns the copy's path.

    Returns None for in-memory/non-SQLite databases (nothing to protect on disk).
    Raises if the copy cannot be made.
    """
    path = _sqlite_path(bind)
    if path is None:
        return None
    if backup_dir is None:
        from src.constants import FINANCE_BACKUP_DIR as backup_dir
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = os.path.join(backup_dir, f"app-before-finance-v{from_version}-to-v{to_version}-{stamp}.db")
    raw = bind.raw_connection()
    try:
        source = raw.driver_connection
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        raw.close()
    try:
        os.chmod(target, 0o600)  # same protection as app.db
    except OSError:  # pragma: no cover - non-POSIX
        pass
    logger.info("[finance] backed up database to %s before migrating v%s -> v%s", target, from_version, to_version)
    return target


def migrate(bind, *, backup_dir=None) -> int:
    """Upgrade the finance schema to ``SCHEMA_VERSION``. Returns the final version."""
    current = _current_version(bind)
    if current >= SCHEMA_VERSION:
        return current
    if current >= 1:
        # Existing finance data: refuse to touch it without a backup.
        backup_database(bind, from_version=current, to_version=SCHEMA_VERSION, backup_dir=backup_dir)
    from src.finance.models import FinanceMeta

    for version in range(max(current, 0) + 1, SCHEMA_VERSION + 1):
        STEPS[version](bind)
        logger.info("[finance] schema step %s applied", version)
    _create(bind, [FinanceMeta.__table__])
    with bind.begin() as conn:
        conn.execute(text("DELETE FROM finance_meta WHERE key = 'schema_version'"))
        conn.execute(text("INSERT INTO finance_meta (key, value) VALUES ('schema_version', :v)"),
                     {"v": str(SCHEMA_VERSION)})
    return SCHEMA_VERSION
