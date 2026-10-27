"""Finance agent tools: net worth, balances, summary, matching and clarification."""

import asyncio
import json
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.tools.finance as tools
from src.finance import service as fin
from src.finance.matching import match_name
from src.finance.models import ensure_tables

OWNER = "alice"
TODAY = date(2026, 10, 27)


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(tools, "_session", _session)
    monkeypatch.setattr(tools, "_today", lambda: TODAY)
    yield factory
    engine.dispose()


def run(fn, args, owner=OWNER):
    return asyncio.run(fn(json.dumps(args), owner=owner))


@pytest.fixture
def seeded(db_factory):
    db = db_factory()
    current = fin.create_account(db, OWNER, name="Monzo Current", opening_balance="1000")
    savings = fin.create_account(db, OWNER, name="Marcus Savings", account_type="savings", opening_balance="5000")
    card = fin.create_account(db, OWNER, name="Amex", account_type="credit_card", opening_balance="-250")
    old = fin.create_account(db, OWNER, name="Old Cash", account_type="cash", opening_balance="40")
    fin.update_account(db, OWNER, old.id, archived=True)
    cats = {c["name"]: c["id"] for c in fin.list_categories(db, OWNER)}
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-10-01", amount="2500", direction="in",
                           category_id=cats["Salary"], description="ACME payroll")
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-10-03", amount="45.20", direction="out",
                           category_id=cats["Groceries"], description="Tesco")
    fin.create_transaction(db, OWNER, account_id=card.id, date="2026-10-12", amount="9.99", direction="out",
                           category_id=cats["Subscriptions"], description="Netflix")
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-09-20", amount="80", direction="out",
                           category_id=cats["Bills"], description="September energy")
    fin.create_transfer(db, OWNER, from_account_id=current.id, to_account_id=savings.id, date="2026-10-05",
                        amount="300")
    ids = {"current": current.id, "savings": savings.id, "card": card.id, "cats": cats}
    db.commit()
    db.close()
    return ids


# ── Name matching ───────────────────────────────────────────────────────


NAMES = [{"name": n} for n in ("Monzo Current", "Barclays Current", "Marcus Savings", "Amex", "Rent/Mortgage")]


@pytest.mark.parametrize("query,expected", [
    ("amex", "Amex"), ("AMEX card", "Amex"), ("monzo", "Monzo Current"), ("my savings account", "Marcus Savings"),
    ("savings", "Marcus Savings"), ("rent", "Rent/Mortgage"), ("Monzo Current", "Monzo Current"),
])
def test_match_name_finds_one(query, expected):
    match = match_name(query, NAMES)
    assert match.ok and match.item["name"] == expected


def test_match_name_ambiguous_and_missing():
    ambiguous = match_name("current", NAMES)
    assert ambiguous.status == "ambiguous" and set(ambiguous.options) == {"Monzo Current", "Barclays Current"}
    missing = match_name("pension", NAMES)
    assert missing.status == "missing" and len(missing.options) == len(NAMES)
    assert match_name("", NAMES).status == "missing"


def test_exact_name_beats_partial_matches():
    items = [{"name": "Bills"}, {"name": "Bills (shared)"}]
    assert match_name("bills", items).item["name"] == "Bills"


# ── query_finances ──────────────────────────────────────────────────────


def test_net_worth_is_assets_minus_liabilities(seeded):
    result = run(tools.do_query_finances, {"view": "net_worth"})
    assert result["exit_code"] == 0
    # current 1000+2500-45.20-80-300, savings 5000+300, card -250-9.99 (a liability),
    # plus the archived cash account, which still holds £40 (archived counts until zero).
    expected = (100000 + 250000 - 4520 - 8000 - 30000) + (500000 + 30000) + 4000 - (25000 + 999)
    assert result["data"]["net_worth_pence"] == expected
    assert "Net worth: £8,154.81" in result["response"]
    assert "Credit cards: £259.99" in result["response"] and "Old Cash: £40.00 (archived)" in result["response"]


def test_overview_with_no_arguments_answers_net_worth_and_month(seeded):
    result = asyncio.run(tools.do_query_finances("", owner=OWNER))
    assert result["exit_code"] == 0
    text = result["response"]
    assert "Net worth" in text and "Monzo Current: £3,074.80" in text
    assert "Period 2026-10-01 to 2026-10-31" in text
    assert "Money in: £2,500.00" in text and "Money out: £55.19" in text


