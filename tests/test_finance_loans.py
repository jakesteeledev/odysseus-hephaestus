"""Loan schedules, payment splits, snapshot overrides, projections and student loans."""

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.finance import loans
from src.finance import networth as nw
from src.finance import service as fin
from src.finance.models import FinanceRepayment, ensure_tables

OWNER = "alice"
TODAY = date(2026, 6, 20)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    monkeypatch.setattr(fin, "_today_for_owner", lambda: TODAY)
    monkeypatch.setattr(loans, "_today_for_owner", lambda: TODAY)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _loan(db, name="Car loan", type_="loan", **schedule):
    item = nw.create_item(db, OWNER, "liability", name=name, liability_type=type_)
    if schedule:
        loans.set_schedule(db, OWNER, item.id, **schedule)
    return item


def _interest(balance, apr_percent, days):
    return int(round(balance * apr_percent / 100 / 12 * days / loans.DAYS_PER_MONTH))


# ── Maths helpers ───────────────────────────────────────────────────────


def test_standard_payment():
    assert loans.standard_payment(1000000, 600, 12) == 86066  # £10,000 at 6% over a year
    assert loans.standard_payment(30000, 0, 3) == 10000  # 0% BNPL
    with_balloon = loans.standard_payment(2000000, 700, 36, balloon=800000)
    assert with_balloon < loans.standard_payment(2000000, 700, 36)


def test_dates():
    assert loans.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert loans.add_months(date(2026, 2, 28), 1, payment_day=31) == date(2026, 3, 31)
    assert loans.next_payment_date(date(2026, 1, 1), 1) == date(2026, 2, 1)
    assert loans.next_payment_date(date(2026, 1, 15), 28) == date(2026, 1, 28)
    assert loans.parse_rate_bp("4.5%") == 450 and loans.parse_rate_bp("") == 0
    with pytest.raises(fin.FinanceError):
        loans.parse_rate_bp("lots")


# ── Payments split into interest and capital ────────────────────────────


def test_payment_split_and_balance(db):
    item = _loan(db, start_date="2026-01-01", start_balance="10000", apr="6", term_months=12, payment_day=1)
    for day in ("2026-02-01", "2026-03-01"):
        loans.add_repayment(db, OWNER, item.id, amount="860.66", on=day)
    details = loans.loan_details(db, OWNER, item.id)
    first, second = reversed(details["payments"])
    i1 = _interest(1000000, 6, 31)
    assert first["interest_pence"] == i1 and first["capital_pence"] == 86066 - i1
    b1 = 1000000 + i1 - 86066
    assert first["balance_after_pence"] == b1
    i2 = _interest(b1, 6, 28)
    assert second["interest_pence"] == i2
    assert details["balance_pence"] == b1 + i2 - 86066
    assert details["interest_paid_pence"] == i1 + i2
    # Net worth uses the calculated balance.
    assert nw.net_worth(db, OWNER)["liabilities_total_pence"] == details["balance_pence"]


def test_snapshot_overrides_calculated_balance_from_its_date(db):
    item = _loan(db, start_date="2026-01-01", start_balance="10000", apr="6", term_months=12, payment_day=1)
    loans.add_repayment(db, OWNER, item.id, amount="860.66", on="2026-02-01")
    nw.set_valuation(db, OWNER, "liability", item.id, value="9000", on="2026-03-15", note="Statement")
    loans.add_repayment(db, OWNER, item.id, amount="860.66", on="2026-04-01")
    details = loans.loan_details(db, OWNER, item.id)
    expected = 900000 + _interest(900000, 6, 17) - 86066
    assert details["balance_pence"] == expected
    assert loans.balance(db, OWNER, item, as_of=date(2026, 3, 20))[0] == 900000


def test_zero_percent_bnpl_projection(db):
    item = _loan(db, name="Klarna sofa", type_="bnpl", start_date="2026-05-10", start_balance="300",
                 apr="0", term_months=3, payment_day=10)
    loans.add_repayment(db, OWNER, item.id, amount="100", on="2026-06-10")
    details = loans.loan_details(db, OWNER, item.id)
    p = details["projection"]
    assert details["balance_pence"] == 20000
    assert p["monthly_payment_pence"] == 10000 and p["interest_remaining_pence"] == 0
    assert p["remaining_payments"] == 2 and p["payoff_date"] == "2026-08-10"
    assert details["missed_payments"] == []


def test_pcp_balloon_final_payment(db):
    item = _loan(db, name="PCP", start_date="2026-01-01", start_balance="20000", apr="7",
                 term_months=36, payment_day=1, balloon="8000")
    p = loans.project(loans.get_schedule(db, OWNER, item.id), 2000000, date(2026, 1, 1))
    payment = p["monthly_payment_pence"]
    assert p["remaining_payments"] == 36 and p["payoff_date"] == "2029-01-01"
    assert abs(p["final_payment_pence"] - (800000 + payment)) <= 100  # last instalment + balloon


