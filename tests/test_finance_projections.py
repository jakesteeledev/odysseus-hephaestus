"""Projection engine: loans, balloons, growth, recurring items, shortfall,
reconciliation, determinism, scenarios and stored assumptions.

All figures are made up.
"""

import json
import random
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.finance import loans
from src.finance import networth as nw
from src.finance import projections as pj
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance.models import ensure_tables

OWNER = "alice"
TODAY = date(2026, 6, 20)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    for module in (fin, loans, pj, rec):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _inputs(cash=10_000_000, **kw):
    return pj.Inputs(today=TODAY, accounts=[pj.Account("cur", "Current", "current", cash)], **kw)


def _flow_sum(result, key):
    return sum(p["flows"][key] for p in result["points"])


# ── Loans ───────────────────────────────────────────────────────────────


def test_mortgage_matches_loan_payoff_to_the_penny(db):
    acc = fin.create_account(db, OWNER, name="Current", account_type="current", opening_balance="500000")
    house = nw.create_item(db, OWNER, "asset", name="House", asset_type="property", value="147500", value_date="2026-01-01")
    mortgage = nw.create_item(db, OWNER, "liability", name="Mortgage", liability_type="mortgage",
                              linked_asset_id=house.id)
    loans.set_schedule(db, OWNER, mortgage.id, start_date="2026-01-15", start_balance="122000", apr="5.4",
                       term_months=300, payment_day=15)
    schedule = loans.get_schedule(db, OWNER, mortgage.id)
    balance, _ = loans.balance(db, OWNER, mortgage, TODAY)
    expected = loans.project(schedule, balance, TODAY)

    result = pj.projection(db, OWNER, years=30, today=TODAY)["base"]
    paid_off = [e for e in result["events"] if e["kind"] == "paid_off" and e["item_id"] == mortgage.id]
    assert paid_off[0]["date"] == expected["payoff_date"]
    assert _flow_sum(result, "loan_interest") == expected["interest_remaining_pence"]
    assert acc.id  # cash pays the mortgage
    # Payments stop after payoff.
    after = [p for p in result["points"] if p["date"] > expected["payoff_date"]][1:]
    assert after and all(p["flows"]["loan_payments"] == 0 for p in after)


def test_pcp_balloon_is_paid_on_its_date_and_net_worth_is_unchanged():
    pcp = pj.Liability("pcp", "Car PCP", "loan", 2_000_000, subtype="pcp", schedule=pj.LoanTerms(
        apr_bp=0, payment=loans.standard_payment(2_000_000, 0, 36, 800_000), payment_day=1,
        term_end=date(2029, 6, 1), balloon=800_000, start_date=date(2026, 6, 1)))
    result = pj.run(_inputs(liabilities=[pcp]), months=48)
    balloon = [e for e in result["events"] if e["kind"] == "balloon"]
    assert balloon and balloon[0]["date"] == "2029-06-01"
    assert balloon[0]["amount_pence"] > 800_000  # the last instalment plus the balloon
    month = next(i for i, p in enumerate(result["points"]) if p["date"] >= "2029-06-01")
    assert result["points"][month]["flows"]["loan_payments"] == balloon[0]["amount_pence"]
    # Paying a debt from cash doesn't change net worth.
    assert result["points"][month]["net_worth_pence"] == result["points"][month - 1]["net_worth_pence"]
    assert all(p["flows"]["loan_payments"] == 0 for p in result["points"][month + 1:])
    assert result["end"]["liabilities_pence"] == 0


def test_zero_percent_loan_charges_no_interest():
    sofa = pj.Liability("dfs", "Sofa", "bnpl", 240_000, schedule=pj.LoanTerms(
        apr_bp=0, payment=10_000, payment_day=5, term_end=date(2028, 6, 5), start_date=date(2026, 6, 5)))
    result = pj.run(_inputs(liabilities=[sofa]), months=36)
    assert _flow_sum(result, "loan_interest") == 0
    assert _flow_sum(result, "interest_charged") == 0
    assert _flow_sum(result, "loan_payments") == 240_000
    assert any(e["kind"] == "paid_off" for e in result["events"])


