"""Finance tracker: money handling, balances, transfers and core rules."""

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.finance import service as fin
from src.finance.models import FinanceTransaction, ensure_tables
from src.finance.money import (
    MoneyError,
    format_pence,
    parse_pence,
    parse_positive_pence,
    pence_to_plain,
)

OWNER = "alice"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── Money ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,pence", [
    ("12.50", 1250), ("12.5", 1250), ("12", 1200), (".99", 99), ("0.005", 1),
    ("£1,234.56", 123456), ("-3.07", -307), ("(4.99)", -499), ("4.99-", -499),
    ("+7.10", 710), (" 2 ", 200), ("GBP 5.00", 500), (5, 500), (0.1 + 0.2, 30),
    (19.99, 1999), ("1 000.00", 100000),
])
def test_parse_pence(raw, pence):
    assert parse_pence(raw) == pence


@pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "£", "--5", None, True, "1e5"])
def test_parse_pence_rejects_garbage(raw):
    with pytest.raises(MoneyError):
        parse_pence(raw)


def test_positive_amount_rejects_zero_and_negative():
    with pytest.raises(MoneyError):
        parse_positive_pence("0")
    with pytest.raises(MoneyError):
        parse_positive_pence("-1")


def test_formatting():
    assert format_pence(123456) == "£1,234.56"
    assert format_pence(-1250) == "-£12.50"
    assert format_pence(5) == "£0.05"
    assert pence_to_plain(1250) == "12.50"
    assert pence_to_plain(-7) == "-0.07"
    assert pence_to_plain(100000) == "1000.00"


# ── Accounts and balances ───────────────────────────────────────────────


def _account(db, name="Current", **kw):
    return fin.create_account(db, OWNER, name=name, **kw)


def test_balance_is_opening_plus_transactions(db):
    acc = _account(db, opening_balance="100.00")
    fin.create_transaction(db, OWNER, account_id=acc.id, date="2026-10-01", amount="2500",
                           direction="in", description="Pay")
    fin.create_transaction(db, OWNER, account_id=acc.id, date="2026-10-02", amount="12.34",
                           direction="out", description="Shop")
    assert fin.account_balances(db, OWNER)[acc.id] == 10000 + 250000 - 1234


def test_amounts_are_stored_as_integer_pence(db):
    acc = _account(db)
    tx = fin.create_transaction(db, OWNER, account_id=acc.id, date="2026-10-01", amount="0.10",
                                direction="out")
    fin.create_transaction(db, OWNER, account_id=acc.id, date="2026-10-01", amount="0.20",
                           direction="out")
    db.commit()
    assert isinstance(tx.amount_pence, int) and tx.amount_pence == 10
    assert fin.account_balances(db, OWNER)[acc.id] == -30


def test_credit_card_balance_goes_negative_when_owed(db):
    card = _account(db, "Card", account_type="credit_card", opening_balance="-50")
    fin.create_transaction(db, OWNER, account_id=card.id, date="2026-10-03", amount="20",
                           direction="out")
    assert fin.account_balances(db, OWNER)[card.id] == -7000


def test_overview_lists_active_accounts_and_total(db):
    a = _account(db, "A", opening_balance="10")
    b = _account(db, "B", opening_balance="5.50")
    old = _account(db, "Old", opening_balance="999")
    fin.update_account(db, OWNER, old.id, archived=True)
    overview = fin.accounts_overview(db, OWNER)
    assert [x["name"] for x in overview["accounts"]] == ["A", "B"]
    assert overview["total_pence"] == 1550
    assert {x["id"] for x in fin.list_accounts(db, OWNER, include_archived=True)} == {a.id, b.id, old.id}


def test_only_empty_accounts_can_be_deleted(db):
    used = _account(db, "Used")
    empty = _account(db, "Empty")
    fin.create_transaction(db, OWNER, account_id=used.id, date="2026-10-01", amount="1",
                           direction="in")
    with pytest.raises(fin.FinanceConflict):
        fin.delete_account(db, OWNER, used.id)
    fin.update_account(db, OWNER, used.id, archived=True)  # archiving keeps history
    assert fin.list_transactions(db, OWNER, fin.TransactionFilter(account_id=used.id))
    fin.delete_account(db, OWNER, empty.id)
    assert empty.id not in fin.account_balances(db, OWNER)


def test_account_validation(db):
    with pytest.raises(fin.FinanceError):
        _account(db, "")
    with pytest.raises(fin.FinanceError):
        _account(db, "X", account_type="pension")
    assert _account(db, "Card", account_type="Credit Card").account_type == "credit_card"


# ── Owner scoping ───────────────────────────────────────────────────────


