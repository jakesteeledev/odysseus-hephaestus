"""Finance v3 step 1: household members, default ownership and the v6 migration."""

import json
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
from src.finance import members as mem
from src.finance import migrations
from src.finance import networth as nw
from src.finance import service as fin
from src.finance.models import (
    V1_TABLES,
    V2_TABLES,
    V3_TABLES,
    V4_TABLES,
    V5_TABLES,
    FinanceItemOwner,
    FinanceMember,
    ensure_tables,
)

OWNER = "alice"
TODAY = date(2026, 10, 28)


def _engine(url="sqlite://", **kw):
    engine = create_engine(url, **kw)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture
def db(monkeypatch):
    engine = _engine(poolclass=StaticPool, connect_args={"check_same_thread": False})
    ensure_tables(engine)
    monkeypatch.setattr(mem, "_today_for_owner", lambda: TODAY)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _owner_rows(db, kind=None):
    query = db.query(FinanceItemOwner.item_kind, FinanceItemOwner.item_id, FinanceItemOwner.member_id)
    if kind:
        query = query.filter(FinanceItemOwner.item_kind == kind)
    return query.all()


# ── Names ───────────────────────────────────────────────────────────────


def test_primary_member_is_named_after_the_account(db):
    member = mem.ensure_household(db, "jake")
    assert member.name == "Jake" and member.is_primary and member.relationship == "me"
    assert mem.member_to_dict(member)["display_name"] == "Jake (Me)"
    assert mem.ensure_household(db, "jake").id == member.id  # only ever one


def test_local_owner_uses_the_admin_account_name(tmp_path, monkeypatch):
    import src.constants as constants
    from src.owner_identity import DEFAULT_LOCAL_OWNER

    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"users": {"guest": {"is_admin": False}, "jake": {"is_admin": True}}}))
    monkeypatch.setattr(constants, "AUTH_FILE", str(auth))
    assert mem.default_member_name(DEFAULT_LOCAL_OWNER) == "Jake"
    monkeypatch.setattr(constants, "AUTH_FILE", str(tmp_path / "missing.json"))
    assert mem.default_member_name(DEFAULT_LOCAL_OWNER) == "Me"


def test_initials_and_age(db):
    sarah = mem.create_member(db, OWNER, name="Sarah Jane Smith", relationship="partner")
    alfie = mem.create_member(db, OWNER, name="Alfie", relationship="son", date_of_birth="2018-10-29")
    assert mem.member_to_dict(sarah)["initials"] == "SS"
    data = mem.member_to_dict(alfie)
    assert data["initials"] == "A" and data["relationship"] == "child"
    assert data["age"] == 7  # eighth birthday is tomorrow
    assert mem.member_to_dict(mem.update_member(db, OWNER, alfie.id, initials="af"))["initials"] == "AF"


# ── Members ─────────────────────────────────────────────────────────────


def test_members_list_primary_first_and_colours_rotate(db):
    mem.create_member(db, OWNER, name="Sarah", relationship="partner")
    mem.create_member(db, OWNER, name="Alfie", relationship="child")
    members = mem.list_members(db, OWNER)
    assert [m["name"] for m in members] == ["Alice", "Sarah", "Alfie"]
    assert [m["color"] for m in members] == ["blue", "green", "purple"]
    assert members[0]["is_primary"] and not members[1]["is_primary"]


def test_member_validation(db):
    mem.create_member(db, OWNER, name="Sarah", relationship="partner")
    with pytest.raises(fin.FinanceConflict):
        mem.create_member(db, OWNER, name="sarah")
    with pytest.raises(fin.FinanceError):
        mem.create_member(db, OWNER, name="Bob", relationship="me")
    with pytest.raises(fin.FinanceError):
        mem.create_member(db, OWNER, name="Bob", relationship="cousin")
    with pytest.raises(fin.FinanceError):
        mem.create_member(db, OWNER, name="Bob", date_of_birth="2030-01-01")
    with pytest.raises(fin.FinanceError):
        mem.create_member(db, OWNER, name="Bob", color="#ff0000")
    with pytest.raises(fin.FinanceError):
        mem.create_member(db, OWNER, name="")


