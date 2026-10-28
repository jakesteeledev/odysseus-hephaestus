"""Finance schema upgrades keep v1 data untouched and back it up first."""

import os
import sqlite3

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from core.database import Base
from src.finance import migrations
from src.finance import service as fin
from src.finance.models import V1_TABLES

OWNER = "alice"


def _engine(path):
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def _dump(path):
    """Every row of every v1 finance table, for before/after comparison."""
    conn = sqlite3.connect(path)
    try:
        return {t.name: sorted(conn.execute(f"SELECT * FROM {t.name}").fetchall()) for t in V1_TABLES}
    finally:
        conn.close()


@pytest.fixture
def v1_database(tmp_path, monkeypatch):
    """A database exactly as Finance v1 left it: v1 tables, real data, no finance_meta."""
    # v1 had no household members, so new accounts got no owner rows.
    from src.finance import members

    monkeypatch.setattr(members, "assign_default_owner", lambda *args, **kwargs: None)
    path = tmp_path / "app.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine, tables=V1_TABLES)
    db = sessionmaker(bind=engine)()
    current = fin.create_account(db, OWNER, name="Current", opening_balance="100")
    savings = fin.create_account(db, OWNER, name="Savings")
    cats = {c["name"]: c["id"] for c in fin.list_categories(db, OWNER)}
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-10-01", amount="12.34",
                           direction="out", category_id=cats["Groceries"], description="Tesco")
    fin.create_transfer(db, OWNER, from_account_id=current.id, to_account_id=savings.id,
                        date="2026-10-02", amount="50")
    db.commit()
    db.close()
    engine.dispose()
    return path


def test_v1_database_is_detected_as_version_1(v1_database):
    engine = _engine(v1_database)
    assert migrations._current_version(engine) == 1
    engine.dispose()


def test_upgrade_preserves_v1_rows_and_makes_a_backup(v1_database, tmp_path):
    before = _dump(v1_database)
    engine = _engine(v1_database)
    backups = tmp_path / "backups"
    assert migrations.migrate(engine, backup_dir=str(backups)) == migrations.SCHEMA_VERSION
    engine.dispose()

    assert _dump(v1_database) == before  # not a single v1 row changed

    copies = list(backups.iterdir())
    assert len(copies) == 1 and copies[0].name.startswith("app-before-finance-v1-to-v")
    assert _dump(copies[0]) == before  # the backup is a full, readable copy
    assert oct(os.stat(copies[0]).st_mode & 0o777) == "0o600"

    engine = _engine(v1_database)
    tables = set(inspect(engine).get_table_names())
    assert {"finance_assets", "finance_liabilities", "finance_valuations", "finance_contributions",
            "finance_loan_schedules", "finance_repayments", "finance_recurring", "finance_recurring_events",
            "finance_budgets", "finance_meta"} <= tables
    with engine.connect() as conn:
        assert conn.execute(text("SELECT value FROM finance_meta WHERE key='schema_version'")).scalar() == str(
            migrations.SCHEMA_VERSION)
    # Balances read the same through the service after the upgrade.
    db = sessionmaker(bind=engine)()
    balances = sorted(fin.account_balances(db, OWNER).values())
    assert balances == sorted([5000, 10000 - 1234 - 5000])
    db.close()
    engine.dispose()


def test_upgrade_runs_once(v1_database, tmp_path):
    backups = tmp_path / "backups"
    engine = _engine(v1_database)
    migrations.migrate(engine, backup_dir=str(backups))
    migrations.migrate(engine, backup_dir=str(backups))
    engine.dispose()
    assert len(list(backups.iterdir())) == 1