def test_balances_view(seeded):
    result = run(tools.do_query_finances, {"view": "balances"})
    balances = {a["name"]: a["balance"] for a in result["data"]["accounts"]}
    assert balances == {"Monzo Current": "£3,074.80", "Marcus Savings": "£5,300.00", "Amex": "-£259.99"}


def test_summary_excludes_transfers_and_respects_range(seeded):
    result = run(tools.do_query_finances, {"view": "summary"})
    s = result["data"]["summary"]
    assert s["total_in_pence"] == 250000 and s["total_out_pence"] == 4520 + 999
    assert [c["name"] for c in s["spend_by_category"]] == ["Groceries", "Subscriptions"]
    assert "Transfers between the user's own accounts are not counted" in result["response"]
    september = run(tools.do_query_finances, {"view": "summary", "start": "2026-09-01", "end": "2026-09-30"})
    assert september["data"]["summary"]["total_out_pence"] == 8000


def test_summary_for_one_account_by_fuzzy_name(seeded):
    result = run(tools.do_query_finances, {"view": "summary", "account": "amex"})
    assert result["data"]["summary"]["total_out_pence"] == 999
    assert "(Amex)" in result["response"]


def test_transactions_with_filters(seeded):
    recent = run(tools.do_query_finances, {"view": "transactions", "limit": 3})
    assert [t["description"] for t in recent["data"]["transactions"]] == ["Netflix", "Transfer: Monzo Current to Marcus Savings", "Transfer: Monzo Current to Marcus Savings"]
    groceries = run(tools.do_query_finances, {"view": "transactions", "category": "groceries"})
    assert [t["description"] for t in groceries["data"]["transactions"]] == ["Tesco"]
    assert "-£45.20 Tesco [Monzo Current, Groceries]" in groceries["response"]
    found = run(tools.do_query_finances, {"view": "transactions", "search": "payroll"})
    assert len(found["data"]["transactions"]) == 1


def test_query_with_ambiguous_account_lists_options(seeded, db_factory):
    db = db_factory()
    fin.create_account(db, OWNER, name="Barclays Current")
    db.commit()
    db.close()
    result = run(tools.do_query_finances, {"view": "summary", "account": "current"})
    assert result["exit_code"] == 1
    assert "matches more than one account" in result["error"]
    assert "Monzo Current" in result["error"] and "Barclays Current" in result["error"]


def test_query_with_no_accounts(db_factory):
    result = run(tools.do_query_finances, {"view": "net_worth"})
    assert result["exit_code"] == 0 and "no finance accounts" in result["response"]


def test_bad_view_is_rejected(seeded):
    assert run(tools.do_query_finances, {"view": "horoscope"})["exit_code"] == 1


def test_users_only_see_their_own_finances(seeded):
    result = run(tools.do_query_finances, {"view": "net_worth"}, owner="bob")
    assert "no finance accounts" in result["response"]


def test_internal_callers_are_refused(seeded):
    assert run(tools.do_query_finances, {}, owner="internal-tool")["exit_code"] == 1


# ── add_transaction ─────────────────────────────────────────────────────


def test_add_transaction_with_fuzzy_names_defaults_to_today(seeded, db_factory):
    result = run(tools.do_add_transaction, {"amount": "12.50", "direction": "out", "account": "monzo",
                                            "category": "eating out", "description": "Lunch"})
    assert result["exit_code"] == 0, result
    assert result["response"].startswith("Money out recorded: £12.50 on 2026-10-27, Monzo Current, Eating out")
    assert "balance is now £3,062.30" in result["response"]
    db = db_factory()
    tx = fin.list_transactions(db, OWNER, fin.TransactionFilter(search="Lunch"))[0]
    assert tx["amount_pence"] == 1250 and tx["date"] == "2026-10-27"
    assert tx["category_id"] == seeded["cats"]["Eating out"]
    db.close()


def test_add_transaction_accepts_negative_amount_and_aliases(seeded):
    result = run(tools.do_add_transaction, {"amount": "-3.10", "type": "expense", "account": "amex",
                                            "date": "yesterday"})
    assert result["exit_code"] == 0
    assert "£3.10 on 2026-10-26, Amex, Uncategorised" in result["response"]


def test_add_transaction_ambiguous_account_asks_and_saves_nothing(seeded, db_factory):
    db = db_factory()
    fin.create_account(db, OWNER, name="Barclays Current")
    db.commit()
    before = len(fin.list_transactions(db, OWNER))
    db.close()
    result = run(tools.do_add_transaction, {"amount": "5", "direction": "out", "account": "current"})
    assert result["exit_code"] == 1
    assert result["error"].startswith("Nothing saved.") and "Ask the user" in result["error"]
    db = db_factory()
    assert len(fin.list_transactions(db, OWNER)) == before
    db.close()


