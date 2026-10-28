"""Finance v3 step 2: joint ownership and per-person figures."""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes
from src.finance import members as mem
from src.finance import networth as nw
from src.finance import service as fin
from src.finance.models import ensure_tables

OWNER = "alice"


def _engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture
def db(monkeypatch):
    engine = _engine()
    ensure_tables(engine)
    monkeypatch.setattr(fin, "_today_for_owner", lambda: date(2026, 10, 28))
    monkeypatch.setattr(mem, "_today_for_owner", lambda: date(2026, 10, 28))
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def household(db):
    """Alice (primary), her partner Sam and their child Alfie."""
    me = mem.ensure_household(db, OWNER)
    sam = mem.create_member(db, OWNER, name="Sam", relationship="partner")
    alfie = mem.create_member(db, OWNER, name="Alfie", relationship="child", date_of_birth="2018-03-01")
    return me, sam, alfie


def _per_member(worth):
    return {row["member"]["name"]: row for row in worth["by_member"]}


# ── Splitting ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("amount, count, expected", [
    (30000, 1, [30000]),
    (30000, 2, [15000, 15000]),
    (30001, 2, [15001, 15000]),
    (1001, 3, [335, 333, 333]),
    (1000, 3, [334, 333, 333]),
    (2, 3, [2, 0, 0]),
    (0, 3, [0, 0, 0]),
    (-1001, 3, [-335, -333, -333]),
    (-1, 2, [-1, 0]),
])
def test_split_pence(amount, count, expected):
    assert mem.split_pence(amount, count) == expected


def test_shares_always_add_up_exactly():
    for amount in list(range(-50, 51)) + [99_999_999, -123_457]:
        for count in range(1, 7):
            shares = mem.split_pence(amount, count)
            assert sum(shares) == amount and len(shares) == count
            assert max(shares) - min(shares) <= count - 1 if amount >= 0 else True


def test_share_of():
    assert mem.share_of(1001, ["a", "b", "c"], None) == 1001  # household: the whole amount
    assert [mem.share_of(1001, ["a", "b", "c"], m) for m in "abc"] == [335, 333, 333]
    assert mem.share_of(1001, ["a", "b"], "z") == 0


# ── Setting owners ──────────────────────────────────────────────────────


def test_set_owners_replaces_and_orders_owners(db, household):
    me, sam, _ = household
    account = fin.create_account(db, OWNER, name="Joint")
    assert mem.set_owners(db, OWNER, "account", account.id, [sam.id, me.id, sam.id]) == [me.id, sam.id]
    assert mem.set_owners(db, OWNER, "account", account.id, [sam.id]) == [sam.id]


def test_set_owners_validation(db, household):
    me, sam, _ = household
    account = fin.create_account(db, OWNER, name="Joint")
    with pytest.raises(fin.FinanceError):
        mem.set_owners(db, OWNER, "account", account.id, [])
    with pytest.raises(fin.FinanceNotFound):
        mem.set_owners(db, OWNER, "account", account.id, ["nobody"])
    with pytest.raises(fin.FinanceNotFound):
        mem.set_owners(db, OWNER, "asset", account.id, [me.id])  # wrong kind
    bob_member = mem.ensure_household(db, "bob")
    with pytest.raises(fin.FinanceNotFound):
        mem.set_owners(db, OWNER, "account", account.id, [bob_member.id])  # another user's member


def test_archived_members_keep_items_but_get_no_new_ones(db, household):
    me, sam, _ = household
    account = fin.create_account(db, OWNER, name="Joint")
    mem.set_owners(db, OWNER, "account", account.id, [me.id, sam.id])
    mem.update_member(db, OWNER, sam.id, archived=True)
    assert mem.set_owners(db, OWNER, "account", account.id, [me.id, sam.id]) == [me.id, sam.id]  # kept
    other = fin.create_account(db, OWNER, name="Other")
    with pytest.raises(fin.FinanceError):
        mem.set_owners(db, OWNER, "account", other.id, [sam.id])


# ── Net worth per person ────────────────────────────────────────────────


