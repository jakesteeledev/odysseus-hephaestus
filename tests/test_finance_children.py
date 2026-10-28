"""Finance v3 step 4: children's accounts, Junior ISAs, allowances and pocket money."""

import sqlite3
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes
from core.database import Base
from src.finance import budgets as bud
from src.finance import children as kids
from src.finance import members as mem
from src.finance import migrations
from src.finance import networth as nw
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance.models import FINANCE_TABLES, FinanceAccount, FinanceMeta, ensure_tables

OWNER = "alice"
TODAY = date(2026, 10, 28)


def _engine(url="sqlite://", **kwargs):
    if url == "sqlite://":
        kwargs.setdefault("poolclass", StaticPool)
        kwargs.setdefault("connect_args", {"check_same_thread": False})
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def _freeze(monkeypatch, day=TODAY):
    for module in (fin, mem, rec, bud, kids):
        monkeypatch.setattr(module, "_today_for_owner", lambda: day)


@pytest.fixture
def db(monkeypatch):
    engine = _engine()
    ensure_tables(engine)
    _freeze(monkeypatch)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def family(db):
    me = mem.ensure_household(db, OWNER)
    alfie = mem.create_member(db, OWNER, name="Alfie", relationship="child", date_of_birth="2018-03-01")
    leap = mem.create_member(db, OWNER, name="Leah", relationship="child", date_of_birth="2008-02-29")
    current = fin.create_account(db, OWNER, name="Current", opening_balance="1000")
    return {"me": me, "alfie": alfie, "leah": leap, "current": current}


def _jisa(db, member, name="Alfie JISA"):
    asset = nw.create_item(db, OWNER, "asset", name=name, asset_type="investment", subtype="junior isa", value="500")
    mem.set_owners(db, OWNER, "asset", asset.id, [member.id])
    return asset


# ── Dates ───────────────────────────────────────────────────────────────


def test_eighteenth_birthday_and_leap_years():
    assert kids.unlock_date(date(2018, 3, 1)) == date(2036, 3, 1)
    assert kids.unlock_date(date(2008, 2, 29)) == date(2026, 3, 1)  # 2026 is not a leap year
    assert kids.unlock_date(date(2004, 2, 29)) == date(2022, 3, 1)
    assert kids.birthday(date(2004, 2, 29), 20) == date(2024, 2, 29)
    assert kids.unlock_date(None) is None


@pytest.mark.parametrize("day, label, start", [
    (date(2026, 4, 5), "2025/26", date(2025, 4, 6)),
    (date(2026, 4, 6), "2026/27", date(2026, 4, 6)),
    (date(2026, 1, 1), "2025/26", date(2025, 4, 6)),
    (date(2026, 12, 31), "2026/27", date(2026, 4, 6)),
    (date(2099, 4, 6), "2099/00", date(2099, 4, 6)),
])
def test_tax_year_boundaries(day, label, start):
    year = kids.tax_year(day)
    assert (year["label"], year["start"]) == (label, start)
    assert year["end"] == date(start.year + 1, 4, 5)


def test_junior_isa_and_sipp_subtypes(db, family):
    isa = nw.create_item(db, OWNER, "asset", name="A", asset_type="investment", subtype="Junior ISA")
    sipp = nw.create_item(db, OWNER, "asset", name="B", asset_type="pension", subtype="junior_sipp")
    adult = nw.create_item(db, OWNER, "asset", name="C", asset_type="investment", subtype="isa")
    assert kids.is_junior_isa(isa) and not kids.is_junior_isa(adult) and not kids.is_junior_isa(sipp)
    assert kids.is_junior_sipp(sipp) and not kids.is_junior_sipp(isa)


# ── Allowance ───────────────────────────────────────────────────────────


def test_allowance_counts_this_tax_year_only(db, family):
    alfie = family["alfie"]
    isa = _jisa(db, alfie)
    nw.add_contribution(db, OWNER, isa.id, amount="1000", on="2026-04-05")  # last tax year
    nw.add_contribution(db, OWNER, isa.id, amount="2500", on="2026-04-06")  # this tax year
    nw.add_contribution(db, OWNER, isa.id, amount="100.50", on="2026-10-01")
    allowance = kids.junior_isa_allowance(db, OWNER, alfie.id, today=TODAY)
    assert (allowance["tax_year"], allowance["contributed_pence"]) == ("2026/27", 260050)
    assert (allowance["limit_pence"], allowance["remaining_pence"], allowance["over_pence"]) == (900000, 639950, 0)
    last_year = kids.junior_isa_allowance(db, OWNER, alfie.id, today=date(2026, 4, 5))
    assert (last_year["tax_year"], last_year["contributed_pence"]) == ("2025/26", 100000)


