"""Recurring items: due dates, matching, confirm/skip/undo, suggestions, projections."""

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.finance import loans
from src.finance import networth as nw
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance.models import FinanceRecurring, FinanceRecurringEvent, ensure_tables

OWNER = "alice"
TODAY = date(2026, 10, 27)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    for module in (fin, rec, loans):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def account(db):
    return fin.create_account(db, OWNER, name="Current", opening_balance="1000")


# ── Frequencies ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("frequency,expected", [
    ("weekly", [date(2026, 1, 31), date(2026, 2, 7), date(2026, 2, 14)]),
    ("four_weekly", [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 28)]),
    ("monthly", [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]),  # month-end holds
    ("quarterly", [date(2026, 1, 31), date(2026, 4, 30), date(2026, 7, 31)]),
    ("yearly", [date(2026, 1, 31), date(2027, 1, 31), date(2028, 1, 31)]),
])
def test_occurrences(frequency, expected):
    assert [rec.occurrence(date(2026, 1, 31), frequency, n) for n in range(3)] == expected


def test_leap_day_yearly_clamps():
    assert rec.occurrence(date(2028, 2, 29), "yearly", 1) == date(2029, 2, 28)
    assert rec.occurrence(date(2028, 2, 29), "yearly", 4) == date(2032, 2, 29)


def test_due_dates_window_and_end_date(db, account):
    item = rec.create_recurring(db, OWNER, name="Gym", account_id=account.id, amount="30", frequency="monthly",
                                next_due="2026-01-15", end_date="2026-05-15")
    assert rec.due_dates(item, date(2026, 3, 1), date(2026, 12, 31)) == [
        date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15)]
    far = rec.create_recurring(db, OWNER, name="Weekly", account_id=account.id, amount="5", frequency="weekly",
                               next_due="2020-01-01")
    days = rec.due_dates(far, date(2026, 10, 1), date(2026, 10, 31))
    assert len(days) in (4, 5) and all(d.weekday() == date(2020, 1, 1).weekday() for d in days)


def test_validation(db, account):
    with pytest.raises(fin.FinanceError):
        rec.create_recurring(db, OWNER, name="X", account_id=account.id, amount="1", frequency="daily")
    with pytest.raises(fin.FinanceError):
        rec.create_recurring(db, OWNER, name="X", account_id=account.id, amount="1", next_due="2026-05-01",
                             end_date="2026-04-01")
    with pytest.raises(fin.FinanceError):
        rec.create_recurring(db, OWNER, name="Pay", account_id=account.id, amount="1", direction="in",
                             liability_id="anything")
    assert rec.create_recurring(db, OWNER, name="X", account_id=account.id, amount="1",
                                frequency="every 4 weeks").frequency == "four_weekly"


# ── Upcoming, confirm, skip, undo ───────────────────────────────────────


def test_upcoming_and_projected_balance(db, account):
    rec.create_recurring(db, OWNER, name="Netflix", account_id=account.id, amount="10.99", next_due="2026-10-30")
    rec.create_recurring(db, OWNER, name="Salary", account_id=account.id, amount="2000", direction="in",
                         next_due="2026-10-28")
    rec.create_recurring(db, OWNER, name="Rent", account_id=account.id, amount="900", next_due="2026-11-01")
    up = rec.upcoming(db, OWNER)
    names = [(r["name"], r["due_date"], r["status"]) for r in up["items"]]
    assert names[:3] == [("Salary", "2026-10-28", "expected"), ("Netflix", "2026-10-30", "expected"),
                         ("Rent", "2026-11-01", "expected")]
    projected = up["projected_balances"][0]
    # Only items due by the end of October count towards the month-end projection.
    assert projected["expected_pence"] == 200000 - 1099
    assert projected["projected_pence"] == 100000 + 200000 - 1099


def test_overdue_confirm_skip_and_undo(db, account):
    item = rec.create_recurring(db, OWNER, name="Council tax", account_id=account.id, amount="150",
                                next_due="2026-09-01", category_id=None)
    up = rec.upcoming(db, OWNER)
    assert [(r["due_date"], r["status"]) for r in up["items"]][:2] == [("2026-09-01", "overdue"),
                                                                       ("2026-10-01", "overdue")]
    rec.skip(db, OWNER, item.id, "2026-09-01")
    event_ = rec.confirm(db, OWNER, item.id, "2026-10-01", on="2026-10-02", amount="151.20")
    tx = fin.list_transactions(db, OWNER)[0]
    assert tx["id"] == event_.transaction_id and tx["amount_pence"] == 15120 and tx["date"] == "2026-10-02"
    assert tx["description"] == "Council tax"
    up = rec.upcoming(db, OWNER)
    assert all(r["due_date"] >= "2026-10-27" for r in up["items"])  # resolved past ones drop off
    with pytest.raises(fin.FinanceConflict):
        rec.skip(db, OWNER, item.id, "2026-10-01")
    with pytest.raises(fin.FinanceError):
        rec.confirm(db, OWNER, item.id, "2026-10-15")  # not a due date
    rec.undo(db, OWNER, item.id, "2026-10-01")  # also removes the transaction confirm created
    assert fin.list_transactions(db, OWNER) == []
    rec.undo(db, OWNER, item.id, "2026-09-01")
    assert [r["status"] for r in rec.upcoming(db, OWNER)["items"]][:2] == ["overdue", "overdue"]