def test_joint_house_and_mortgage_split_equally(db, household):
    me, sam, _ = household
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property", value="300000")
    mortgage = nw.create_item(db, OWNER, "liability", name="Mortgage", liability_type="mortgage",
                              linked_asset_id=house.id, value="200000")
    for kind, item in (("asset", house), ("liability", mortgage)):
        mem.set_owners(db, OWNER, kind, item.id, [me.id, sam.id])

    worth = nw.net_worth(db, OWNER)
    assert (worth["assets_total_pence"], worth["liabilities_total_pence"]) == (30_000_000, 20_000_000)
    people = _per_member(worth)
    for name in ("Alice", "Sam"):
        assert people[name]["assets_pence"] == 15_000_000
        assert people[name]["liabilities_pence"] == 10_000_000
        assert people[name]["net_worth_pence"] == 5_000_000
    assert people["Alfie"]["net_worth_pence"] == 0

    mine = nw.net_worth(db, OWNER, person=sam.id)
    assert mine["person"] == sam.id and mine["net_worth_pence"] == 5_000_000
    home = mine["assets"][0]["items"][0]
    assert (home["value_pence"], home["full_value_pence"], home["joint"]) == (15_000_000, 30_000_000, True)


def test_household_equals_sum_of_members_with_odd_pence(db, household):
    me, sam, alfie = household
    current = fin.create_account(db, OWNER, name="Current", opening_balance="1000.01")
    joint = fin.create_account(db, OWNER, name="Joint savings", opening_balance="100.01")
    card = fin.create_account(db, OWNER, name="Card", account_type="credit_card", opening_balance="-250.03")
    car = nw.create_item(db, OWNER, "asset", name="Car", asset_type="vehicle", value="9999.99")
    loan = nw.create_item(db, OWNER, "liability", name="Loan", liability_type="loan", value="1234.57")
    isa = nw.create_item(db, OWNER, "asset", name="Junior ISA", asset_type="investment", value="500.00")
    mem.set_owners(db, OWNER, "account", joint.id, [me.id, sam.id, alfie.id])
    mem.set_owners(db, OWNER, "account", card.id, [me.id, sam.id])
    mem.set_owners(db, OWNER, "asset", car.id, [me.id, sam.id, alfie.id])
    mem.set_owners(db, OWNER, "liability", loan.id, [sam.id, alfie.id])
    mem.set_owners(db, OWNER, "asset", isa.id, [alfie.id])
    del current

    worth = nw.net_worth(db, OWNER)
    rows = worth["by_member"]
    for key in ("assets_pence", "liabilities_pence", "net_worth_pence"):
        household_key = {"assets_pence": "assets_total_pence", "liabilities_pence": "liabilities_total_pence",
                         "net_worth_pence": "net_worth_pence"}[key]
        assert sum(r[key] for r in rows) == worth[household_key]
    # Each person's own view agrees with the breakdown.
    for row in rows:
        view = nw.net_worth(db, OWNER, person=row["member"]["id"])
        assert view["net_worth_pence"] == row["net_worth_pence"]
        assert view["assets_total_pence"] == row["assets_pence"]
    # The joint savings account's 10001p went 3335 / 3333 / 3333, first owner first.
    joint_shares = [nw.net_worth(db, OWNER, person=m.id)["assets"][0]["items"] for m in (me, sam, alfie)]
    assert [next(i["value_pence"] for i in items if i["id"] == joint.id) for items in joint_shares] == [3335, 3333, 3333]


def test_accounts_and_items_per_person(db, household):
    me, sam, _ = household
    mine = fin.create_account(db, OWNER, name="Mine", opening_balance="100")
    joint = fin.create_account(db, OWNER, name="Joint", opening_balance="300.01")
    mem.set_owners(db, OWNER, "account", joint.id, [me.id, sam.id])
    pension = nw.create_item(db, OWNER, "asset", name="Sam's pension", asset_type="pension", value="5000")
    mem.set_owners(db, OWNER, "asset", pension.id, [sam.id])

    household_view = fin.accounts_overview(db, OWNER)
    assert household_view["total_pence"] == 40001
    assert {a["name"]: (a["owner_ids"], a["joint"]) for a in household_view["accounts"]} == {
        "Mine": ([me.id], False), "Joint": ([me.id, sam.id], True)}
    sams = fin.accounts_overview(db, OWNER, person=sam.id)
    assert [(a["name"], a["balance_pence"], a["share_pence"]) for a in sams["accounts"]] == [("Joint", 30001, 15000)]
    alices = fin.accounts_overview(db, OWNER, person=me.id)
    assert alices["total_pence"] + sams["total_pence"] == household_view["total_pence"]

    assert [i["name"] for i in nw.list_items(db, OWNER, "asset", person=me.id)] == []
    assert [(i["name"], i["share_pence"]) for i in nw.list_items(db, OWNER, "asset", person=sam.id)] == [
        ("Sam's pension", 500000)]
    del mine