def test_primary_member_cannot_be_archived_deleted_or_change_relationship(db):
    me = mem.ensure_household(db, OWNER)
    with pytest.raises(fin.FinanceConflict):
        mem.update_member(db, OWNER, me.id, archived=True)
    with pytest.raises(fin.FinanceConflict):
        mem.delete_member(db, OWNER, me.id)
    with pytest.raises(fin.FinanceError):
        mem.update_member(db, OWNER, me.id, relationship="partner")
    assert mem.update_member(db, OWNER, me.id, name="Ali").name == "Ali"


def test_member_who_owns_something_can_only_be_archived(db):
    sarah = mem.create_member(db, OWNER, name="Sarah", relationship="partner")
    account = fin.create_account(db, OWNER, name="Joint")
    db.add(FinanceItemOwner(owner=OWNER, item_kind="account", item_id=account.id, member_id=sarah.id))
    db.flush()
    with pytest.raises(fin.FinanceConflict):
        mem.delete_member(db, OWNER, sarah.id)
    mem.update_member(db, OWNER, sarah.id, archived=True)
    assert [m["name"] for m in mem.list_members(db, OWNER)] == ["Alice"]
    assert len(mem.list_members(db, OWNER, include_archived=True)) == 2

    bob = mem.create_member(db, OWNER, name="Bob")
    mem.delete_member(db, OWNER, bob.id)
    assert db.query(FinanceMember).filter(FinanceMember.id == bob.id).first() is None


def test_members_are_scoped_to_the_odysseus_user(db):
    mem.create_member(db, OWNER, name="Sarah")
    assert [m["name"] for m in mem.list_members(db, "bob")] == ["Bob"]
    with pytest.raises(fin.FinanceNotFound):
        mem.update_member(db, "bob", mem.list_members(db, OWNER)[1]["id"], name="X")


# ── Default ownership ───────────────────────────────────────────────────


def test_new_items_belong_to_the_primary_member_and_links_go_on_delete(db):
    me = mem.ensure_household(db, OWNER)
    account = fin.create_account(db, OWNER, name="Current")
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property", value="300000")
    loan = nw.create_item(db, OWNER, "liability", name="Car loan", liability_type="loan")
    assert sorted(_owner_rows(db)) == sorted([("account", account.id, me.id), ("asset", house.id, me.id),
                                              ("liability", loan.id, me.id)])
    assert mem.owners_of(db, OWNER, "account") == {account.id: [me.id]}

    fin.delete_account(db, OWNER, account.id)
    nw.delete_item(db, OWNER, "liability", loan.id)
    assert _owner_rows(db) == [("asset", house.id, me.id)]


def test_joint_owners_come_back_in_a_stable_order(db):
    me = mem.ensure_household(db, OWNER)
    sarah = mem.create_member(db, OWNER, name="Sarah")
    account = fin.create_account(db, OWNER, name="Joint")
    db.add(FinanceItemOwner(owner=OWNER, item_kind="account", item_id=account.id, member_id=sarah.id))
    db.flush()
    assert mem.owners_of(db, OWNER, "account", [account.id]) == {account.id: [me.id, sarah.id]}


# ── Migration v5 -> v6 ─────────────────────────────────────────────────


def _v5_database(path, monkeypatch):
    """Tables and data as Finance v2 (schema 5) left them: no members, no owner rows."""
    engine = _engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine, tables=V1_TABLES + V2_TABLES + V3_TABLES + V4_TABLES + V5_TABLES)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE finance_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"))
        conn.execute(text("INSERT INTO finance_meta VALUES ('schema_version', '5')"))
    with monkeypatch.context() as patch:
        patch.setattr(mem, "assign_default_owner", lambda *args, **kwargs: None)
        db = sessionmaker(bind=engine)()
        for owner in (OWNER, "bob"):
            current = fin.create_account(db, owner, name="Current", opening_balance="100")
            fin.create_account(db, owner, name="Old", opening_balance="0")
            fin.update_account(db, owner, current.id, archived=False)
            cats = {c["name"]: c["id"] for c in fin.list_categories(db, owner)}
            fin.create_transaction(db, owner, account_id=current.id, date="2026-10-01", amount="12.34",
                                   direction="out", category_id=cats["Groceries"])
            house = nw.create_item(db, owner, "asset", name="Home", asset_type="property", value="300000")
            nw.create_item(db, owner, "liability", name="Mortgage", liability_type="mortgage",
                           linked_asset_id=house.id, value="200000")
        db.commit()
        db.close()
    return engine