def test_add_transaction_unknown_category_never_creates_one(seeded, db_factory):
    result = run(tools.do_add_transaction, {"amount": "5", "account": "monzo", "category": "Pet food"})
    assert result["exit_code"] == 1
    assert "No category called 'Pet food'" in result["error"] and "Do not create" in result["error"]
    db = db_factory()
    assert "Pet food" not in {c["name"] for c in fin.list_categories(db, OWNER)}
    db.close()


def test_add_transaction_without_account_uses_the_only_one_or_asks(db_factory):
    db = db_factory()
    fin.create_account(db, OWNER, name="Only")
    db.commit()
    db.close()
    assert run(tools.do_add_transaction, {"amount": "1"})["exit_code"] == 0
    db = db_factory()
    fin.create_account(db, OWNER, name="Second")
    db.commit()
    db.close()
    asked = run(tools.do_add_transaction, {"amount": "1"})
    assert asked["exit_code"] == 1 and "Which account?" in asked["error"]


def test_add_transaction_validation(seeded):
    for bad in ({"amount": "", "account": "monzo"}, {"amount": "abc", "account": "monzo"},
                {"amount": "1", "account": "monzo", "direction": "sideways"},
                {"amount": "1", "account": "monzo", "date": "next blue moon"}):
        result = run(tools.do_add_transaction, bad)
        assert result["exit_code"] == 1 and result["error"].startswith("Nothing saved"), bad


def test_add_transaction_refuses_archived_account_names(seeded):
    result = run(tools.do_add_transaction, {"amount": "1", "account": "old cash"})
    assert result["exit_code"] == 1 and "No account called" in result["error"]


# ── add_transfer ────────────────────────────────────────────────────────


def test_add_transfer(seeded):
    result = run(tools.do_add_transfer, {"from_account": "monzo", "to_account": "savings", "amount": 100})
    assert result["exit_code"] == 0, result
    assert "£100.00 from Monzo Current to Marcus Savings on 2026-10-27" in result["response"]
    assert "Monzo Current £2,974.80" in result["response"] and "Marcus Savings £5,400.00" in result["response"]
    summary = run(tools.do_query_finances, {"view": "summary"})
    assert summary["data"]["summary"]["total_out_pence"] == 4520 + 999  # unchanged by the transfer


def test_add_transfer_needs_two_distinct_known_accounts(seeded):
    assert "needs from_account and to_account" in run(tools.do_add_transfer, {"amount": 5, "to_account": "savings"})["error"]
    same = run(tools.do_add_transfer, {"from_account": "monzo", "to_account": "monzo current", "amount": 5})
    assert same["exit_code"] == 1 and "two different accounts" in same["error"]
    unknown = run(tools.do_add_transfer, {"from_account": "monzo", "to_account": "isa", "amount": 5})
    assert unknown["exit_code"] == 1 and "No account called 'isa'" in unknown["error"]


# ── v2: assets, liabilities and update_valuation ───────────────────────


@pytest.fixture
def items(seeded, db_factory):
    from src.finance import networth as nw

    db = db_factory()
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property", value="300000",
                           value_date="2026-01-01")
    nw.create_item(db, OWNER, "asset", name="Vanguard ISA", asset_type="investment", value="12000",
                   value_date="2026-09-30")
    nw.create_item(db, OWNER, "asset", name="Aviva pension", asset_type="pension", value="45000")
    nw.create_item(db, OWNER, "liability", name="Home mortgage", liability_type="mortgage", value="180000",
                   linked_asset_id=house.id, value_date="2026-01-01")
    nw.create_item(db, OWNER, "liability", name="Car finance", liability_type="loan", subtype="pcp",
                   value="8000", value_date="2026-09-01")
    db.commit()
    db.close()


def test_net_worth_breakdown_includes_assets_and_liabilities(items):
    result = run(tools.do_query_finances, {"view": "net_worth"})
    text = result["response"]
    worth = result["data"]["net_worth"]
    assert worth["assets_total_pence"] == 841480 + 30000000 + 1200000 + 4500000
    assert worth["liabilities_total_pence"] == 25999 + 18000000 + 800000
    assert "- Property: £300,000.00" in text and "equity £120,000.00" in text
    assert "- Mortgages: £180,000.00" in text and "Car finance: £8,000.00 (as of 2026-09-01)" in text