def test_allowance_is_per_child_across_their_junior_isas(db, family):
    alfie, leah = family["alfie"], family["leah"]
    first, second = _jisa(db, alfie), _jisa(db, alfie, "Alfie JISA 2")
    leahs = _jisa(db, leah, "Leah JISA")
    nw.add_contribution(db, OWNER, first.id, amount="6000", on="2026-05-01")
    nw.add_contribution(db, OWNER, second.id, amount="3500", on="2026-06-01")
    nw.add_contribution(db, OWNER, leahs.id, amount="10", on="2026-06-01")
    allowance = kids.junior_isa_allowance(db, OWNER, alfie.id, today=TODAY)
    assert (allowance["contributed_pence"], allowance["remaining_pence"], allowance["over_pence"]) == (950000, 0, 50000)
    assert kids.junior_isa_allowance(db, OWNER, leah.id, today=TODAY)["contributed_pence"] == 1000


def test_allowance_is_a_setting(db, family):
    assert kids.settings(db, OWNER) == {"junior_isa_limit_pence": 900000}
    assert kids.set_setting(db, OWNER, "junior_isa_limit_pence", "£9,500") == 950000
    assert kids.junior_isa_allowance(db, OWNER, family["alfie"].id, today=TODAY)["limit_pence"] == 950000
    for bad in ("-1", "abc", "2000000"):
        with pytest.raises(fin.FinanceError):
            kids.set_setting(db, OWNER, "junior_isa_limit_pence", bad)
    with pytest.raises(fin.FinanceError):
        kids.set_setting(db, OWNER, "nope", "1")
    assert kids.settings(db, "bob")["junior_isa_limit_pence"] == 900000  # per user


# ── Child accounts and the Children section ─────────────────────────────


def test_child_account_types(db, family):
    savings = fin.create_account(db, OWNER, name="Alfie saver", account_type="child savings")
    current = fin.create_account(db, OWNER, name="Alfie current", account_type="child_current")
    assert (savings.account_type, current.account_type) == ("child_savings", "child_current")
    with pytest.raises(fin.FinanceError):
        fin.create_account(db, OWNER, name="Bad", account_type="junior")


def test_children_overview(db, family):
    alfie, leah = family["alfie"], family["leah"]
    saver = fin.create_account(db, OWNER, name="Alfie saver", account_type="child_savings", opening_balance="40")
    mem.set_owners(db, OWNER, "account", saver.id, [alfie.id])
    isa = _jisa(db, alfie)
    nw.add_contribution(db, OWNER, isa.id, amount="300", on="2026-09-01")
    sipp = nw.create_item(db, OWNER, "asset", name="Alfie pension", asset_type="pension", subtype="junior sipp",
                          value="1000")
    mem.set_owners(db, OWNER, "asset", sipp.id, [alfie.id])
    leahs = _jisa(db, leah, "Leah JISA")
    rec.create_recurring(db, OWNER, name="Pocket money", account_id=family["current"].id, amount="5",
                         frequency="weekly", next_due="2026-11-01", to_account_id=saver.id)

    data = kids.children_overview(db, OWNER, today=TODAY)
    assert data["tax_year"] == "2026/27" and data["junior_isa_limit_pence"] == 900000
    by_name = {c["member"]["name"]: c for c in data["children"]}
    a = by_name["Alfie"]
    assert a["member"]["age"] == 8 and a["adult_on"] == "2036-03-01"
    assert a["net_worth_pence"] == 4000 + 50000 + 100000
    assert [(i["name"], i["locked"], i["unlock_date"], i["contributed_pence"]) for i in a["junior_isas"]] == [
        ("Alfie JISA", True, "2036-03-01", 30000)]
    assert a["junior_isa_allowance"]["remaining_pence"] == 870000
    assert [s["name"] for s in a["junior_sipps"]] == ["Alfie pension"]
    assert [(r["name"], r["amount_pence"], r["frequency_label"], r["to_account"]) for r in a["regular_transfers"]] == [
        ("Pocket money", 500, "Weekly", "Alfie saver")]
    lh = by_name["Leah"]
    # Born 29 Feb 2008: turned 18 on 1 Mar 2026, so her Junior ISA is open.
    assert lh["member"]["age"] == 18 and lh["junior_isas"][0]["locked"] is False
    assert lh["junior_isas"][0]["unlock_date"] == "2026-03-01" and lh["junior_isas"][0]["id"] == leahs.id
    assert data["total_pence"] == a["net_worth_pence"] + lh["net_worth_pence"]
    # The unlock date is the birthday itself: locked the day before.
    day_before = kids.children_overview(db, OWNER, today=date(2026, 2, 28))
    assert {c["member"]["name"]: c["junior_isas"][0]["locked"] for c in day_before["children"]}["Leah"] is True