def _dump_all(path, tables):
    conn = sqlite3.connect(path)
    try:
        return {t.name: sorted(conn.execute(f"SELECT * FROM {t.name}").fetchall()) for t in tables}
    finally:
        conn.close()


def test_upgrade_to_v6_assigns_everything_to_a_primary_member(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    engine = _v5_database(path, monkeypatch)
    old_tables = V1_TABLES + V2_TABLES + V3_TABLES + V4_TABLES + V5_TABLES
    before = _dump_all(path, old_tables)
    assert migrations._current_version(engine) == 5

    backups = tmp_path / "backups"
    assert migrations.migrate(engine, backup_dir=str(backups)) == 6
    assert migrations.migrate(engine, backup_dir=str(backups)) == 6  # runs once
    assert [p.name.split("-to-")[0] for p in backups.iterdir()] == ["app-before-finance-v5"]
    assert _dump_all(path, old_tables) == before  # not a single existing row changed

    db = sessionmaker(bind=engine)()
    for owner, name in ((OWNER, "Alice"), ("bob", "Bob")):
        members = db.query(FinanceMember).filter(FinanceMember.owner == owner).all()
        assert [(m.name, m.is_primary, m.relationship, m.linked_user) for m in members] == [(name, True, "me", None)]
        owned = db.query(FinanceItemOwner).filter(FinanceItemOwner.owner == owner).all()
        assert {o.member_id for o in owned} == {members[0].id}
        assert sorted(o.item_kind for o in owned) == ["account", "account", "asset", "liability"]
    # Balances and net worth read the same as before.
    assert sorted(fin.account_balances(db, OWNER).values()) == [0, 10000 - 1234]
    assert nw.net_worth(db, OWNER)["net_worth_pence"] == 10000 - 1234 + 30000000 - 20000000
    db.close()
    engine.dispose()


def test_items_left_without_an_owner_are_claimed_by_the_primary_member(db):
    me = mem.ensure_household(db, OWNER)
    account = fin.create_account(db, OWNER, name="Current")
    db.query(FinanceItemOwner).delete()
    db.flush()
    mem.list_members(db, OWNER)
    assert _owner_rows(db) == [("account", account.id, me.id)]


# ── Routes ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    engine = _engine(poolclass=StaticPool, connect_args={"check_same_thread": False})
    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def test_member_routes(client):
    members = client.get("/api/finance/members").json()["members"]
    assert [(m["display_name"], m["owns_count"]) for m in members] == [("Alice (Me)", 0)]
    assert client.get("/api/finance/meta").json()["relationships"] == ["partner", "child", "other"]

    sarah = client.post("/api/finance/members", json={"name": "Sarah", "relationship": "partner"}).json()
    assert sarah["relationship_label"] == "Partner" and sarah["owns_count"] == 0
    assert client.post("/api/finance/members", json={"name": "Sarah"}).status_code == 409
    assert client.post("/api/finance/members", json={"name": "X", "relationship": "me"}).status_code == 400

    alfie = client.post("/api/finance/members",
                        json={"name": "Alfie", "relationship": "child", "date_of_birth": "2018-03-01"}).json()
    updated = client.patch(f"/api/finance/members/{alfie['id']}", json={"date_of_birth": None, "color": "red"}).json()
    assert updated["date_of_birth"] is None and updated["color"] == "red"

    client.post("/api/finance/accounts", json={"name": "Current"})
    me = client.get("/api/finance/members").json()["members"][0]
    assert me["owns_count"] == 1
    assert client.delete(f"/api/finance/members/{me['id']}").status_code == 409
    assert client.delete(f"/api/finance/members/{sarah['id']}").json() == {"ok": True}
    assert client.patch("/api/finance/members/nope", json={"name": "X"}).status_code == 404