def test_projection_from_fixed_payment_and_interest_remaining(db):
    item = _loan(db, name="Mortgage", type_="mortgage", start_date="2026-01-01", start_balance="150000",
                 apr="4.5", monthly_payment="1000", payment_day=1)
    p = loans.loan_details(db, OWNER, item.id)["projection"]
    assert p["pays_off"] is True and p["monthly_payment_pence"] == 100000
    assert 200 < p["remaining_payments"] < 260 and p["interest_remaining_pence"] > 0
    item2 = _loan(db, name="Too small", start_date="2026-01-01", start_balance="100000", apr="20",
                  monthly_payment="10", payment_day=1)
    assert loans.loan_details(db, OWNER, item2.id)["projection"]["pays_off"] is False


def test_missed_payments_are_flagged(db):
    item = _loan(db, start_date="2026-01-01", start_balance="10000", apr="6", term_months=12, payment_day=1)
    for day in ("2026-02-01", "2026-03-03", "2026-05-01"):
        loans.add_repayment(db, OWNER, item.id, amount="860.66", on=day)
    assert loans.loan_details(db, OWNER, item.id)["missed_payments"] == ["2026-06-01", "2026-04-01"]


def test_schedule_validation(db):
    item = _loan(db)
    with pytest.raises(fin.FinanceError):
        loans.set_schedule(db, OWNER, item.id, start_date="2026-01-01", start_balance="100", balloon="50")
    with pytest.raises(fin.FinanceError):
        loans.set_schedule(db, OWNER, item.id, start_date="2026-01-01", start_balance="100", term_months=0)
    with pytest.raises(fin.FinanceError):
        loans.set_schedule(db, OWNER, item.id, start_date="2026-01-01", start_balance="100", term_months=12,
                           balloon="500")


# ── Student loans ───────────────────────────────────────────────────────


def test_student_loans_have_no_schedule_but_track_balance(db):
    slc = nw.create_item(db, OWNER, "liability", name="Student loan", liability_type="student_loan", subtype="plan 2")
    with pytest.raises(fin.FinanceError):
        loans.set_schedule(db, OWNER, slc.id, start_date="2026-01-01", start_balance="30000")
    loans.set_interest_rate(db, OWNER, slc.id, "7.3")
    nw.set_valuation(db, OWNER, "liability", slc.id, value="30000", on="2026-04-01", note="SLC statement")
    loans.add_repayment(db, OWNER, slc.id, amount="150", on="2026-05-01", note="Payroll")
    details = loans.loan_details(db, OWNER, slc.id)
    assert details["balance_pence"] == 3000000 + _interest(3000000, 7.3, 30) - 15000
    assert details["schedule"] is None and details["projection"] is None
    assert details["interest_rate_percent"] == 7.3
    assert nw.list_items(db, OWNER, "liability")[0]["interest_rate_percent"] == 7.3


# ── Linked payments from transactions ───────────────────────────────────


def test_linked_transaction_payment_follows_edits_and_deletes(db):
    current = fin.create_account(db, OWNER, name="Current", opening_balance="5000")
    item = _loan(db, start_date="2026-01-01", start_balance="10000", apr="0", term_months=10, payment_day=1)
    tx = fin.create_transaction(db, OWNER, account_id=current.id, date="2026-02-01", amount="1000",
                                direction="out", description="LOAN CO")
    loans.link_transaction_payment(db, OWNER, tx.id, item.id)
    assert loans.loan_details(db, OWNER, item.id)["balance_pence"] == 900000
    assert fin.list_transactions(db, OWNER)[0]["payment_liability_id"] == item.id
    fin.update_transaction(db, OWNER, tx.id, amount="1500")
    assert loans.loan_details(db, OWNER, item.id)["balance_pence"] == 850000
    with pytest.raises(fin.FinanceConflict):
        loans.delete_repayment(db, OWNER, loans.loan_details(db, OWNER, item.id)["payments"][0]["id"])
    fin.delete_transaction(db, OWNER, tx.id)
    db.expire_all()
    assert db.query(FinanceRepayment).count() == 0
    assert loans.loan_details(db, OWNER, item.id)["balance_pence"] == 1000000


def test_only_money_out_can_be_a_payment_and_history_blocks_delete(db):
    current = fin.create_account(db, OWNER, name="Current")
    item = _loan(db, start_date="2026-01-01", start_balance="100", term_months=1)
    income = fin.create_transaction(db, OWNER, account_id=current.id, date="2026-01-05", amount="5", direction="in")
    with pytest.raises(fin.FinanceError):
        loans.link_transaction_payment(db, OWNER, income.id, item.id)
    with pytest.raises(fin.FinanceConflict):
        nw.delete_item(db, OWNER, "liability", item.id)  # it has a schedule
    loans.delete_schedule(db, OWNER, item.id)
    nw.delete_item(db, OWNER, "liability", item.id)
