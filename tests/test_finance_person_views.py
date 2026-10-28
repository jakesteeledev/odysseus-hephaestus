"""Finance v3 step 3: per-person transactions, summaries, budgets, upcoming and charts."""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes
from src.finance import budgets as bud
from src.finance import insights
from src.finance import members as mem
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance.models import ensure_tables

OWNER = "alice"
TODAY = date(2026, 10, 28)


def _engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def _freeze(monkeypatch):
    for module in (fin, mem, insights, rec, bud):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)


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
def home(db):
    """Alice and Sam with a joint account, Alice's own account and Alfie's savings."""
    me = mem.ensure_household(db, OWNER)
    sam = mem.create_member(db, OWNER, name="Sam", relationship="partner")
    alfie = mem.create_member(db, OWNER, name="Alfie", relationship="child", date_of_birth="2018-03-01")
    joint = fin.create_account(db, OWNER, name="Joint", opening_balance="0")
    mem.set_owners(db, OWNER, "account", joint.id, [me.id, sam.id])
    mine = fin.create_account(db, OWNER, name="Alice current", opening_balance="0")
    kids = fin.create_account(db, OWNER, name="Alfie savings", account_type="savings", opening_balance="0")
    mem.set_owners(db, OWNER, "account", kids.id, [alfie.id])
    food = fin.create_category(db, OWNER, name="Food")
    return {"me": me, "sam": sam, "alfie": alfie, "joint": joint, "mine": mine, "kids": kids, "food": food}


def _tx(db, account, day, amount, direction="out", category=None):
    return fin.create_transaction(db, OWNER, account_id=account.id, date=day, amount=amount, direction=direction,
                                  category_id=category.id if category else None)


def _members(h):
    return [h["me"].id, h["sam"].id, h["alfie"].id]


def test_transactions_per_person(db, home):
    h = home
    _tx(db, h["joint"], "2026-10-02", "100.01", category=h["food"])
    _tx(db, h["mine"], "2026-10-03", "20")
    _tx(db, h["kids"], "2026-10-04", "5", direction="in")

    everyone = fin.list_transactions(db, OWNER)
    assert sorted(t["share_pence"] for t in everyone) == [500, 2000, 10001]
    sams = fin.list_transactions(db, OWNER, fin.TransactionFilter(person=h["sam"].id))
    assert [(t["account_id"], t["share_pence"], t["owner_ids"]) for t in sams] == [
        (h["joint"].id, 5000, [h["me"].id, h["sam"].id])]
    alices = fin.list_transactions(db, OWNER, fin.TransactionFilter(person=h["me"].id))
    assert sorted(t["share_pence"] for t in alices) == [2000, 5001]
    assert [t["share_pence"] for t in fin.list_transactions(
        db, OWNER, fin.TransactionFilter(person=h["alfie"].id))] == [500]


def test_summary_household_equals_sum_of_people(db, home):
    h = home
    _tx(db, h["joint"], "2026-10-02", "100.01", category=h["food"])
    _tx(db, h["joint"], "2026-10-05", "33.33", category=h["food"])
    _tx(db, h["mine"], "2026-10-03", "20", category=h["food"])
    _tx(db, h["joint"], "2026-10-06", "2500", direction="in")
    start, end = fin.month_range(2026, 10)
    household = fin.summary(db, OWNER, start=start, end=end)
    people = [fin.summary(db, OWNER, start=start, end=end, person=pid) for pid in _members(h)]
    for key in ("total_in_pence", "total_out_pence", "net_pence"):
        assert household[key] == sum(p[key] for p in people), key
    food = lambda s: next((c["total_pence"] for c in s["spend_by_category"] if c["name"] == "Food"), 0)  # noqa: E731
    assert food(household) == 10001 + 3333 + 2000
    # Joint food spend is split per account total (13334 -> 6667 each), plus Alice's own 2000.
    assert [food(p) for p in people] == [6667 + 2000, 6667, 0]
    assert household["moved_in_pence"] == household["moved_out_pence"] == 0


