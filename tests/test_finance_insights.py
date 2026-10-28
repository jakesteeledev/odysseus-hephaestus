"""Chart data: net worth history and monthly totals."""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes
from src.finance import insights
from src.finance import networth as nw
from src.finance import service as fin
from src.finance.models import ensure_tables

OWNER = "alice"
TODAY = date(2026, 10, 15)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    for module in (fin, insights):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    session = sessionmaker(bind=engine)()
    yield session, engine
    session.close()
    engine.dispose()


def _seed(db):
    acc = fin.create_account(db, OWNER, name="Current", opening_balance="1000")
    savings = fin.create_account(db, OWNER, name="Savings")
    cats = {c["name"]: c["id"] for c in fin.list_categories(db, OWNER)}
    add = lambda day, amount, direction, cat=None: fin.create_transaction(  # noqa: E731
        db, OWNER, account_id=acc.id, date=day, amount=amount, direction=direction, category_id=cat)
    add("2026-08-01", "2000", "in", cats["Salary"])
    add("2026-08-10", "100", "out", cats["Groceries"])
    add("2026-09-01", "2000", "in", cats["Salary"])
    add("2026-09-12", "150", "out", cats["Groceries"])
    add("2026-09-20", "40", "out", cats["Eating out"])
    add("2026-10-05", "80", "out", cats["Groceries"])
    fin.create_transfer(db, OWNER, from_account_id=acc.id, to_account_id=savings.id, date="2026-09-25", amount="500")
    loan = nw.create_item(db, OWNER, "liability", name="Loan", liability_type="loan")
    nw.set_valuation(db, OWNER, "liability", loan.id, value="3000", on="2026-09-01")
    return cats


def test_net_worth_history_uses_month_ends(db):
    session, _ = db
    _seed(session)
    points = insights.net_worth_history(session, OWNER, range_key="6m")["points"]
    assert [p["month"] for p in points] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    by_month = {p["month"]: p for p in points}
    assert by_month["2026-07"]["net_worth_pence"] == 100000  # opening balance only
    assert by_month["2026-08"]["net_worth_pence"] == 100000 + 200000 - 10000
    sept = by_month["2026-09"]
    assert sept["liabilities_pence"] == 300000
    assert sept["assets_pence"] == 100000 + 400000 - 10000 - 15000 - 4000  # transfer moves money, no change
    assert sept["net_worth_pence"] == sept["assets_pence"] - 300000
    assert by_month["2026-10"]["as_of"] == "2026-10-15"  # this month is as of today


def test_all_range_starts_at_the_first_record(db):
    session, _ = db
    _seed(session)
    months = [p["month"] for p in insights.net_worth_history(session, OWNER, range_key="all")["points"]]
    assert months[-1] == "2026-10" and months[0] <= "2026-08"
    with pytest.raises(fin.FinanceError):
        insights.net_worth_history(session, OWNER, range_key="5y")


def test_monthly_totals_exclude_transfers_and_zero_fill(db):
    session, _ = db
    cats = _seed(session)
    points = insights.monthly_totals(session, OWNER, range_key="6m")["points"]
    by_month = {p["month"]: p for p in points}
    assert by_month["2026-06"] == {"month": "2026-06", "in_pence": 0, "out_pence": 0, "net_pence": 0}
    assert by_month["2026-09"]["out_pence"] == 15000 + 4000  # the £500 transfer is not spending
    assert by_month["2026-09"]["in_pence"] == 200000
    groceries = insights.monthly_totals(session, OWNER, range_key="6m", category_id=cats["Groceries"])["points"]
    assert [p["category_out_pence"] for p in groceries][-3:] == [10000, 15000, 8000]


def test_insight_routes(db, monkeypatch):
    session, engine = db
    _seed(session)
    session.commit()
    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as client:
        history = client.get("/api/finance/insights/history?range=6m").json()
        assert len(history["points"]) == 6
        monthly = client.get("/api/finance/insights/monthly?range=1y").json()
        assert len(monthly["points"]) == 12
        spending = client.get("/api/finance/insights/spending?start=2026-08-01&end=2026-09-30").json()
        assert spending["total_out_pence"] == 10000 + 15000 + 4000
        assert [c["name"] for c in spending["spend_by_category"]] == ["Groceries", "Eating out"]
        assert client.get("/api/finance/insights/spending?start=2026-09-30&end=2026-08-01").status_code == 400
        assert client.get("/api/finance/insights/history?range=forever").status_code == 400