def test_assets_and_liabilities_views(items):
    assets = run(tools.do_query_finances, {"view": "assets"})
    assert {i["name"] for i in assets["data"]["assets"]} == {"Home", "Vanguard ISA", "Aviva pension"}
    debts = run(tools.do_query_finances, {"view": "debts"})
    assert debts["data"]["view"] == "liabilities"
    assert "Liabilities (owed): £188,000.00" in debts["response"]
    assert "Credit cards (owed): Amex £259.99" in debts["response"]


def test_update_valuation_by_fuzzy_name(items):
    result = run(tools.do_update_valuation, {"name": "isa", "value": "12,500"})
    assert result["exit_code"] == 0, result
    assert result["response"].startswith("Updated Vanguard ISA value to £12,500.00 on 2026-10-27 "
                                         "(was £12,000.00 on 2026-09-30)")
    debt = run(tools.do_update_valuation, {"name": "car", "value": "7650", "note": "statement"})
    assert debt["exit_code"] == 0 and "Car finance balance owed to £7,650.00" in debt["response"]


def test_update_valuation_ambiguous_and_unknown(items, db_factory):
    from src.finance import networth as nw

    exact = run(tools.do_update_valuation, {"name": "home", "value": "1"})
    # "Home" matches the asset exactly, so it wins over "Home mortgage".
    assert exact["exit_code"] == 0 and "Updated Home value" in exact["response"]
    db = db_factory()
    nw.create_item(db, OWNER, "asset", name="Vanguard SIPP", asset_type="pension")
    db.commit()
    db.close()
    both = run(tools.do_update_valuation, {"name": "vanguard", "value": "1"})
    assert both["exit_code"] == 1 and "matches more than one item" in both["error"]
    assert "Vanguard ISA (asset)" in both["error"] and "Vanguard SIPP (asset)" in both["error"]
    missing = run(tools.do_update_valuation, {"name": "boat", "value": "1"})
    assert missing["exit_code"] == 1 and "No asset or liability called 'boat'" in missing["error"]
    assert "Do not create one" in missing["error"]


def test_update_valuation_refuses_accounts_and_needs_a_value(items):
    account = run(tools.do_update_valuation, {"name": "Monzo Current", "value": "5"})
    assert account["exit_code"] == 1 and "is an account" in account["error"]
    assert "What is the new value" in run(tools.do_update_valuation, {"name": "isa"})["error"]
    assert run(tools.do_update_valuation, {"name": "isa", "value": "abc"})["exit_code"] == 1


def test_update_valuation_with_no_items(db_factory):
    result = run(tools.do_update_valuation, {"name": "pension", "value": "5"})
    assert result["exit_code"] == 1 and "no assets or liabilities yet" in result["error"]


# ── v2 stage 2: loans in tools ─────────────────────────────────────────


@pytest.fixture
def car_loan(seeded, db_factory):
    from src.finance import loans
    from src.finance import networth as nw

    db = db_factory()
    car = nw.create_item(db, OWNER, "liability", name="Car finance", liability_type="loan", subtype="pcp")
    loans.set_schedule(db, OWNER, car.id, start_date="2026-01-01", start_balance="12000", apr="0",
                       term_months=24, payment_day=1)
    loans.add_repayment(db, OWNER, car.id, amount="500", on="2026-02-01")
    slc = nw.create_item(db, OWNER, "liability", name="Student loan", liability_type="student_loan")
    nw.set_valuation(db, OWNER, "liability", slc.id, value="28000", on="2026-04-01")
    ids = {"car": car.id, "slc": slc.id}
    db.commit()
    db.close()
    return ids


def test_liabilities_view_has_loan_details(car_loan, monkeypatch):
    from src.finance import loans

    monkeypatch.setattr(loans, "_today_for_owner", lambda: TODAY)
    result = run(tools.do_query_finances, {"view": "liabilities", "name": "car"})
    assert result["exit_code"] == 0
    text = result["response"]
    assert "Car finance: £11,500.00" in text and "Student loan" not in text
    assert "Next payment £500.00" in text and "interest still to pay £0.00" in text
    assert "Missed expected payment(s): 2026-10-01, 2026-09-01" in text
    everything = run(tools.do_query_finances, {"view": "liabilities"})
    assert "Student loan: £28,000.00" in everything["response"]


def test_add_transaction_towards_a_debt(car_loan, db_factory):
    from src.finance import loans
    from src.finance.models import FinanceLiability

    result = run(tools.do_add_transaction, {"amount": "500", "account": "monzo", "towards": "car",
                                            "description": "Car finance"})
    assert result["exit_code"] == 0, result
    assert "Counted as a payment towards Car finance; still owed £11,000.00" in result["response"]
    db = db_factory()
    car = db.query(FinanceLiability).filter_by(id=car_loan["car"]).one()
    assert loans.balance(db, OWNER, car)[0] == 1100000
    db.close()


