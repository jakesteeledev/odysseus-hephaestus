"""Budgets: limits by month, spent, committed, states and month boundaries."""

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.finance import budgets as bud
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance.models import FinanceBudget, ensure_tables

OWNER = "alice"
TODAY = date(2026, 10, 20)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    for module in (fin, rec, bud):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def setup(db):
    acc = fin.create_account(db, OWNER, name="Current", opening_balance="2000")
    savings = fin.create_account(db, OWNER, name="Savings")
    cats = {c["name"]: c["id"] for c in fin.list_categories(db, OWNER)}
    return acc, savings, cats


def _spend(db, acc, cat, amount, day, desc="x"):
    fin.create_transaction(db, OWNER, account_id=acc.id, date=day, amount=amount, direction="out",
                           category_id=cat, description=desc)


def test_spent_remaining_and_states(db, setup):
    acc, savings, cats = setup
    bud.set_budget(db, OWNER, cats["Groceries"], "300")
    bud.set_budget(db, OWNER, cats["Eating out"], "100")
    bud.set_budget(db, OWNER, cats["Shopping"], "200")
    _spend(db, acc, cats["Groceries"], "120.50", "2026-10-05")
    _spend(db, acc, cats["Eating out"], "85", "2026-10-07")
    _spend(db, acc, cats["Shopping"], "250", "2026-10-09")
    _spend(db, acc, cats["Groceries"], "999", "2026-09-30")  # last month
    _spend(db, acc, cats["Groceries"], "999", "2026-11-01")  # next month
    fin.create_transfer(db, OWNER, from_account_id=acc.id, to_account_id=savings.id, date="2026-10-10", amount="500")
    status = bud.budget_status(db, OWNER)
    rows = {r["name"]: r for r in status["budgets"]}
    assert rows["Groceries"]["spent_pence"] == 12050 and rows["Groceries"]["remaining_pence"] == 17950
    assert rows["Groceries"]["state"] == "ok" and rows["Groceries"]["percent_spent"] == 40
    assert rows["Eating out"]["state"] == "warning"  # 85% of the limit
    assert rows["Shopping"]["state"] == "over" and rows["Shopping"]["remaining_pence"] == -5000
    assert [r["name"] for r in status["budgets"]] == ["Shopping", "Eating out", "Groceries"]
    assert status["total_limit_pence"] == 60000 and status["total_spent_pence"] == 12050 + 8500 + 25000
    assert status["unbudgeted_spent_pence"] == 0  # transfers are not spending


def test_committed_recurring_items_reduce_what_is_left(db, setup):
    acc, _, cats = setup
    bud.set_budget(db, OWNER, cats["Subscriptions"], "50", month="2026-09")
    rec.create_recurring(db, OWNER, name="Netflix", account_id=acc.id, amount="10.99", next_due="2026-10-25",
                         category_id=cats["Subscriptions"])
    gym = rec.create_recurring(db, OWNER, name="Gym", account_id=acc.id, amount="30", next_due="2026-10-05",
                               category_id=cats["Subscriptions"])
    rec.confirm(db, OWNER, gym.id, "2026-10-05")  # now spent, no longer committed
    row = bud.budget_status(db, OWNER)["budgets"][0]
    assert row["spent_pence"] == 3000 and row["committed_pence"] == 1099
    assert row["left_after_committed_pence"] == 5000 - 3000 - 1099
    assert row["state"] == "warning"  # 41.99 of 50 is over 80%
    past = bud.budget_status(db, OWNER, year=2026, month=9)
    assert past["is_past"] and past["budgets"][0]["committed_pence"] == 0


def test_limits_change_from_a_month_and_past_months_keep_theirs(db, setup):
    _, _, cats = setup
    bud.set_budget(db, OWNER, cats["Groceries"], "300", month="2026-08")
    bud.set_budget(db, OWNER, cats["Groceries"], "350", month="2026-10")
    assert bud.budget_status(db, OWNER, year=2026, month=9)["budgets"][0]["limit_pence"] == 30000
    assert bud.budget_status(db, OWNER, year=2026, month=12)["budgets"][0]["limit_pence"] == 35000
    assert bud.budget_status(db, OWNER, year=2026, month=7)["budgets"] == []
    bud.set_budget(db, OWNER, cats["Groceries"], None, month="2026-11")  # removed from November
    assert bud.budget_status(db, OWNER, year=2026, month=11)["budgets"] == []
    assert bud.budget_status(db, OWNER, year=2026, month=10)["budgets"][0]["limit_pence"] == 35000
    bud.set_budget(db, OWNER, cats["Groceries"], "360", month="2026-10")  # same month replaces
    assert db.query(FinanceBudget).filter_by(month=date(2026, 10, 1)).count() == 1


def test_month_boundaries(db, setup):
    acc, _, cats = setup
    bud.set_budget(db, OWNER, cats["Groceries"], "100", month="2026-01")
    _spend(db, acc, cats["Groceries"], "10", "2026-02-28")
    _spend(db, acc, cats["Groceries"], "20", "2026-03-01")
    _spend(db, acc, cats["Groceries"], "40", "2026-03-31")
    assert bud.budget_status(db, OWNER, year=2026, month=2)["budgets"][0]["spent_pence"] == 1000
    assert bud.budget_status(db, OWNER, year=2026, month=3)["budgets"][0]["spent_pence"] == 6000


def test_unbudgeted_spending_and_validation(db, setup):
    acc, _, cats = setup
    bud.set_budget(db, OWNER, cats["Groceries"], "100")
    _spend(db, acc, cats["Health"], "45", "2026-10-03")
    assert bud.budget_status(db, OWNER)["unbudgeted_spent_pence"] == 4500
    with pytest.raises(fin.FinanceError):
        bud.set_budget(db, OWNER, cats["Salary"], "100")  # income category
    with pytest.raises(fin.FinanceError):
        bud.set_budget(db, OWNER, cats["Groceries"], "-5")
    with pytest.raises(fin.FinanceError):
        bud.parse_month("October")
    with pytest.raises(fin.FinanceNotFound):
        bud.set_budget(db, "bob", cats["Groceries"], "5")


def test_deleting_a_category_removes_its_budgets(db, setup):
    _, _, cats = setup
    bud.set_budget(db, OWNER, cats["Shopping"], "100")
    fin.delete_category(db, OWNER, cats["Shopping"])
    db.expire_all()
    assert db.query(FinanceBudget).count() == 0