def test_payoff_event_reports_cash_flow_change():
    loan = pj.Liability("l", "Car loan", "loan", 30_000, schedule=pj.LoanTerms(
        apr_bp=0, payment=10_000, payment_day=1, start_date=date(2026, 1, 1)))
    salary = pj.Regular("s", "Salary", "cur", 200_000, "in", "monthly", date(2026, 6, 25))
    result = pj.run(_inputs(liabilities=[loan], regulars=[salary]), months=12)
    paid = next(e for e in result["events"] if e["kind"] == "paid_off")
    assert paid["monthly_change_pence"] == 10_000
    assert paid["cash_flow_after_pence"] - paid["cash_flow_before_pence"] == 10_000
    assert "improves by £100.00" in paid["followup"]


# ── Assets ──────────────────────────────────────────────────────────────


def test_property_grows_by_exactly_the_annual_rate():
    house = pj.Asset("h", "House", "property", 14_750_000)
    result = pj.run(_inputs(cash=0, assets=[house]), months=12)
    value = result["years"][0]["assets"][0]["value_pence"]
    assert abs(value - 14_971_250) <= 1  # £149,712.50


def test_vehicle_depreciates_and_pension_adds_contributions():
    car = pj.Asset("car", "Car", "vehicle", 1_000_000)
    pension = pj.Asset("p", "Pension", "pension", 0)
    contrib = pj.Regular("c", "SIPP", "cur", 10_000, "out", "monthly", date(2026, 7, 1), asset_id="p")
    a = pj.Assumptions()
    a.growth_bp["pension"] = 0
    result = pj.run(_inputs(assets=[car, pension], regulars=[contrib]), a, months=12)
    year = {x["id"]: x for x in result["years"][0]["assets"]}
    assert abs(year["car"]["value_pence"] - 850_000) <= 1
    assert year["p"]["value_pence"] == 120_000
    assert year["p"]["contributions_pence"] == 120_000


# ── Recurring items ─────────────────────────────────────────────────────


def test_recurring_payment_linked_to_scheduled_loan_counts_once():
    loan = pj.Liability("l", "Loan", "loan", 1_000_000, schedule=pj.LoanTerms(
        apr_bp=0, payment=20_000, payment_day=1, start_date=date(2026, 1, 1)))
    linked = pj.Regular("r", "Loan payment", "cur", 20_000, "out", "monthly", date(2026, 7, 1), liability_id="l")
    result = pj.run(_inputs(liabilities=[loan], regulars=[linked]), months=6)
    for p in result["points"]:
        assert p["flows"]["loan_payments"] == 20_000
        assert p["flows"]["spending"] == 0
    assert result["start"]["cash_pence"] - result["end"]["cash_pence"] == 6 * 20_000


def test_recurring_payment_reduces_unscheduled_debt_and_stops_when_cleared():
    debt = pj.Liability("d", "Family loan", "other", 50_000)
    linked = pj.Regular("r", "Pay Mum", "cur", 20_000, "out", "monthly", date(2026, 7, 1), liability_id="d")
    result = pj.run(_inputs(liabilities=[debt], regulars=[linked]), months=6)
    assert [p["flows"]["loan_payments"] for p in result["points"]] == [20_000, 20_000, 10_000, 0, 0, 0]
    assert any(e["kind"] == "paid_off" for e in result["events"])


def test_unscheduled_debt_with_a_rate_accrues_interest():
    student = pj.Liability("sl", "Student loan", "student_loan", 1_200_000, interest_bp=600)
    result = pj.run(_inputs(liabilities=[student]), months=1)
    assert result["end"]["liabilities_pence"] == 1_200_000 + 6_000