def test_child_without_birthday_stays_locked(db, family):
    sam = mem.create_member(db, OWNER, name="Sam", relationship="child")
    _jisa(db, sam, "Sam JISA")
    child = next(c for c in kids.children_overview(db, OWNER, today=TODAY)["children"] if c["member"]["name"] == "Sam")
    assert child["junior_isas"][0]["locked"] is True and child["junior_isas"][0]["unlock_date"] is None


# ── Pocket money: regular transfers ─────────────────────────────────────


@pytest.fixture
def pocket(db, family):
    saver = fin.create_account(db, OWNER, name="Alfie saver", account_type="child_savings")
    mem.set_owners(db, OWNER, "account", saver.id, [family["alfie"].id])
    item = rec.create_recurring(db, OWNER, name="Pocket money", account_id=family["current"].id, amount="5",
                                frequency="weekly", next_due="2026-10-26", direction="in", category_id=None,
                                to_account_id=saver.id)
    return {**family, "saver": saver, "item": item}


def test_recurring_transfer_shape(db, pocket):
    item = pocket["item"]
    assert (item.direction, item.to_account_id) == ("out", pocket["saver"].id)  # always out of the payer
    with pytest.raises(fin.FinanceError):
        rec.create_recurring(db, OWNER, name="Loop", account_id=pocket["saver"].id, amount="1",
                             to_account_id=pocket["saver"].id)
    rec.update_recurring(db, OWNER, item.id, to_account_id=None)
    assert rec.get_recurring(db, OWNER, item.id).to_account_id is None
    rec.update_recurring(db, OWNER, item.id, to_account_id=pocket["saver"].id)
    assert rec.recurring_to_dict(rec.get_recurring(db, OWNER, item.id))["is_transfer"] is True


def test_pocket_money_confirm_records_a_transfer_and_undo_removes_it(db, pocket):
    item = pocket["item"]
    rec.confirm(db, OWNER, item.id, "2026-10-26")
    rows = fin.list_transactions(db, OWNER)
    assert sorted((r["account_id"], r["direction"], r["amount_pence"]) for r in rows) == sorted([
        (pocket["current"].id, "out", 500), (pocket["saver"].id, "in", 500)])
    assert all(r["between_members"] for r in rows)
    assert fin.account_balances(db, OWNER)[pocket["saver"].id] == 500
    start, end = fin.month_range(2026, 10)
    assert fin.summary(db, OWNER, start=start, end=end)["total_out_pence"] == 0  # never spending
    assert fin.summary(db, OWNER, start=start, end=end, person=pocket["alfie"].id)["moved_in_pence"] == 500
    rec.undo(db, OWNER, item.id, "2026-10-26")
    assert fin.list_transactions(db, OWNER) == []


def test_pocket_money_in_upcoming_and_budgets(db, pocket):
    item, alfie, me = pocket["item"], pocket["alfie"], pocket["me"]
    household = rec.upcoming(db, OWNER, today=TODAY)
    rows = [r for r in household["items"] if r["recurring_id"] == item.id]
    assert rows and rows[0]["is_transfer"] and rows[0]["share_direction"] == "out"
    projected = {p["name"]: p for p in household["projected_balances"]}
    # Weekly from 26 Oct: 26 Oct is overdue, 2 Nov is next month, so one occurrence before month end.
    assert projected["Alfie saver"]["expected_pence"] == 500
    assert projected["Current"]["expected_pence"] == -500
    kid = rec.upcoming(db, OWNER, today=TODAY, person=alfie.id)
    kid_row = next(r for r in kid["items"] if r["recurring_id"] == item.id)
    assert (kid_row["share_pence"], kid_row["share_direction"]) == (500, "in")
    mine = next(r for r in rec.upcoming(db, OWNER, today=TODAY, person=me.id)["items"] if r["recurring_id"] == item.id)
    assert (mine["share_pence"], mine["share_direction"]) == (500, "out")
    assert rec.committed_out(db, OWNER, date(2026, 10, 1), date(2026, 10, 31), today=TODAY) == {}