def test_owners_cannot_see_or_touch_each_others_data(db):
    mine = _account(db, "Mine")
    theirs = fin.create_account(db, "bob", name="Bob's")
    tx = fin.create_transaction(db, "bob", account_id=theirs.id, date="2026-10-01", amount="1",
                                direction="in")
    assert [a["name"] for a in fin.list_accounts(db, OWNER)] == ["Mine"]
    assert fin.list_transactions(db, OWNER) == []
    with pytest.raises(fin.FinanceNotFound):
        fin.create_transaction(db, OWNER, account_id=theirs.id, date="2026-10-01", amount="1",
                               direction="in")
    with pytest.raises(fin.FinanceNotFound):
        fin.delete_transaction(db, OWNER, tx.id)
    with pytest.raises(fin.FinanceNotFound):
        fin.create_transfer(db, OWNER, from_account_id=mine.id, to_account_id=theirs.id,
                            date="2026-10-01", amount="1")


def test_empty_owner_is_refused(db):
    with pytest.raises(fin.FinanceError):
        fin.list_accounts(db, "")


# ── Transfers ───────────────────────────────────────────────────────────


def test_transfer_moves_money_and_is_excluded_from_totals(db):
    current = _account(db, "Current", opening_balance="500")
    savings = _account(db, "Savings")
    out_side, in_side = fin.create_transfer(db, OWNER, from_account_id=current.id,
                                            to_account_id=savings.id, date="2026-10-05",
                                            amount="200")
    assert out_side.transfer_id == in_side.transfer_id
    assert out_side.category_id is None and in_side.category_id is None
    balances = fin.account_balances(db, OWNER)
    assert balances[current.id] == 30000 and balances[savings.id] == 20000
    assert fin.accounts_overview(db, OWNER)["total_pence"] == 50000
    s = fin.summary(db, OWNER, start=date(2026, 10, 1), end=date(2026, 11, 1))
    assert s["total_in_pence"] == 0 and s["total_out_pence"] == 0 and s["spend_by_category"] == []


def test_transfer_needs_two_different_accounts(db):
    acc = _account(db)
    with pytest.raises(fin.FinanceError):
        fin.create_transfer(db, OWNER, from_account_id=acc.id, to_account_id=acc.id,
                            date="2026-10-01", amount="1")


def test_editing_one_side_of_a_transfer_updates_both(db):
    a, b = _account(db, "A"), _account(db, "B")
    out_side, in_side = fin.create_transfer(db, OWNER, from_account_id=a.id, to_account_id=b.id,
                                            date="2026-10-05", amount="10")
    fin.update_transaction(db, OWNER, in_side.id, amount="25.50", date="2026-10-06",
                           description="Moved")
    db.refresh(out_side)
    db.refresh(in_side)
    for side in (out_side, in_side):
        assert side.amount_pence == 2550
        assert side.date == date(2026, 10, 6)
        assert side.description == "Moved"
    assert fin.account_balances(db, OWNER) == {a.id: -2550, b.id: 2550}
    with pytest.raises(fin.FinanceError):
        fin.update_transaction(db, OWNER, out_side.id, category_id="anything")


def test_deleting_one_side_of_a_transfer_deletes_both(db):
    a, b = _account(db, "A"), _account(db, "B")
    out_side, _ = fin.create_transfer(db, OWNER, from_account_id=a.id, to_account_id=b.id,
                                      date="2026-10-05", amount="10")
    assert fin.delete_transaction(db, OWNER, out_side.id) == 2
    assert db.query(FinanceTransaction).count() == 0
    assert fin.account_balances(db, OWNER) == {a.id: 0, b.id: 0}


def test_transfer_rows_report_the_other_account(db):
    a, b = _account(db, "A"), _account(db, "B")
    fin.create_transfer(db, OWNER, from_account_id=a.id, to_account_id=b.id, date="2026-10-05",
                        amount="10")
    rows = fin.list_transactions(db, OWNER, fin.TransactionFilter(account_id=a.id))
    assert rows[0]["direction"] == "out" and rows[0]["transfer_account_id"] == b.id
    assert rows[0]["description"] == "Transfer: A to B"


# ── Categories ──────────────────────────────────────────────────────────


def test_default_categories_are_seeded_once(db):
    names = [c["name"] for c in fin.list_categories(db, OWNER)]
    assert "Groceries" in names and names[-1] == fin.UNCATEGORISED
    groceries = next(c for c in fin.list_categories(db, OWNER) if c["name"] == "Groceries")
    fin.delete_category(db, OWNER, groceries["id"])
    names = [c["name"] for c in fin.list_categories(db, OWNER)]
    assert "Groceries" not in names  # deleting a default does not bring it back
    assert names.count(fin.UNCATEGORISED) == 1