def test_transfer_to_credit_card_pays_it_down_and_stops():
    accounts = [pj.Account("cur", "Current", "current", 1_000_000), pj.Account("cc", "Card", "credit_card", -25_000)]
    pay = pj.Regular("t", "Pay card", "cur", 10_000, "out", "monthly", date(2026, 7, 1), to_account_id="cc")
    result = pj.run(pj.Inputs(today=TODAY, accounts=accounts, regulars=[pay]), months=5)
    assert result["end"]["cards_pence"] == 0
    assert result["end"]["cash_pence"] == 1_000_000 - 25_000
    assert any(e["kind"] == "card_cleared" for e in result["events"])


def test_transfers_between_cash_accounts_change_nothing():
    accounts = [pj.Account("cur", "Current", "current", 100_000), pj.Account("sav", "Savings", "savings", 0)]
    move = pj.Regular("t", "Save", "cur", 5_000, "out", "weekly", date(2026, 6, 22), to_account_id="sav")
    result = pj.run(pj.Inputs(today=TODAY, accounts=accounts, regulars=[move]), months=3)
    assert result["end"]["cash_pence"] == 100_000
    assert _flow_sum(result, "spending") == 0


# ── Shortfall ───────────────────────────────────────────────────────────


def test_deficit_runs_out_in_month_three_and_shortfall_grows():
    a = pj.Assumptions(spending_override=160_000)
    result = pj.run(_inputs(cash=331_800), a, months=6)
    runs_out = [e for e in result["events"] if e["kind"] == "cash_runs_out"]
    assert runs_out[0]["date"] == "2026-09-20"  # month 3
    shortfall = [p["shortfall_pence"] for p in result["points"]]
    assert shortfall[:2] == [0, 0]
    assert shortfall[2] == 160_000 * 3 - 331_800
    assert [b - a for a, b in zip(shortfall[2:], shortfall[3:])] == [160_000] * 3
    assert all(p["cash_pence"] == 0 for p in result["points"][2:])
    assert result["shortfall"]["peak_pence"] == shortfall[-1]
    assert result["shortfall"]["runs_out_month"] == "2026-09"
    # Net worth stays honest: it falls by the deficit every month.
    nws = [result["start"]["net_worth_pence"]] + [p["net_worth_pence"] for p in result["points"]]
    assert all(b - a == -160_000 for a, b in zip(nws, nws[1:]))


def test_shortfall_interest_and_repayment():
    a = pj.Assumptions(spending_override=0, shortfall_rate_bp=2400)
    income = pj.Regular("s", "Salary", "cur", 50_000, "in", "monthly", date(2026, 8, 1))
    big = [{"type": "one_off", "amount": 100_000, "direction": "out", "on": date(2026, 7, 1)}]
    result = pj.run(_inputs(cash=0, regulars=[income]), a, months=4, changes=big)
    shortfall = [p["shortfall_pence"] for p in result["points"]]
    assert shortfall[0] == 100_000
    assert shortfall[1] == 100_000 + 2_000 - 50_000  # 2% a month interest, then the salary repays
    assert shortfall[3] == 0


# ── Reconciliation and determinism ──────────────────────────────────────