def test_pocket_money_matches_only_transfers_to_the_same_account(db, pocket):
    item = pocket["item"]
    fin.create_transaction(db, OWNER, account_id=pocket["current"].id, date="2026-10-26", amount="5",
                           direction="out", description="Pocket money")
    other = fin.create_account(db, OWNER, name="Elsewhere")
    fin.create_transfer(db, OWNER, from_account_id=pocket["current"].id, to_account_id=other.id,
                        date="2026-10-26", amount="5", description="Pocket money")
    assert rec.match_candidates(db, OWNER, item, date(2026, 10, 26)) == []
    out_side, _ = fin.create_transfer(db, OWNER, from_account_id=pocket["current"].id,
                                      to_account_id=pocket["saver"].id, date="2026-10-27", amount="5",
                                      description="Pocket money")
    assert [c["id"] for c in rec.match_candidates(db, OWNER, item, date(2026, 10, 26))] == [out_side.id]
    plain = next(t for t in fin.list_transactions(db, OWNER) if not t["transfer_id"])
    with pytest.raises(fin.FinanceError):
        rec.match(db, OWNER, item.id, "2026-10-26", plain["id"])
    rec.match(db, OWNER, item.id, "2026-10-26", out_side.id)


def test_make_recurring_from_a_transfer(db, pocket):
    _, in_side = fin.create_transfer(db, OWNER, from_account_id=pocket["current"].id,
                                     to_account_id=pocket["saver"].id, date="2026-10-03", amount="7.50",
                                     description="Pocket money")
    item = rec.make_recurring_from_transaction(db, OWNER, in_side.id, frequency="weekly")
    assert (item.account_id, item.to_account_id, item.amount_pence, item.direction) == (
        pocket["current"].id, pocket["saver"].id, 750, "out")


# ── Migration v6 -> v7 ──────────────────────────────────────────────────

OLD_ACCOUNTS_DDL = """CREATE TABLE finance_accounts (
	id VARCHAR NOT NULL,
	owner VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	account_type VARCHAR NOT NULL,
	opening_balance_pence INTEGER NOT NULL,
	archived BOOLEAN NOT NULL,
	sort_order INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_finance_account_type CHECK (account_type IN ('current', 'savings', 'credit_card', 'cash'))
)"""


def _v6_database(path, monkeypatch):
    """Schema 6 as Finance v3 step 3 left it: the old account type CHECK."""
    engine = _engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(OLD_ACCOUNTS_DDL))
        conn.execute(text("CREATE INDEX ix_finance_accounts_owner ON finance_accounts (owner)"))
    others = [t for t in FINANCE_TABLES if t.name not in ("finance_accounts", "finance_settings", "finance_meta")]
    Base.metadata.create_all(bind=engine, tables=others)
    Base.metadata.create_all(bind=engine, tables=[FinanceMeta.__table__])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO finance_meta VALUES ('schema_version', '6')"))
    _freeze(monkeypatch)
    db = sessionmaker(bind=engine)()
    me = mem.ensure_household(db, OWNER)
    a = fin.create_account(db, OWNER, name="Current", opening_balance="100")
    b = fin.create_account(db, OWNER, name="Savings", account_type="savings")
    fin.create_transfer(db, OWNER, from_account_id=a.id, to_account_id=b.id, date="2026-10-02", amount="50")
    fin.create_transaction(db, OWNER, account_id=a.id, date="2026-10-01", amount="12.34", direction="out")
    rec.create_recurring(db, OWNER, name="Rent", account_id=a.id, amount="10", next_due="2026-11-01")
    db.commit()
    db.close()
    return engine, me


def _rows(path, table):
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute(f"SELECT * FROM {table}").fetchall())
    finally:
        conn.close()