def test_transfers_between_members(db, home):
    h = home
    _tx(db, h["mine"], "2026-10-01", "500", direction="in")
    # Alice -> Alfie: money moves from one member to another.
    fin.create_transfer(db, OWNER, from_account_id=h["mine"].id, to_account_id=h["kids"].id,
                        date="2026-10-10", amount="10")
    # Alice -> joint: half of it is now Sam's.
    fin.create_transfer(db, OWNER, from_account_id=h["mine"].id, to_account_id=h["joint"].id,
                        date="2026-10-11", amount="100")
    start, end = fin.month_range(2026, 10)
    me, sam, alfie = (fin.summary(db, OWNER, start=start, end=end, person=pid) for pid in _members(h))
    assert (me["moved_out_pence"], me["moved_in_pence"]) == (1000 + 5000, 0)
    assert (sam["moved_in_pence"], sam["moved_out_pence"]) == (5000, 0)
    assert (alfie["moved_in_pence"], alfie["moved_out_pence"]) == (1000, 0)
    household = fin.summary(db, OWNER, start=start, end=end)
    assert household["moved_in_pence"] == household["moved_out_pence"] == 0
    # Transfers are never income or spending, for anyone.
    assert household["total_out_pence"] == me["total_out_pence"] == 0
    assert sum(p["moved_in_pence"] - p["moved_out_pence"] for p in (me, sam, alfie)) == 0

    rows = fin.list_transactions(db, OWNER, fin.TransactionFilter(person=h["alfie"].id))
    assert [(r["direction"], r["between_members"]) for r in rows] == [("in", True)]

    # Same owners on both sides: not between members, and nobody's position moves.
    other = fin.create_account(db, OWNER, name="Alice saver", opening_balance="0")
    fin.create_transfer(db, OWNER, from_account_id=h["mine"].id, to_account_id=other.id,
                        date="2026-10-12", amount="50")
    same = [r for r in fin.list_transactions(db, OWNER) if r["account_id"] == other.id]
    assert same[0]["between_members"] is False
    assert fin.summary(db, OWNER, start=start, end=end, person=h["me"].id)["moved_out_pence"] == 6000


def test_owner_change_updates_past_transactions(db, home):
    h = home
    _tx(db, h["mine"], "2025-06-01", "80", category=h["food"])
    start, end = fin.month_range(2025, 6)
    assert fin.summary(db, OWNER, start=start, end=end, person=h["sam"].id)["total_out_pence"] == 0
    mem.set_owners(db, OWNER, "account", h["mine"].id, [h["me"].id, h["sam"].id])
    assert fin.summary(db, OWNER, start=start, end=end, person=h["sam"].id)["total_out_pence"] == 4000
    assert fin.summary(db, OWNER, start=start, end=end)["total_out_pence"] == 8000


def test_budgets_stay_household_with_person_share(db, home):
    h = home
    bud.set_budget(db, OWNER, h["food"].id, "300", month="2026-10")
    _tx(db, h["joint"], "2026-10-02", "100.01", category=h["food"])
    _tx(db, h["mine"], "2026-10-03", "20", category=h["food"])
    household = bud.budget_status(db, OWNER, year=2026, month=10)
    sams = bud.budget_status(db, OWNER, year=2026, month=10, person=h["sam"].id)
    for status in (household, sams):
        row = status["budgets"][0]
        assert (row["limit_pence"], row["spent_pence"]) == (30000, 12001)
    assert household["budgets"][0]["person_spent_pence"] == 12001
    assert sams["budgets"][0]["person_spent_pence"] == 5000
    assert sams["total_person_spent_pence"] == 5000
    alices = bud.budget_status(db, OWNER, year=2026, month=10, person=h["me"].id)
    assert alices["budgets"][0]["person_spent_pence"] == 5001 + 2000