def _random_inputs(seed):
    r = random.Random(seed)
    accounts = [pj.Account("cur", "Current", "current", r.randint(-50_000, 500_000)),
                pj.Account("sav", "Savings", "savings", r.randint(0, 2_000_000)),
                pj.Account("cc", "Card", "credit_card", -r.randint(0, 300_000))]
    assets = [pj.Asset("house", "House", "property", r.randint(10_000_000, 40_000_000)),
              pj.Asset("car", "Car", "vehicle", r.randint(500_000, 3_000_000)),
              pj.Asset("pen", "Pension", "pension", r.randint(0, 5_000_000)),
              pj.Asset("isa", "ISA", "investment", r.randint(0, 2_000_000))]
    liabilities = [
        pj.Liability("mort", "Mortgage", "mortgage", r.randint(5_000_000, 20_000_000), linked_asset_id="house",
                     schedule=pj.LoanTerms(apr_bp=r.randint(100, 700), payment=r.randint(50_000, 150_000),
                                           payment_day=r.randint(1, 31), start_date=date(2020, 1, 1))),
        pj.Liability("pcp", "PCP", "loan", r.randint(500_000, 2_000_000), subtype="pcp",
                     schedule=pj.LoanTerms(apr_bp=r.choice([0, 690]), payment=r.randint(15_000, 40_000),
                                           payment_day=r.randint(1, 28), term_end=date(2028, r.randint(1, 12), 1),
                                           balloon=r.randint(100_000, 500_000), start_date=date(2025, 1, 1))),
        pj.Liability("sl", "Student loan", "student_loan", r.randint(500_000, 5_000_000), interest_bp=r.randint(0, 800)),
        pj.Liability("fam", "Family", "other", r.randint(0, 300_000)),
    ]
    regulars = [
        pj.Regular("sal", "Salary", "cur", r.randint(150_000, 400_000), "in", "monthly", date(2026, 6, 28)),
        pj.Regular("rent", "Council tax", "cur", r.randint(10_000, 30_000), "out", "monthly", date(2026, 7, 1)),
        pj.Regular("gym", "Gym", "cc", r.randint(1_000, 5_000), "out", "monthly", date(2026, 7, 3)),
        pj.Regular("ins", "Insurance", "cur", r.randint(30_000, 90_000), "out", "yearly", date(2026, 11, 1)),
        pj.Regular("food", "Groceries", "cur", r.randint(5_000, 15_000), "out", "weekly", date(2026, 6, 22)),
        pj.Regular("pc", "Pay card", "cur", r.randint(5_000, 50_000), "out", "monthly", date(2026, 7, 5), to_account_id="cc"),
        pj.Regular("sv", "Save", "cur", r.randint(5_000, 50_000), "out", "four_weekly", date(2026, 6, 30), to_account_id="sav"),
        pj.Regular("fp", "Pay family", "cur", r.randint(1_000, 20_000), "out", "monthly", date(2026, 7, 10), liability_id="fam"),
        pj.Regular("mp", "Mortgage DD", "cur", 90_000, "out", "monthly", date(2026, 7, 1), liability_id="mort"),
        pj.Regular("isa", "ISA", "sav", r.randint(5_000, 50_000), "out", "monthly", date(2026, 7, 6), asset_id="isa"),
        pj.Regular("bonus", "Bonus", "cur", r.randint(100_000, 500_000), "in", "yearly", date(2027, 3, 31),
                   end_date=date(2030, 4, 1)),
    ]
    inputs = pj.Inputs(today=TODAY, accounts=accounts, assets=assets, liabilities=liabilities, regulars=regulars,
                       spending_baseline=r.randint(50_000, 300_000), spending_months_used=3)
    a = pj.Assumptions(inflation_bp=r.choice([0, 250]), shortfall_rate_bp=r.choice([0, 2000]),
                       cash_interest_bp=r.choice([0, 350]))
    changes = [
        {"type": "sell", "asset_id": "house", "on": date(2031, 5, 1), "clear_liability_id": "mort", "price": None},
        {"type": "balloon", "liability_id": "pcp", "choice": r.choice(["pay", "hand_back", "refinance"]),
         "months": 24, "rate_bp": 900, "asset_id": "car"},
        {"type": "overpay", "liability_id": "sl", "amount": 10_000, "monthly": True, "from": date(2027, 1, 1),
         "until": None},
        {"type": "one_off", "amount": 750_000, "direction": "out", "on": date(2027, 8, 15)},
        {"type": "extra_income", "amount": 25_000, "from": date(2028, 1, 1), "until": date(2030, 1, 1)},
        {"type": "reduce_spending", "amount": 20_000, "from": None, "until": None},
        {"type": "growth", "asset_type": "investment", "rate_bp": -300},
    ]
    return inputs, a, changes