def test_no_migration_without_a_backup(v1_database, tmp_path, monkeypatch):
    before = _dump(v1_database)

    def broken_backup(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(migrations, "backup_database", broken_backup)
    engine = _engine(v1_database)
    with pytest.raises(OSError):
        migrations.migrate(engine, backup_dir=str(tmp_path / "backups"))
    assert migrations._current_version(engine) == 1
    assert "finance_assets" not in set(inspect(engine).get_table_names())
    engine.dispose()
    assert _dump(v1_database) == before


def test_fresh_database_needs_no_backup(tmp_path):
    engine = _engine(tmp_path / "fresh.db")
    backups = tmp_path / "backups"
    assert migrations.migrate(engine, backup_dir=str(backups)) == migrations.SCHEMA_VERSION
    assert not backups.exists()
    assert "finance_accounts" in set(inspect(engine).get_table_names())
    engine.dispose()


def _create_v2_schema(engine):
    """Finance tables exactly as schema version 2 had them (before step 3)."""
    from sqlalchemy.schema import CreateTable

    from src.finance.models import V2_TABLES

    with engine.begin() as conn:
        for table in V1_TABLES + V2_TABLES:
            ddl = str(CreateTable(table).compile(engine))
            lines = [ln.rstrip().rstrip(",") for ln in ddl.strip().splitlines()]
            if table.name == "finance_rules":
                lines = [ln for ln in lines if "liability_id" not in ln]
            if table.name == "finance_liabilities":
                lines = [ln for ln in lines if "interest_bp" not in ln]
            head, body, tail = lines[0], [ln for ln in lines[1:-1] if ln.strip()], lines[-1]
            conn.execute(text(head + "\n" + ",\n".join(body) + "\n" + tail))
        conn.execute(text("CREATE TABLE finance_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"))
        conn.execute(text("INSERT INTO finance_meta VALUES ('schema_version', '2')"))


def test_upgrade_from_v2_adds_loans_and_keeps_data(tmp_path):
    path = tmp_path / "app.db"
    engine = _engine(path)
    _create_v2_schema(engine)
    rules_cols = {r[1] for r in engine.connect().execute(text("PRAGMA table_info(finance_rules)"))}
    assert "liability_id" not in rules_cols
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO finance_categories (id, owner, name, kind, color, is_system, created_at, updated_at) "
                          "VALUES ('c1', 'alice', 'Bills', 'expense', '', 0, '2026-01-01', '2026-01-01')"))
        conn.execute(text("INSERT INTO finance_rules (id, owner, pattern, category_id, created_at, updated_at) "
                          "VALUES ('r1', 'alice', 'octopus', 'c1', '2026-01-01', '2026-01-01')"))
        conn.execute(text("INSERT INTO finance_liabilities (id, owner, name, liability_type, subtype, lender, notes, "
                          "archived, created_at, updated_at) VALUES ('l1', 'alice', 'Car', 'loan', '', '', '', 0, "
                          "'2026-01-01', '2026-01-01')"))
    before = {t: sorted(engine.connect().execute(text(f"SELECT * FROM {t}")).fetchall())
              for t in ("finance_rules", "finance_liabilities", "finance_categories")}
    backups = tmp_path / "backups"
    assert migrations._current_version(engine) == 2
    assert migrations.migrate(engine, backup_dir=str(backups)) == migrations.SCHEMA_VERSION
    assert [p.name.split("-to-")[0] for p in backups.iterdir()] == ["app-before-finance-v2"]
    with engine.connect() as conn:
        rules = conn.execute(text("SELECT id, pattern, category_id, liability_id FROM finance_rules")).fetchall()
        assert rules == [("r1", "octopus", "c1", None)]
        assert conn.execute(text("SELECT interest_bp FROM finance_liabilities")).fetchall() == [(None,)]
        tables = set(inspect(engine).get_table_names())
        for t, rows in before.items():
            # Old columns keep their values; the new columns are empty.
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({t})"))][:len(rows[0])]
            assert sorted(conn.execute(text(f"SELECT {', '.join(cols)} FROM {t}")).fetchall()) == rows
    assert {"finance_loan_schedules", "finance_repayments"} <= tables
    engine.dispose()


def test_upgrade_from_v8_allows_fortnightly_and_keeps_recurring_rows(tmp_path):
    """Step 9 rebuilds finance_recurring (new frequency CHECK) without touching its rows."""
    from src.finance.models import FINANCE_TABLES

    path = tmp_path / "app.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine, tables=[t for t in FINANCE_TABLES if t.name not in (
        "finance_jobs", "finance_job_changes", "finance_payslips")])
    with engine.begin() as conn:
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'finance_recurring'")).scalar()
        old = sql.replace(migrations.NEW_RECURRING_CHECK, migrations.OLD_RECURRING_CHECK)
        old = "\n".join(line for line in old.splitlines() if "job_id" not in line)
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text("DROP TABLE finance_recurring"))
        conn.execute(text(old))
        conn.execute(text("INSERT INTO finance_accounts (id, owner, name, account_type, opening_balance_pence, "
                          "archived, sort_order, created_at, updated_at) VALUES ('a1', 'alice', 'Current', 'current', "
                          "0, 0, 0, '2026-01-01', '2026-01-01')"))
        conn.execute(text("INSERT INTO finance_recurring (id, owner, name, account_id, amount_pence, direction, "
                          "frequency, anchor_date, match_text, archived, created_at, updated_at) VALUES ('r1', "
                          "'alice', 'Rent', 'a1', 95000, 'out', 'monthly', '2026-01-01', 'rent', 0, '2026-01-01', "
                          "'2026-01-01')"))
        conn.execute(text("INSERT INTO finance_meta (key, value) VALUES ('schema_version', '8')"))
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(text("UPDATE finance_recurring SET frequency = 'fortnightly'"))
    before = engine.connect().execute(text("SELECT * FROM finance_recurring")).fetchall()
    backups = tmp_path / "backups"
    assert migrations.migrate(engine, backup_dir=str(backups)) == migrations.SCHEMA_VERSION
    assert [p.name.split("-to-")[0] for p in backups.iterdir()] == ["app-before-finance-v8"]
    with engine.begin() as conn:
        after = conn.execute(text("SELECT * FROM finance_recurring")).fetchall()
        assert [tuple(r)[:-1] for r in after] == [tuple(r) for r in before]  # job_id is added last, empty
        assert after[0][-1] is None
        conn.execute(text("UPDATE finance_recurring SET frequency = 'fortnightly'"))
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    assert {"finance_jobs", "finance_job_changes", "finance_payslips"} <= set(inspect(engine).get_table_names())
    engine.dispose()