def test_changing_owners_changes_historical_figures(db, household):
    me, sam, _ = household
    account = fin.create_account(db, OWNER, name="Savings", opening_balance="0")
    fin.create_transaction(db, OWNER, account_id=account.id, date="2026-01-15", amount="1000", direction="in")
    past = date(2026, 2, 1)
    assert nw.net_worth(db, OWNER, as_of=past, person=sam.id)["net_worth_pence"] == 0

    mem.set_owners(db, OWNER, "account", account.id, [me.id, sam.id])
    assert nw.net_worth(db, OWNER, as_of=past, person=sam.id)["net_worth_pence"] == 50000
    assert nw.net_worth(db, OWNER, as_of=past, person=me.id)["net_worth_pence"] == 50000
    assert nw.net_worth(db, OWNER, as_of=past)["net_worth_pence"] == 100000


def test_mortgage_warns_when_owners_differ_from_the_property(db, household):
    me, sam, _ = household
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property", value="300000")
    mortgage = nw.create_item(db, OWNER, "liability", name="Mortgage", liability_type="mortgage",
                              linked_asset_id=house.id, value="200000")
    warning = lambda: nw.list_items(db, OWNER, "liability")[0]["owner_warning"]  # noqa: E731
    assert warning() is None
    mem.set_owners(db, OWNER, "asset", house.id, [me.id, sam.id])
    assert warning() == "Its owners are different from the owners of Home."
    mem.set_owners(db, OWNER, "liability", mortgage.id, [sam.id, me.id])
    assert warning() is None


# ── Routes ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    engine = _engine()
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


def test_ownership_routes(client):
    me = _ok(client.get("/api/finance/members"))["members"][0]
    sam = _ok(client.post("/api/finance/members", json={"name": "Sam", "relationship": "partner"}))

    solo = _ok(client.post("/api/finance/accounts", json={"name": "Mine", "opening_balance": "100"}))
    assert solo["owner_ids"] == [me["id"]] and solo["joint"] is False
    joint = _ok(client.post("/api/finance/accounts", json={"name": "Joint", "opening_balance": "301",
                                                            "owner_ids": [sam["id"], me["id"]]}))
    assert joint["owner_ids"] == [me["id"], sam["id"]] and joint["joint"] is True

    house = _ok(client.post("/api/finance/assets", json={"name": "Home", "type": "property", "value": "300000",
                                                          "owner_ids": [me["id"], sam["id"]]}))
    assert house["joint"] is True
    mortgage = _ok(client.post("/api/finance/liabilities", json={"name": "Mortgage", "type": "mortgage",
                                                                  "linked_asset_id": house["id"], "value": "200000"}))
    assert "owners of Home" in mortgage["owner_warning"]
    assert _ok(client.put(f"/api/finance/liabilities/{mortgage['id']}/owners",
                          json={"member_ids": [me["id"], sam["id"]]}))["owner_ids"] == [me["id"], sam["id"]]
    assert _ok(client.get("/api/finance/liabilities"))["liabilities"][0]["owner_warning"] is None

    assert client.put(f"/api/finance/accounts/{solo['id']}/owners", json={"member_ids": []}).status_code == 400
    assert client.put("/api/finance/budgets/x/owners", json={"member_ids": [me["id"]]}).status_code in (404, 405, 422)

    sams = _ok(client.get(f"/api/finance/accounts?person={sam['id']}"))
    assert [a["name"] for a in sams["accounts"]] == ["Joint"] and sams["total_pence"] == 15050
    assert _ok(client.get("/api/finance/accounts?person=household"))["total_pence"] == 40100
    assert client.get("/api/finance/accounts?person=nobody").status_code == 404

    worth = _ok(client.get("/api/finance/networth"))
    assert {r["member"]["name"]: r["net_worth_pence"] for r in worth["by_member"]} == {
        "Alice": 10000 + 15050 + 5_000_000, "Sam": 15050 + 5_000_000}
    assert _ok(client.get(f"/api/finance/networth?person={sam['id']}"))["net_worth_pence"] == 15050 + 5_000_000
    assert [i["name"] for i in _ok(client.get(f"/api/finance/assets?person={sam['id']}"))["assets"]] == ["Home"]