@pytest.mark.parametrize("seed", range(12))
def test_reconciliation_holds_over_a_messy_ten_years(seed):
    inputs, a, changes = _random_inputs(seed)
    for ch in ([], changes):
        result = pj.run(inputs, a, months=120, changes=ch)  # raises ProjectionError if anything doesn't add up
        for p in result["points"]:
            assert p["net_worth_pence"] == p["assets_pence"] - p["liabilities_pence"]
            assert p["cash_pence"] >= 0 and p["shortfall_pence"] >= 0
        for y in result["years"]:
            point = next(p for p in result["points"] if p["date"] == y["end"])
            assert y["net_worth_pence"] == point["net_worth_pence"]
        assert len(result["years"]) == 10


def test_same_inputs_give_the_same_output():
    inputs, a, changes = _random_inputs(3)
    first = json.dumps(pj.run(inputs, a, months=120, changes=changes), sort_keys=True)
    second = json.dumps(pj.run(inputs, a, months=120, changes=changes), sort_keys=True)
    assert first == second


def test_reconciliation_failure_raises(monkeypatch):
    inputs, a, _ = _random_inputs(1)
    original = pj._Run.apply_regular

    def leaky(self, r, due, f):
        original(self, r, due, f)
        self.cash += 1  # money from nowhere

    monkeypatch.setattr(pj._Run, "apply_regular", leaky)
    with pytest.raises(pj.ProjectionError):
        pj.run(inputs, a, months=12)


def test_run_does_not_change_the_assumptions_passed_in():
    inputs, a, changes = _random_inputs(2)
    before = json.dumps(pj.assumptions_to_dict(a), sort_keys=True)
    pj.run(inputs, a, months=12, changes=changes)
    assert json.dumps(pj.assumptions_to_dict(a), sort_keys=True) == before


# ── Scenarios ───────────────────────────────────────────────────────────


def test_selling_the_house_clears_the_mortgage():
    house = pj.Asset("h", "House", "property", 20_000_000)
    mort = pj.Liability("m", "Mortgage", "mortgage", 12_000_000, linked_asset_id="h", schedule=pj.LoanTerms(
        apr_bp=400, payment=80_000, payment_day=1, start_date=date(2020, 1, 1)))
    a = pj.Assumptions()
    a.growth_bp["property"] = 0
    sale = [{"type": "sell", "asset_id": "h", "on": date(2027, 1, 10), "clear_liability_id": "m", "price": None}]
    result = pj.run(_inputs(cash=0, assets=[house], liabilities=[mort]), a, months=12, changes=sale)
    kinds = [e["kind"] for e in result["events"]]
    assert "sale" in kinds and "paid_off" in kinds
    assert result["end"]["liabilities_pence"] == result["end"]["shortfall_pence"]
    assert result["years"][0]["assets"] == []


def test_pcp_hand_back_removes_car_and_balloon():
    car = pj.Asset("car", "Car", "vehicle", 900_000)
    pcp = pj.Liability("pcp", "Car PCP", "loan", 1_000_000, subtype="pcp", schedule=pj.LoanTerms(
        apr_bp=0, payment=20_000, payment_day=1, term_end=date(2027, 6, 1), balloon=800_000,
        start_date=date(2024, 6, 1)))
    choice = [{"type": "balloon", "liability_id": "pcp", "choice": "hand_back", "asset_id": "car"}]
    result = pj.run(_inputs(assets=[car], liabilities=[pcp]), months=18, changes=choice)
    assert any(e["kind"] == "hand_back" for e in result["events"])
    assert result["end"]["liabilities_pence"] == 0
    assert all(a["id"] != "car" for a in result["years"][-1]["assets"])