def test_confirm_links_debt_payments_and_contributions(db, account):
    car = nw.create_item(db, OWNER, "liability", name="Car", liability_type="loan")
    loans.set_schedule(db, OWNER, car.id, start_date="2026-01-01", start_balance="6000", term_months=12)
    isa = nw.create_item(db, OWNER, "asset", name="ISA", asset_type="investment")
    pay = rec.create_recurring(db, OWNER, name="Car finance", account_id=account.id, amount="500",
                               next_due="2026-10-01", liability_id=car.id)
    save = rec.create_recurring(db, OWNER, name="ISA saving", account_id=account.id, amount="200",
                                next_due="2026-10-05", asset_id=isa.id)
    rec.confirm(db, OWNER, pay.id, "2026-10-01")
    rec.confirm(db, OWNER, save.id, "2026-10-05")
    assert loans.loan_details(db, OWNER, car.id)["balance_pence"] == 550000
    assert nw.contributions_total(db, OWNER, isa.id) == 20000


# ── Matching ────────────────────────────────────────────────────────────


def test_matching_suggests_and_matches_recorded_transactions(db, account):
    item = rec.create_recurring(db, OWNER, name="Spotify", account_id=account.id, amount="11.99",
                                next_due="2026-10-24", match_text="SPOTIFY P1234")
    near = fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-25", amount="11.99",
                                  direction="out", description="Spotify UK p1234")
    fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-25", amount="11.99",
                           direction="in", description="Spotify refund")  # wrong direction
    fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-01", amount="11.99",
                           direction="out", description="Spotify")  # too early
    fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-24", amount="30",
                           direction="out", description="Spotify gift")  # amount too different
    row = rec.upcoming(db, OWNER)["items"][0]
    assert [m["id"] for m in row["matches"]] == [near.id]
    rec.match(db, OWNER, item.id, "2026-10-24", near.id)
    assert fin.list_transactions(db, OWNER, fin.TransactionFilter(search="spotify uk"))  # transaction kept
    with pytest.raises(fin.FinanceConflict):
        other = rec.create_recurring(db, OWNER, name="Spotify 2", account_id=account.id, amount="11.99",
                                     next_due="2026-10-24")
        rec.match(db, OWNER, other.id, "2026-10-24", near.id)  # already matched
    rec.undo(db, OWNER, item.id, "2026-10-24")  # matched transaction is kept
    assert fin.list_transactions(db, OWNER, fin.TransactionFilter(search="spotify uk"))


def test_amount_tolerance_and_exact_amount_without_words(db, account):
    item = rec.create_recurring(db, OWNER, name="Energy", account_id=account.id, amount="100",
                                next_due="2026-10-20")
    close = fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-21", amount="108",
                                   direction="out", description="Octopus energy")
    assert [m["id"] for m in rec.match_candidates(db, OWNER, item, date(2026, 10, 20))] == [close.id]
    fin.delete_transaction(db, OWNER, close.id)
    blind = fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-21", amount="100",
                                   direction="out", description="OCTOPUS")
    assert [m["id"] for m in rec.match_candidates(db, OWNER, item, date(2026, 10, 20))] == [blind.id]


def test_deleting_the_transaction_makes_the_occurrence_expected_again(db, account):
    item = rec.create_recurring(db, OWNER, name="Phone", account_id=account.id, amount="20", next_due="2026-10-10")
    tx = fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-10", amount="20", direction="out",
                                description="Phone")
    rec.match(db, OWNER, item.id, "2026-10-10", tx.id)
    fin.delete_transaction(db, OWNER, tx.id)
    db.expire_all()
    assert db.query(FinanceRecurringEvent).count() == 0


# ── Make recurring and suggestions ──────────────────────────────────────


def test_make_recurring_from_transaction(db, account):
    tx = fin.create_transaction(db, OWNER, account_id=account.id, date="2026-10-03", amount="45",
                                direction="out", description="Thames Water")
    item = rec.make_recurring_from_transaction(db, OWNER, tx.id, frequency="monthly")
    assert item.name == "Thames Water" and item.anchor_date == date(2026, 10, 3)
    listed = rec.list_recurring(db, OWNER)[0]
    assert listed["next_due"] == "2026-11-03"  # the October one is the transaction itself


def test_suggestions_from_history(db, account):
    for day in ("2026-05-28", "2026-06-28", "2026-07-28", "2026-08-28", "2026-09-28"):
        fin.create_transaction(db, OWNER, account_id=account.id, date=day, amount="2000", direction="in",
                               description="ACME LTD SALARY")
    for day in ("2026-09-06", "2026-09-13", "2026-09-20", "2026-09-27"):
        fin.create_transaction(db, OWNER, account_id=account.id, date=day, amount="4.50", direction="out",
                               description="Cafe Nero")
    for day in ("2026-08-02", "2026-09-19", "2026-10-01"):
        fin.create_transaction(db, OWNER, account_id=account.id, date=day, amount="60", direction="out",
                               description="Random shop")
    found = {s["name"]: s for s in rec.suggestions(db, OWNER)}
    assert set(found) == {"ACME LTD SALARY", "Cafe Nero"}
    assert found["ACME LTD SALARY"]["frequency"] == "monthly" and found["ACME LTD SALARY"]["next_due"] == "2026-10-28"
    assert found["Cafe Nero"]["frequency"] == "weekly"
    rec.create_recurring(db, OWNER, name="Salary", account_id=account.id, amount="2000", direction="in",
                         match_text="ACME SALARY")
    assert "ACME LTD SALARY" not in {s["name"] for s in rec.suggestions(db, OWNER)}


def test_owner_scoping(db, account):
    item = rec.create_recurring(db, OWNER, name="Gym", account_id=account.id, amount="30")
    with pytest.raises(fin.FinanceNotFound):
        rec.skip(db, "bob", item.id, TODAY)
    assert rec.upcoming(db, "bob")["items"] == []
    assert db.query(FinanceRecurring).count() == 1
