"""Finance schema versions and upgrades.

Odysseus has no migration framework (``core.database`` creates tables and
runs idempotent ``_migrate_*`` helpers), so finance keeps its own small one:

- ``finance_meta.schema_version`` records which steps have run.
- A database with finance tables but no ``finance_meta`` is version 1 (v1).
- Before any step runs on an existing finance database, the whole database
  is copied to ``FINANCE_BACKUP_DIR`` with SQLite's online backup API. If the
  copy fails, nothing is migrated.
- Every step is idempotent (``create_all`` skips existing tables; column
  additions check ``PRAGMA table_info`` first) and only ever adds tables,
  nullable columns or new rows, so existing rows are never rewritten.
  Step 6 inserts a primary household member per user and makes them the
  owner of every existing account, asset and liability.
- Step 7 is the one exception to "only adds": SQLite can't change a CHECK
  constraint, so ``finance_accounts`` is rebuilt to allow the child account
  types. Every row is copied unchanged, inside one transaction that rolls
  back unless the row count matches and every foreign key still resolves.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from core.database import Base

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 7


def _create(bind, tables) -> None:
    Base.metadata.create_all(bind=bind, tables=tables, checkfirst=True)


def _step_1(bind) -> None:
    from src.finance.models import V1_TABLES

    _create(bind, V1_TABLES)


def _step_2(bind) -> None:
    from src.finance.models import V2_TABLES

    _create(bind, V2_TABLES)


def _add_column(bind, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ADD COLUMN, only if the column is missing (idempotent)."""
    with bind.begin() as conn:
        existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        if existing and column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _step_3(bind) -> None:
    from src.finance.models import V3_TABLES

    _create(bind, V3_TABLES)
    # Nullable additions only: existing rows keep their values.
    _add_column(bind, "finance_rules", "liability_id", "VARCHAR REFERENCES finance_liabilities(id)")
    _add_column(bind, "finance_liabilities", "interest_bp", "INTEGER")


def _step_4(bind) -> None:
    from src.finance.models import V4_TABLES

    _create(bind, V4_TABLES)


def _step_5(bind) -> None:
    from src.finance.models import V5_TABLES

    _create(bind, V5_TABLES)


def _step_6(bind) -> None:
    """Household members: one primary member per user, owning all existing items.

    Only inserts rows into the two new tables; existing rows are not touched.
    """
    from sqlalchemy.orm import Session

    from src.finance.members import ensure_household
    from src.finance.models import V6_TABLES

    _create(bind, V6_TABLES)
    owners: set[str] = set()
    with bind.connect() as conn:
        for table in ("finance_accounts", "finance_assets", "finance_liabilities", "finance_categories"):
            owners.update(row[0] for row in conn.execute(text(f"SELECT DISTINCT owner FROM {table}")) if row[0])
    with Session(bind=bind) as db:
        for owner in sorted(owners):
            ensure_household(db, owner)
        db.commit()


OLD_ACCOUNT_CHECK = "account_type IN ('current', 'savings', 'credit_card', 'cash')"
NEW_ACCOUNT_CHECK = ("account_type IN ('current', 'savings', 'credit_card', 'cash', "
                     "'child_savings', 'child_current')")


def _rebuild_accounts_table(bind) -> None:
    """Rebuild finance_accounts with the wider account type CHECK (SQLite's
    documented table-rebuild procedure). Skipped when already allowed."""
    with bind.connect() as conn:
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'finance_accounts'")).fetchone()
        indexes = [r[0] for r in conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'finance_accounts' "
            "AND sql IS NOT NULL"))]
    if row is None or "child_savings" in row[0]:
        return
    if OLD_ACCOUNT_CHECK not in row[0] or not row[0].startswith("CREATE TABLE finance_accounts ("):
        raise RuntimeError("finance_accounts has an unexpected shape; not rebuilding it")
    create_new = row[0].replace(OLD_ACCOUNT_CHECK, NEW_ACCOUNT_CHECK).replace(
        "CREATE TABLE finance_accounts (", "CREATE TABLE finance_accounts_v7 (", 1)

    raw = bind.raw_connection()
    con = raw.driver_connection
    previous_isolation = con.isolation_level
    con.isolation_level = None  # manual transaction control
    try:
        # Must be set outside a transaction; dropping the old table would
        # otherwise trip the foreign keys that point at it.
        con.execute("PRAGMA foreign_keys=OFF")
        if con.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("could not pause foreign keys to rebuild finance_accounts")
        con.execute("BEGIN IMMEDIATE")
        try:
            before = con.execute("SELECT * FROM finance_accounts ORDER BY id").fetchall()
            con.execute(create_new)
            con.execute("INSERT INTO finance_accounts_v7 SELECT * FROM finance_accounts")
            con.execute("DROP TABLE finance_accounts")
            con.execute("ALTER TABLE finance_accounts_v7 RENAME TO finance_accounts")
            for sql in indexes:
                con.execute(sql)
            after = con.execute("SELECT * FROM finance_accounts ORDER BY id").fetchall()
            if after != before:
                raise RuntimeError("finance_accounts rows changed while rebuilding; rolled back")
            broken = con.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"foreign keys broken while rebuilding finance_accounts: {broken[:3]}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")
        con.isolation_level = previous_isolation
        raw.close()


def _step_7(bind) -> None:
    """Children: settings table, recurring transfers and child account types."""
    from src.finance.models import V7_TABLES

    _create(bind, V7_TABLES)
    _add_column(bind, "finance_recurring", "to_account_id", "VARCHAR REFERENCES finance_accounts(id)")
    if bind.url.get_backend_name() == "sqlite":
        _rebuild_accounts_table(bind)


STEPS = {1: _step_1, 2: _step_2, 3: _step_3, 4: _step_4, 5: _step_5, 6: _step_6, 7: _step_7}


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