def test_pcp_refinance_spreads_the_balloon():
    pcp = pj.Liability("pcp", "Car PCP", "loan", 820_000, subtype="pcp", schedule=pj.LoanTerms(
        apr_bp=0, payment=20_000, payment_day=1, term_end=date(2026, 7, 1), balloon=800_000,
        start_date=date(2023, 7, 1)))
    choice = [{"type": "balloon", "liability_id": "pcp", "choice": "refinance", "months": 24, "rate_bp": 0}]
    result = pj.run(_inputs(liabilities=[pcp]), months=30, changes=choice)
    refi = next(e for e in result["events"] if e["kind"] == "refinance")
    assert refi["amount_pence"] == 800_000
    assert _flow_sum(result, "loan_payments") == 820_000
    assert result["end"]["liabilities_pence"] == 0


def test_monthly_overpayment_pays_off_sooner():
    loan = lambda: pj.Liability("l", "Loan", "loan", 1_000_000, schedule=pj.LoanTerms(  # noqa: E731
        apr_bp=500, payment=20_000, payment_day=1, start_date=date(2026, 1, 1)))
    base = pj.run(_inputs(liabilities=[loan()]), months=120)
    faster = pj.run(_inputs(liabilities=[loan()]), months=120, changes=[
        {"type": "overpay", "liability_id": "l", "amount": 10_000, "monthly": True, "from": None, "until": None}])
    when = lambda r: next(e["date"] for e in r["events"] if e["kind"] == "paid_off")  # noqa: E731
    assert when(faster) < when(base)
    assert _flow_sum(faster, "loan_interest") < _flow_sum(base, "loan_interest")


def test_saved_scenarios_round_trip_and_compare(db):
    fin.create_account(db, OWNER, name="Current", account_type="current", opening_balance="1000")
    car = nw.create_item(db, OWNER, "asset", name="Car", asset_type="vehicle", value="9000", value_date="2026-06-01")
    s = pj.save_scenario(db, OWNER, name="Sell the car", changes=[
        {"type": "sell", "asset_id": car.id, "on": "2027-03", "price": "7000"},
        {"type": "extra_income", "amount": "1000", "from": "2027-01-01"}])
    assert pj.find_scenario(db, OWNER, "sell car").id == s.id
    listed = pj.list_scenarios(db, OWNER)
    assert listed[0]["descriptions"][0] == "Sell Car in Mar 2027 for £7,000.00"
    result = pj.projection(db, OWNER, years=2, scenario_ids=[s.id], today=TODAY)
    assert result["scenarios"][0]["name"] == "Sell the car"
    assert result["scenarios"][0]["end"]["net_worth_pence"] > result["base"]["end"]["net_worth_pence"]
    with pytest.raises(fin.FinanceError):
        pj.save_scenario(db, OWNER, name="Bad", changes=[{"type": "sell", "on": "2027-01-01"}])
    with pytest.raises(fin.FinanceError):
        pj.save_scenario(db, OWNER, name="sell the CAR", changes=[{"type": "one_off", "amount": "5", "on": "2027-01-01"}])
    pj.delete_scenario(db, OWNER, s.id)
    assert pj.list_scenarios(db, OWNER) == []


# ── Stored assumptions and inputs from the database ─────────────────────


def test_assumptions_are_stored_and_reset(db):
    a = pj.get_assumptions(db, OWNER)
    assert a.growth_bp == {"property": 150, "pension": 500, "investment": 500, "vehicle": -1500, "other": 0}
    assert a.horizon_years == 5 and a.spending_override is None
    house = nw.create_item(db, OWNER, "asset", name="House", asset_type="property", value="100000")
    a = pj.set_assumptions(db, OWNER, {"growth": {"property": "2.5%"}, "asset_growth": {house.id: "3"},
                                       "inflation": "2", "shortfall_rate": "20", "spending_override": "1,200",
                                       "horizon_years": 10, "spending_months": 6})
    assert a.growth_bp["property"] == 250 and a.asset_growth_bp == {house.id: 300}
    assert a.inflation_bp == 200 and a.shortfall_rate_bp == 2000 and a.spending_override == 120_000
    assert a.horizon_years == 10 and a.spending_months == 6
    a = pj.set_assumptions(db, OWNER, {"growth": {"property": ""}, "asset_growth": {house.id: None},
                                       "spending_override": ""})
    assert a.growth_bp["property"] == 150 and a.asset_growth_bp == {} and a.spending_override is None
    for bad in ({"horizon_years": 31}, {"growth": {"boat": 1}}, {"inflation": "-1"}, {"colour": "red"}):
        with pytest.raises(fin.FinanceError):
            pj.set_assumptions(db, OWNER, bad)
    # Another user is unaffected.
    assert pj.get_assumptions(db, "bob").growth_bp["property"] == 150