def test_deleting_a_category_moves_transactions_to_uncategorised(db):
    acc = _account(db)
    cat = fin.create_category(db, OWNER, name="Coffee", kind="expense", color="orange")
    tx = fin.create_transaction(db, OWNER, account_id=acc.id, date="2026-10-01", amount="3",
                                direction="out", category_id=cat.id)
    assert fin.category_usage(db, OWNER, cat.id) == 1
    assert fin.delete_category(db, OWNER, cat.id) == 1
    db.refresh(tx)
    assert tx.category_id == fin.uncategorised(db, OWNER).id


def test_uncategorised_cannot_be_deleted_and_names_are_unique(db):
    with pytest.raises(fin.FinanceConflict):
        fin.delete_category(db, OWNER, fin.uncategorised(db, OWNER).id)
    with pytest.raises(fin.FinanceConflict):
        fin.create_category(db, OWNER, name="groceries")
    with pytest.raises(fin.FinanceError):
        fin.create_category(db, OWNER, name="Teal thing", color="#00ffff")


def test_transactions_default_to_uncategorised(db):
    acc = _account(db)
    tx = fin.create_transaction(db, OWNER, account_id=acc.id, date="2026-10-01", amount="1",
                                direction="out")
    assert tx.category_id == fin.uncategorised(db, OWNER).id


# ── Listing and summary ─────────────────────────────────────────────────


def _seed_month(db):
    acc = _account(db, "Current")
    other = _account(db, "Cash", account_type="cash")
    cats = {c["name"]: c["id"] for c in fin.list_categories(db, OWNER)}
    add = lambda **kw: fin.create_transaction(db, OWNER, **kw)  # noqa: E731
    add(account_id=acc.id, date="2026-10-01", amount="2000", direction="in",
        category_id=cats["Salary"], description="ACME payroll")
    add(account_id=acc.id, date="2026-10-03", amount="45.20", direction="out",
        category_id=cats["Groceries"], description="Tesco")
    add(account_id=acc.id, date="2026-10-20", amount="14.80", direction="out",
        category_id=cats["Groceries"], description="Aldi")
    add(account_id=other.id, date="2026-10-21", amount="9.99", direction="out",
        category_id=cats["Subscriptions"], description="Netflix")
    add(account_id=acc.id, date="2026-09-30", amount="100", direction="out",
        category_id=cats["Bills"], description="September bill")
    add(account_id=acc.id, date="2026-11-01", amount="100", direction="out",
        category_id=cats["Bills"], description="November bill")
    fin.create_transfer(db, OWNER, from_account_id=acc.id, to_account_id=other.id,
                        date="2026-10-10", amount="50")
    return acc, other, cats


def test_month_range_handles_december():
    assert fin.month_range(2026, 12) == (date(2026, 12, 1), date(2027, 1, 1))
    with pytest.raises(fin.FinanceError):
        fin.month_range(2026, 13)


def test_monthly_summary(db):
    _seed_month(db)
    start, end = fin.month_range(2026, 10)
    s = fin.summary(db, OWNER, start=start, end=end)
    assert s["total_in_pence"] == 200000
    assert s["total_out_pence"] == 4520 + 1480 + 999
    assert s["net_pence"] == 200000 - 6999
    assert [(c["name"], c["total_pence"], c["count"]) for c in s["spend_by_category"]] == [
        ("Groceries", 6000, 2), ("Subscriptions", 999, 1)]


def test_summary_for_one_account(db):
    acc, other, _ = _seed_month(db)
    start, end = fin.month_range(2026, 10)
    assert fin.summary(db, OWNER, start=start, end=end, account_id=other.id)["total_out_pence"] == 999


def test_list_filters(db):
    acc, other, cats = _seed_month(db)
    start, end = fin.month_range(2026, 10)
    month = fin.list_transactions(db, OWNER, fin.TransactionFilter(start=start, end=end))
    assert len(month) == 6  # 4 entries + both transfer sides
    assert month[0]["date"] == "2026-10-21"  # newest first
    out = fin.list_transactions(db, OWNER, fin.TransactionFilter(start=start, end=end,
                                                                 direction="out",
                                                                 include_transfers=False))
    assert {t["description"] for t in out} == {"Tesco", "Aldi", "Netflix"}
    groceries = fin.list_transactions(db, OWNER, fin.TransactionFilter(category_id=cats["Groceries"]))
    assert len(groceries) == 2
    assert [t["description"] for t in fin.list_transactions(
        db, OWNER, fin.TransactionFilter(search="TESCO"))] == ["Tesco"]
    assert len(fin.list_transactions(db, OWNER, fin.TransactionFilter(account_id=other.id))) == 2


def test_transaction_validation(db):
    acc = _account(db)
    base = dict(account_id=acc.id, date="2026-10-01", amount="1", direction="out")
    for bad in ({"amount": "0"}, {"amount": "-5"}, {"direction": "sideways"},
                {"date": "27/10/2026"}, {"description": "x" * 201}):
        with pytest.raises(fin.FinanceError):
            fin.create_transaction(db, OWNER, **{**base, **bad})