def test_upcoming_per_person(db, home):
    h = home
    rec.create_recurring(db, OWNER, name="Rent", account_id=h["joint"].id, amount="1200.01", next_due="2026-11-01")
    rec.create_recurring(db, OWNER, name="Gym", account_id=h["mine"].id, amount="30", next_due="2026-11-02")
    names = lambda data: sorted((r["name"], r["share_pence"]) for r in data["items"])  # noqa: E731
    assert names(rec.upcoming(db, OWNER, today=TODAY)) == [("Gym", 3000), ("Rent", 120001)]
    assert names(rec.upcoming(db, OWNER, today=TODAY, person=h["sam"].id)) == [("Rent", 60000)]
    assert names(rec.upcoming(db, OWNER, today=TODAY, person=h["me"].id)) == [("Gym", 3000), ("Rent", 60001)]
    sams = rec.upcoming(db, OWNER, today=TODAY, person=h["sam"].id)
    assert [p["name"] for p in sams["projected_balances"]] == ["Joint"]
    assert rec.upcoming(db, OWNER, today=TODAY, person=h["alfie"].id)["items"] == []


def test_charts_per_person(db, home):
    h = home
    _tx(db, h["joint"], "2026-09-02", "1000", direction="in")
    _tx(db, h["mine"], "2026-10-03", "20", category=h["food"])
    fin.create_transfer(db, OWNER, from_account_id=h["mine"].id, to_account_id=h["kids"].id,
                        date="2026-10-10", amount="10")
    history = insights.net_worth_history(db, OWNER, range_key="6m", today=TODAY)
    last = history["points"][-1]
    assert last["net_worth_pence"] == 100000 - 2000
    assert sum(last["by_member"].values()) == last["net_worth_pence"]
    assert last["by_member"] == {h["me"].id: 50000 - 2000 - 1000, h["sam"].id: 50000, h["alfie"].id: 1000}
    sams = insights.net_worth_history(db, OWNER, range_key="6m", today=TODAY, person=h["sam"].id)
    assert [p["net_worth_pence"] for p in sams["points"][-2:]] == [50000, 50000]
    assert "by_member" not in sams["points"][-1]

    monthly = insights.monthly_totals(db, OWNER, range_key="6m", today=TODAY, person=h["me"].id)
    sep, oct_ = monthly["points"][-2:]
    assert (sep["in_pence"], oct_["out_pence"], oct_["moved_out_pence"]) == (50000, 2000, 1000)
    house = insights.monthly_totals(db, OWNER, range_key="6m", today=TODAY, category_id=h["food"].id)
    assert house["points"][-1]["category_out_pence"] == 2000 and "moved_in_pence" not in house["points"][-1]
    sams_food = insights.monthly_totals(db, OWNER, range_key="6m", today=TODAY, category_id=h["food"].id,
                                        person=h["sam"].id)
    assert sams_food["points"][-1]["category_out_pence"] == 0


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


def test_person_routes(client):
    me = _ok(client.get("/api/finance/members"))["members"][0]
    sam = _ok(client.post("/api/finance/members", json={"name": "Sam", "relationship": "partner"}))
    joint = _ok(client.post("/api/finance/accounts", json={"name": "Joint", "owner_ids": [me["id"], sam["id"]]}))
    _ok(client.post("/api/finance/transactions", json={"account_id": joint["id"], "date": "2026-10-02",
                                                       "amount": "10.01", "direction": "out"}))
    p = f"person={sam['id']}"
    assert [t["share_pence"] for t in _ok(client.get(f"/api/finance/transactions?year=2026&month=10&{p}"))[
        "transactions"]] == [500]
    assert _ok(client.get(f"/api/finance/summary?year=2026&month=10&{p}"))["total_out_pence"] == 500
    assert _ok(client.get("/api/finance/summary?year=2026&month=10"))["total_out_pence"] == 1001
    assert _ok(client.get(f"/api/finance/budgets?{p}"))["person"] == sam["id"]
    assert _ok(client.get(f"/api/finance/upcoming?{p}"))["person"] == sam["id"]
    assert _ok(client.get(f"/api/finance/insights/history?range=6m&{p}"))["person"] == sam["id"]
    assert _ok(client.get(f"/api/finance/insights/monthly?range=6m&{p}"))["points"][-1]["out_pence"] == 500
    assert _ok(client.get(f"/api/finance/insights/spending?start=2026-10-01&end=2026-10-31&{p}"))[
        "total_out_pence"] == 500
    assert client.get("/api/finance/summary?year=2026&month=10&person=nobody").status_code == 404
    assert _ok(client.get("/api/finance/summary?year=2026&month=10&person=household"))["person"] is None