def test_upgrade_to_v7_rebuilds_accounts_without_changing_data(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    engine, _me = _v6_database(path, monkeypatch)
    assert migrations._current_version(engine) == 6
    with pytest.raises(Exception):  # the old CHECK refuses child accounts
        with engine.begin() as conn:
            conn.execute(text("UPDATE finance_accounts SET account_type = 'child_savings'"))
    tables = ("finance_accounts", "finance_transactions", "finance_recurring", "finance_item_owners",
              "finance_members")
    before = {t: _rows(path, t) for t in tables}

    backups = tmp_path / "backups"
    assert migrations.migrate(engine, backup_dir=str(backups)) == migrations.SCHEMA_VERSION
    assert migrations.migrate(engine, backup_dir=str(backups)) == migrations.SCHEMA_VERSION
    assert [p.name.split("-to-")[0] for p in backups.iterdir()] == ["app-before-finance-v6"]
    after = {t: _rows(path, t) for t in tables}
    assert after["finance_accounts"] == before["finance_accounts"]
    assert after["finance_transactions"] == before["finance_transactions"]
    assert after["finance_item_owners"] == before["finance_item_owners"]
    # Existing recurring rows only gain the new, empty column.
    assert [row[:-1] if len(row) > len(before["finance_recurring"][0]) else row
            for row in after["finance_recurring"]] == before["finance_recurring"]

    conn = sqlite3.connect(path)
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'finance_accounts'").fetchone()[0]
        assert "child_savings" in sql and "finance_accounts_v7" not in sql
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'ix_finance_accounts_owner'").fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_v7'").fetchall()
    finally:
        conn.close()

    db = sessionmaker(bind=engine)()
    assert sorted(fin.account_balances(db, OWNER).values()) == [10000 - 5000 - 1234, 5000]
    kid = fin.create_account(db, OWNER, name="Kid", account_type="child_savings")
    assert db.get(FinanceAccount, kid.id).account_type == "child_savings"
    # Foreign keys are enforced again after the rebuild.
    with pytest.raises(Exception):
        db.execute(text("INSERT INTO finance_transactions (id, owner, account_id, date, amount_pence, direction, "
                        "category_id, description, notes, created_at, updated_at) VALUES ('x', 'alice', 'missing', "
                        "'2026-01-01', 1, 'in', NULL, '', '', '2026-01-01', '2026-01-01')"))
        db.flush()
    db.rollback()
    db.close()
    engine.dispose()


def test_failed_rebuild_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    engine, _ = _v6_database(path, monkeypatch)
    before = _rows(path, "finance_accounts")
    monkeypatch.setattr(migrations, "NEW_ACCOUNT_CHECK", "account_type IN ('nothing')")  # copy will fail the CHECK
    with pytest.raises(Exception):
        migrations._rebuild_accounts_table(engine)
    assert _rows(path, "finance_accounts") == before
    conn = sqlite3.connect(path)
    try:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'finance_accounts_v7'").fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    engine.dispose()


# ── Routes ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    engine = _engine()
    _freeze(monkeypatch)
    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_children_routes(client):
    assert "child_savings" in _ok(client.get("/api/finance/meta"))["account_types"]
    kid = _ok(client.post("/api/finance/members", json={"name": "Alfie", "relationship": "son",
                                                        "date_of_birth": "2018-03-01"}))
    saver = _ok(client.post("/api/finance/accounts", json={"name": "Alfie saver", "account_type": "child_savings",
                                                            "owner_ids": [kid["id"]]}))
    current = _ok(client.post("/api/finance/accounts", json={"name": "Current"}))
    item = _ok(client.post("/api/finance/recurring", json={
        "name": "Pocket money", "account_id": current["id"], "amount": "5", "frequency": "weekly",
        "to_account_id": saver["id"]}))
    assert item["is_transfer"] is True and item["direction"] == "out"
    data = _ok(client.get("/api/finance/children"))
    assert [c["member"]["name"] for c in data["children"]] == ["Alfie"]
    assert data["children"][0]["regular_transfers"][0]["name"] == "Pocket money"
    assert _ok(client.get("/api/finance/settings")) == {"junior_isa_limit_pence": 900000}
    assert _ok(client.patch("/api/finance/settings", json={"junior_isa_limit": "9500"}))[
        "junior_isa_limit_pence"] == 950000
    assert client.patch("/api/finance/settings", json={"junior_isa_limit": "lots"}).status_code == 400
    assert _ok(client.patch(f"/api/finance/recurring/{item['id']}", json={"to_account_id": None}))[
        "is_transfer"] is False