def test_add_transaction_towards_unknown_or_income_saves_nothing(car_loan, db_factory):
    unknown = run(tools.do_add_transaction, {"amount": "5", "account": "monzo", "towards": "mortgage"})
    assert unknown["exit_code"] == 1 and "No debt called 'mortgage'" in unknown["error"]
    income = run(tools.do_add_transaction, {"amount": "5", "account": "monzo", "direction": "in", "towards": "car"})
    assert income["exit_code"] == 1 and "Only money going out" in income["error"]
    db = db_factory()
    assert not fin.list_transactions(db, OWNER, fin.TransactionFilter(start=TODAY))
    db.close()


# ── v2 stage 3: recurring items in tools ───────────────────────────────


@pytest.fixture
def bills(seeded, db_factory, monkeypatch):
    from src.finance import recurring as rec

    monkeypatch.setattr(rec, "_today_for_owner", lambda: TODAY)
    monkeypatch.setattr(fin, "_today_for_owner", lambda: TODAY)
    db = db_factory()
    netflix = rec.create_recurring(db, OWNER, name="Netflix", account_id=seeded["current"], amount="10.99",
                                   next_due="2026-10-29")
    rec.create_recurring(db, OWNER, name="Rent", account_id=seeded["current"], amount="900", next_due="2026-11-01")
    rec.create_recurring(db, OWNER, name="Council tax", account_id=seeded["current"], amount="150",
                         next_due="2026-10-20")
    ids = {"netflix": netflix.id}
    db.commit()
    db.close()
    return ids


def test_upcoming_view(bills):
    week = run(tools.do_query_finances, {"view": "upcoming", "days": 7})
    text = week["response"]
    assert "- 2026-10-20 Council tax -£150.00 [Monzo Current, monthly] OVERDUE" in text
    assert "- 2026-10-29 Netflix -£10.99" in text and "- 2026-11-01 Rent -£900.00" in text
    assert "Total going out: £1,060.99" in text
    # Month-end projection only counts items due by 31 October.
    assert "Monzo Current: £2,913.81 (now £3,074.80)" in text
    assert run(tools.do_query_finances, {"view": "bills"})["data"]["view"] == "upcoming"


def test_manage_recurring_add_list_skip(bills):
    added = run(tools.do_manage_recurring, {"action": "add", "name": "Gym", "amount": "35", "account": "monzo",
                                            "frequency": "monthly", "next_due": "2026-11-05", "category": "health"})
    assert added["exit_code"] == 0 and "Gym: -£35.00 monthly from Monzo Current, next due 2026-11-05" in added["response"]
    listed = run(tools.do_manage_recurring, {"action": "list"})
    assert "- Gym: -£35.00 monthly" in listed["response"]
    skipped = run(tools.do_manage_recurring, {"action": "skip", "name": "council"})
    assert skipped["exit_code"] == 0 and "Skipped Council tax due 2026-10-20" in skipped["response"]


def test_manage_recurring_confirm_creates_or_matches(bills, db_factory):
    created = run(tools.do_manage_recurring, {"action": "confirm", "name": "council tax"})
    assert created["exit_code"] == 0 and "recorded £150.00 on Monzo Current" in created["response"]
    from src.finance.models import FinanceAccount

    db = db_factory()
    current = db.query(FinanceAccount).filter_by(name="Monzo Current").one()
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-10-29", amount="10.99",
                           direction="out", description="NETFLIX.COM")
    db.commit()
    db.close()
    matched = run(tools.do_manage_recurring, {"action": "confirm", "name": "netflix"})
    assert matched["exit_code"] == 0 and "by matching the transaction already recorded on 2026-10-29" in matched["response"]
    assert "No new transaction was added" in matched["response"]


def test_manage_recurring_asks_when_unclear(bills):
    missing = run(tools.do_manage_recurring, {"action": "confirm", "name": "spotify"})
    assert missing["exit_code"] == 1 and "doesn't match any recurring item" in missing["error"]
    noname = run(tools.do_manage_recurring, {"action": "skip"})
    assert noname["exit_code"] == 1 and "Which recurring item?" in noname["error"]
    incomplete = run(tools.do_manage_recurring, {"action": "add", "name": "Gym"})
    assert incomplete["exit_code"] == 1 and "needs a name and an amount" in incomplete["error"]
    assert run(tools.do_manage_recurring, {"action": "explode"})["exit_code"] == 1