def test_spending_baseline_leaves_out_recurring_loans_and_transfers(db):
    cur = fin.create_account(db, OWNER, name="Current", account_type="current")
    sav = fin.create_account(db, OWNER, name="Savings", account_type="savings")
    loan = nw.create_item(db, OWNER, "liability", name="Loan", liability_type="loan")
    for month in (3, 4, 5):
        fin.create_transaction(db, OWNER, account_id=cur.id, date=f"2026-{month:02d}-10", amount="300",
                               direction="out", description="Shop")
        tx = fin.create_transaction(db, OWNER, account_id=cur.id, date=f"2026-{month:02d}-12", amount="150",
                                    direction="out", description="Loan")
        loans.link_transaction_payment(db, OWNER, tx.id, loan.id)
        fin.create_transfer(db, OWNER, from_account_id=cur.id, to_account_id=sav.id, date=f"2026-{month:02d}-15",
                            amount="500")
    bill = rec.create_recurring(db, OWNER, name="Phone", account_id=cur.id, amount="20", next_due="2026-03-20")
    for month in (3, 4, 5):
        rec.confirm(db, OWNER, bill.id, f"2026-{month:02d}-20")
    # June (this month) doesn't count: it's not a full month yet.
    fin.create_transaction(db, OWNER, account_id=cur.id, date="2026-06-02", amount="999", direction="out")
    assert pj.spending_baseline(db, OWNER, TODAY, 3) == (30_000, 3)
    assert pj.spending_baseline(db, OWNER, TODAY, 6) == (30_000, 3)  # only three months of history


def test_load_inputs_warnings(db):
    fin.create_account(db, OWNER, name="Current", account_type="current")
    nw.create_item(db, OWNER, "asset", name="Old ISA", asset_type="investment", value="100", value_date="2024-01-01")
    nw.create_item(db, OWNER, "asset", name="Watch", asset_type="other")
    nw.create_item(db, OWNER, "liability", name="IOU", liability_type="other", value="50")
    pcp = nw.create_item(db, OWNER, "liability", name="Car PCP", liability_type="loan", subtype="pcp")
    loans.set_schedule(db, OWNER, pcp.id, start_date="2026-01-01", start_balance="10000", apr="0")
    kinds = {w["kind"] for w in pj.load_inputs(db, OWNER, today=TODAY).warnings}
    assert {"stale_value", "no_value", "flat_liability", "no_payment", "no_income", "no_vehicle",
            "short_history"} <= kinds


def test_value_at_finds_a_future_balance():
    loan = pj.Liability("l", "Loan", "loan", 1_000_000, schedule=pj.LoanTerms(
        apr_bp=0, payment=10_000, payment_day=1, start_date=date(2026, 1, 1)))
    result = pj.run(_inputs(liabilities=[loan]), months=60)
    found = pj.value_at(result, "l", date(2028, 1, 1))
    assert found["kind"] == "liability" and found["date"] == "2028-06-20"
    assert found["value_pence"] == 1_000_000 - 24 * 10_000


def test_horizon_limits():
    with pytest.raises(fin.FinanceError):
        pj.run(_inputs(), months=0)
    with pytest.raises(fin.FinanceError):
        pj.run(_inputs(), months=361)
    assert len(pj.run(_inputs(), months=360)["years"]) == 30
