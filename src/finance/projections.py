"""Multi-year projections: a deterministic month-by-month engine.

All projection maths happens here, never in the chat model. The engine
steps one month at a time from today and reports monthly points, yearly
rows, events, the assumptions it used and data warnings.

How each month is worked out (month k covers the day after
``today + (k-1) months`` up to ``today + k months``):

1. Scenario one-offs and sales dated in the month.
2. Salary from payroll (Stage 2) and recurring income.
3. Loans with a schedule: each payment uses ``loans.payment_step``, the same
   maths as the loan payoff projection (monthly interest, payment day, term
   end, balloon). A recurring payment linked to such a loan *is* that
   payment, so it is not counted again. After payoff the payment stops.
4. Other recurring items: payments towards liabilities without a schedule
   reduce them (and stop at zero), contributions add to their asset,
   transfers to a credit card pay it down, the rest is income or spending.
5. Day-to-day spending not covered by recurring items: the average of the
   last few full months of other outgoings (an editable assumption).
6. Interest on liabilities without a schedule (only if a rate is set),
   asset growth (an annual rate applied monthly so a year compounds to
   exactly that rate), interest on cash.
7. If cash would go below zero, the gap becomes a separate projected debt,
   "Shortfall (borrowing needed)", with its own interest rate. Spare cash
   pays it back first.

Every month is reconciled twice and an error is raised rather than
returning numbers that don't add up:

- liquid money (cash + card balances - shortfall) changes by exactly
  income - spending - loan payments - contributions + interest received
  - shortfall interest + sale proceeds;
- net worth changes by exactly income - spending - interest charged +
  interest received + asset growth + employer/tax-relief contributions +
  gains or losses on sales and hand-backs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from src.finance.models import (
    FinanceAccount,
    FinanceAsset,
    FinanceContribution,
    FinanceLiability,
    FinanceProjectionScenario,
    FinanceProjectionSetting,
    FinanceRecurring,
    FinanceRecurringEvent,
    FinanceRepayment,
    FinanceTransaction,
)
from src.finance.service import (
    FinanceError,
    FinanceNotFound,
    _clean_text,
    _new_id,
    _require_owner,
    _today_for_owner,
    parse_date,
)

MAX_YEARS = 30
CASH_TYPES = ("current", "savings", "cash", "child_savings", "child_current")
GROWTH_TYPES = ("property", "pension", "investment", "vehicle", "other")
SHORTFALL_ID = "shortfall"
SHORTFALL_NAME = "Shortfall (borrowing needed)"
STALE_VALUE_DAYS = 366

# Defaults in basis points (150 = 1.5%) unless noted.
ASSUMPTION_DEFAULTS = {
    "growth:property": 150,
    "growth:pension": 500,
    "growth:investment": 500,
    "growth:vehicle": -1500,
    "growth:other": 0,
    "cash_interest": 0,
    "inflation": 0,  # on day-to-day spending; 0 = off
    "shortfall_rate": 0,
    "spending_months": 3,  # months of history for the spending baseline
    "spending_override": None,  # pence a month, None = use the baseline
    "horizon_years": 5,
}
ASSUMPTION_LABELS = {
    "growth:property": "Property growth", "growth:pension": "Pension growth",
    "growth:investment": "Investment growth", "growth:vehicle": "Vehicle value",
    "growth:other": "Other assets", "cash_interest": "Cash interest", "inflation": "Inflation on spending",
    "shortfall_rate": "Shortfall interest", "spending_months": "Spending baseline months",
    "spending_override": "Day-to-day spending", "horizon_years": "Horizon",
}
RATE_LIMITS = {"growth": (-5000, 5000), "cash_interest": (0, 2000), "inflation": (0, 2000),
               "shortfall_rate": (0, 10000)}

CHANGE_TYPES = ("extra_income", "reduce_spending", "one_off", "overpay", "sell", "balloon", "growth", "salary")
BALLOON_CHOICES = ("pay", "hand_back", "refinance")


class ProjectionError(FinanceError):
    """The projection failed its own reconciliation (a bug, never shown as numbers)."""


# ── Inputs (plain data, so the engine has no database access) ──────────


@dataclass
class Account:
    id: str
    name: str
    type: str
    balance: int

    @property
    def is_card(self) -> bool:
        return self.type == "credit_card"


@dataclass
class Asset:
    id: str
    name: str
    type: str
    value: int
    value_date: Optional[date] = None
    subtype: str = ""


@dataclass
class LoanTerms:
    apr_bp: int
    payment: Optional[int]
    payment_day: int
    term_end: Optional[date] = None
    balloon: int = 0
    start_date: Optional[date] = None


@dataclass
class Liability:
    id: str
    name: str
    type: str
    balance: int
    schedule: Optional[LoanTerms] = None
    interest_bp: Optional[int] = None
    linked_asset_id: Optional[str] = None
    subtype: str = ""


@dataclass
class Regular:
    """A recurring item. Attribute names match FinanceRecurring so
    ``recurring.due_dates`` works on both."""

    id: str
    name: str
    account_id: str
    amount_pence: int
    direction: str
    frequency: str
    anchor_date: date
    end_date: Optional[date] = None
    to_account_id: Optional[str] = None
    liability_id: Optional[str] = None
    asset_id: Optional[str] = None
    job_id: Optional[str] = None


@dataclass
class Inputs:
    today: date
    accounts: list = field(default_factory=list)
    assets: list = field(default_factory=list)
    liabilities: list = field(default_factory=list)
    regulars: list = field(default_factory=list)
    spending_baseline: int = 0
    spending_months_used: int = 0
    warnings: list = field(default_factory=list)
    jobs: list = field(default_factory=list)  # payroll jobs (Stage 2)
    payroll: object = None  # callable(job, start, end, changes) -> list of pay periods


@dataclass
class Assumptions:
    growth_bp: dict = field(default_factory=lambda: {t: ASSUMPTION_DEFAULTS[f"growth:{t}"] for t in GROWTH_TYPES})
    asset_growth_bp: dict = field(default_factory=dict)  # asset id -> bp
    cash_interest_bp: int = 0
    inflation_bp: int = 0
    shortfall_rate_bp: int = 0
    spending_months: int = 3
    spending_override: Optional[int] = None
    horizon_years: int = 5

    def growth_for(self, asset: Asset) -> int:
        if asset.id in self.asset_growth_bp:
            return self.asset_growth_bp[asset.id]
        return self.growth_bp.get(asset.type, 0)


# ── Small helpers ──────────────────────────────────────────────────────


def _add_months(day: date, months: int) -> date:
    from src.finance.loans import add_months

    return add_months(day, months)


def _monthly_factor(annual_bp: int) -> float:
    """Monthly growth factor so twelve months compound to exactly the annual rate."""
    return (1 + annual_bp / 10000) ** (1 / 12)


def _nominal_interest(balance: int, annual_bp: int) -> int:
    """A month's interest at APR / 12 (the same convention as loans)."""
    return int(round(balance * annual_bp / 10000 / 12)) if balance > 0 and annual_bp else 0


def _iso(day: Optional[date]) -> Optional[str]:
    return day.isoformat() if day else None


def _month_label(day: date) -> str:
    return day.strftime("%b %Y")


def _pounds(pence: int) -> str:
    sign = "-" if pence < 0 else ""
    return f"{sign}£{abs(pence) / 100:,.2f}"


def _percent(bp: int) -> str:
    text = f"{bp / 100:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


# ── Engine ─────────────────────────────────────────────────────────────


class _Run:
    def __init__(self, inputs: Inputs, assumptions: Assumptions, months: int, changes: list):
        self.inputs = inputs
        self.a = assumptions
        self.months = months
        self.changes = changes
        self.today = inputs.today
        self.accounts = {a.id: a for a in inputs.accounts}
        self.cash = sum(a.balance for a in inputs.accounts if not a.is_card)
        self.cards = {a.id: a.balance for a in inputs.accounts if a.is_card}
        self.assets = {a.id: a for a in inputs.assets}
        self.asset_value = {a.id: float(a.value) for a in inputs.assets}
        self.liabs = {l.id: l for l in inputs.liabilities}
        self.balance = {l.id: l.balance for l in inputs.liabilities}
        self.shortfall = 0
        self.events: list = []
        self.stopped: set = set()  # regular ids that have ended (loan paid off, card cleared)
        self.sold: set = set()
        self.terms: dict = {}
        self.final_paid: set = set()
        from src.finance.loans import next_payment_date

        for l in inputs.liabilities:
            t = l.schedule
            if t is None or not t.payment or l.balance <= 0:
                continue
            first = next_payment_date(max(self.today, (t.start_date or self.today) - timedelta(days=1)), t.payment_day)
            self.terms[l.id] = {"rate": t.apr_bp / 10000 / 12, "payment": t.payment, "day": t.payment_day,
                                "term_end": t.term_end, "balloon": t.balloon, "next_due": first}
        # Payroll-fed assets: manual contribution items to them would double count.
        self.job_assets = {j.pension_asset_id for j in inputs.jobs if getattr(j, "pension_asset_id", None)}
        self.change_growth()
        self.monthly_payment = {}  # liability id -> regular monthly payment (for cash flow events)
        for lid, t in self.terms.items():
            self.monthly_payment[lid] = t["payment"]
        for r in inputs.regulars:
            if r.liability_id and r.liability_id not in self.terms and not r.to_account_id:
                self.monthly_payment[r.liability_id] = self.monthly_payment.get(r.liability_id, 0) + \
                    _monthly_equivalent(r)

    def change_growth(self):
        for c in self.changes:
            if c["type"] != "growth":
                continue
            if c.get("asset_id"):
                self.a.asset_growth_bp[c["asset_id"]] = c["rate_bp"]
            else:
                self.a.growth_bp[c["asset_type"]] = c["rate_bp"]

    # Money movements. Everything that changes a balance goes through here.

    def pool_add(self, account_id: Optional[str], delta: int):
        if account_id in self.cards:
            self.cards[account_id] += delta
        else:
            self.cash += delta

    def assets_total(self) -> int:
        return (self.cash + sum(max(0, v) for v in self.cards.values())
                + sum(int(round(v)) for v in self.asset_value.values()))

    def liabilities_total(self) -> int:
        return sum(max(0, -v) for v in self.cards.values()) + sum(self.balance.values()) + self.shortfall

    def net_worth(self) -> int:
        return self.assets_total() - self.liabilities_total()

    def liquid(self) -> int:
        return self.cash + sum(self.cards.values()) - self.shortfall

    def event(self, day: date, kind: str, text: str, **extra):
        self.events.append({"date": day.isoformat(), "month": day.strftime("%Y-%m"), "kind": kind, "text": text,
                            **extra})

    # Loans with a schedule.

    def pay_scheduled(self, lid: str, start: date, end: date, f: dict):
        from src.finance.loans import add_months, payment_step

        t = self.terms.get(lid)
        while t is not None and t["next_due"] <= end and self.balance[lid] > 0:
            due = t["next_due"]
            liab = self.liabs[lid]
            final = t["term_end"] is not None and due >= t["term_end"]
            bal = self.balance[lid]
            balloon_choice = self.balloon_choice(lid) if final and t["balloon"] else None
            if balloon_choice in ("hand_back", "refinance"):
                interest = int(round(bal * t["rate"]))
                paid = min(t["payment"], bal + interest)
                remaining = bal + interest - paid
                self.pool_add(None, -paid)
                f["interest_charged"] += interest
                f["loan_payments"] += paid
                f["loan_interest"] += interest
                f["loan_capital"] += paid - interest
                if balloon_choice == "hand_back":
                    vehicle = self.handback_asset(lid)
                    value = int(round(self.asset_value.pop(vehicle))) if vehicle else 0
                    if vehicle:
                        self.sold.add(vehicle)
                    self.balance[lid] = 0
                    f["gains"] += remaining - value
                    self.event(due, "hand_back",
                               f"{liab.name}: car handed back at the final payment ({_pounds(remaining)} balloon "
                               f"not paid{f', {self.assets[vehicle].name} removed' if vehicle else ''})",
                               item_id=lid, amount_pence=remaining)
                    self.end_loan(lid, due, "ends")
                    return
                c = self.balloon_change(lid)
                months = c.get("months") or 36
                rate_bp = c.get("rate_bp") or 0
                from src.finance.loans import standard_payment

                self.balance[lid] = remaining
                t.update(rate=rate_bp / 10000 / 12, payment=standard_payment(remaining, rate_bp, months),
                         term_end=add_months(due, months, t["day"]), balloon=0,
                         next_due=add_months(due, 1, t["day"]))
                self.monthly_payment[lid] = t["payment"]
                self.event(due, "refinance",
                           f"{liab.name}: {_pounds(remaining)} balloon refinanced over {months} months at "
                           f"{_percent(rate_bp)} ({_pounds(t['payment'])} a month)",
                           item_id=lid, amount_pence=remaining)
                continue
            interest, paid, after = payment_step(bal, t["rate"], t["payment"], final=final)
            self.pool_add(None, -paid)
            self.balance[lid] = after
            f["interest_charged"] += interest
            f["loan_payments"] += paid
            f["loan_interest"] += interest
            f["loan_capital"] += paid - interest
            t["next_due"] = add_months(due, 1, t["day"])
            if after == 0:
                if final and t["balloon"]:
                    self.event(due, "balloon", f"{liab.name}: final (balloon) payment of {_pounds(paid)} due",
                               item_id=lid, amount_pence=paid)
                self.end_loan(lid, due, "paid off")

    def balloon_change(self, lid: str) -> dict:
        return next((c for c in self.changes if c["type"] == "balloon" and c["liability_id"] == lid), {})

    def balloon_choice(self, lid: str) -> Optional[str]:
        return self.balloon_change(lid).get("choice")

    def handback_asset(self, lid: str) -> Optional[str]:
        chosen = self.balloon_change(lid).get("asset_id")
        if chosen and chosen in self.asset_value:
            return chosen
        vehicles = [a.id for a in self.inputs.assets if a.type == "vehicle" and a.id in self.asset_value]
        return vehicles[0] if len(vehicles) == 1 else None

    def end_loan(self, lid: str, day: date, how: str):
        self.terms.pop(lid, None)
        for r in self.inputs.regulars:
            if r.liability_id == lid:
                self.stopped.add(r.id)
        liab = self.liabs[lid]
        payment = self.monthly_payment.get(lid, 0)
        self.event(day, "paid_off", f"{liab.name} {how} {_month_label(day)}", item_id=lid,
                   monthly_change_pence=payment)

    # Recurring items.

    def apply_regular(self, r: Regular, due: date, f: dict):
        if r.id in self.stopped or r.job_id:
            return
        amount = r.amount_pence
        if r.to_account_id:
            to_card = r.to_account_id in self.cards
            from_card = r.account_id in self.cards
            if not to_card and not from_card:
                return  # between cash accounts: no change
            if to_card:
                owed = -self.cards[r.to_account_id]
                if owed <= 0:
                    self.stopped.add(r.id)
                    self.event(due, "card_cleared",
                               f"{self.accounts[r.to_account_id].name} cleared; {r.name} stops", item_id=r.id,
                               monthly_change_pence=_monthly_equivalent(r))
                    return
                amount = min(amount, owed)
            self.pool_add(r.account_id, -amount)
            self.pool_add(r.to_account_id, amount)
            f["card_payments"] += amount
            return
        if r.liability_id:
            if r.liability_id in self.terms or r.liability_id not in self.balance:
                return  # the schedule is the payment
            bal = self.balance[r.liability_id]
            if bal <= 0:
                self.stopped.add(r.id)
                return
            paid = min(amount, bal)
            self.pool_add(r.account_id, -paid)
            self.balance[r.liability_id] = bal - paid
            f["loan_payments"] += paid
            f["loan_capital"] += paid
            if bal - paid == 0:
                self.end_loan(r.liability_id, due, "paid off")
            return
        if r.asset_id:
            if r.asset_id in self.sold or r.asset_id not in self.asset_value or r.asset_id in self.job_assets:
                return
            self.pool_add(r.account_id, -amount)
            self.asset_value[r.asset_id] += amount
            f["contributions"] += amount
            f["asset_contributions"][r.asset_id] = f["asset_contributions"].get(r.asset_id, 0) + amount
            return
        if r.direction == "in":
            self.pool_add(r.account_id, amount)
            f["income"] += amount
        else:
            self.pool_add(r.account_id, -amount)
            f["spending"] += amount

    # Scenario changes.

    def active(self, c: dict, start: date, end: date) -> bool:
        begins = c.get("from") or self.today
        until = c.get("until")
        return begins <= end and (until is None or until > start)

    def apply_changes_dated(self, start: date, end: date, f: dict):
        for c in self.changes:
            on = c.get("on")
            kind = c["type"]
            if kind == "one_off" and start < on <= end:
                if c["direction"] == "in":
                    self.cash += c["amount"]
                    f["income"] += c["amount"]
                else:
                    self.cash -= c["amount"]
                    f["spending"] += c["amount"]
            elif kind == "sell" and start < on <= end and c["asset_id"] in self.asset_value:
                self.sell(c, on, f)
            elif kind == "overpay" and not c.get("monthly") and start < on <= end:
                self.overpay(c["liability_id"], c["amount"], on, f)

    def apply_changes_monthly(self, start: date, end: date, f: dict):
        for c in self.changes:
            if c["type"] == "extra_income" and self.active(c, start, end):
                self.cash += c["amount"]
                f["income"] += c["amount"]
            elif c["type"] == "reduce_spending" and self.active(c, start, end):
                cut = min(c["amount"], f["baseline_spending"])
                self.cash += cut
                f["spending"] -= cut
                f["baseline_spending"] -= cut
            elif c["type"] == "overpay" and c.get("monthly") and self.active(c, start, end):
                self.overpay(c["liability_id"], c["amount"], end, f)

    def overpay(self, lid: str, amount: int, day: date, f: dict):
        bal = self.balance.get(lid, 0)
        if bal <= 0:
            return
        paid = min(amount, bal)
        self.cash -= paid
        self.balance[lid] = bal - paid
        f["loan_payments"] += paid
        f["loan_capital"] += paid
        f["overpayments"] += paid
        if bal - paid == 0:
            self.end_loan(lid, day, "paid off")

    def sell(self, c: dict, day: date, f: dict):
        asset = self.assets[c["asset_id"]]
        value = int(round(self.asset_value.pop(asset.id)))
        self.sold.add(asset.id)
        price = c.get("price") if c.get("price") is not None else value
        to_loan = 0
        lid = c.get("clear_liability_id")
        if lid and self.balance.get(lid, 0) > 0:
            to_loan = min(price, self.balance[lid])
            self.balance[lid] -= to_loan
            f["loan_payments"] += to_loan
            f["loan_capital"] += to_loan
            f["sale_to_loans"] += to_loan
        self.cash += price - to_loan
        f["sale_proceeds"] += price - to_loan
        f["gains"] += price - value
        text = f"{asset.name} sold for {_pounds(price)}"
        if to_loan:
            text += f", {_pounds(to_loan)} clearing {self.liabs[lid].name}"
        self.event(day, "sale", text, item_id=asset.id, amount_pence=price)
        if lid and to_loan and self.balance[lid] == 0:
            self.end_loan(lid, day, "paid off")

    # One month.

    def step(self, k: int) -> dict:
        from src.finance.recurring import due_dates

        start, end = _add_months(self.today, k - 1), _add_months(self.today, k)
        before_nw, before_liquid = self.net_worth(), self.liquid()
        f = {key: 0 for key in ("income", "spending", "baseline_spending", "loan_payments", "loan_interest",
                                "loan_capital", "overpayments", "contributions", "external_contributions",
                                "card_payments", "interest_charged", "cash_interest", "shortfall_interest",
                                "growth", "gains", "sale_proceeds", "sale_to_loans", "salary_gross", "tax", "ni",
                                "student_loan", "pension_employee", "pension_employer", "pension_relief",
                                "payroll_loan_repaid")}
        f["asset_contributions"] = {}
        self.apply_changes_dated(start, end, f)
        if self.inputs.payroll is not None:
            self.inputs.payroll(self, start, end, f)
        regular_days = sorted(((d, r) for r in self.inputs.regulars
                               for d in due_dates(r, start + timedelta(days=1), end)),
                              key=lambda pair: (pair[0], pair[1].direction != "in", pair[1].id))
        for due, r in regular_days:
            if r.direction == "in" and not r.to_account_id:
                self.apply_regular(r, due, f)
        for lid in sorted(self.terms):
            self.pay_scheduled(lid, start, end, f)
        for due, r in regular_days:
            if not (r.direction == "in" and not r.to_account_id):
                self.apply_regular(r, due, f)
        # Day-to-day spending.
        base = self.a.spending_override if self.a.spending_override is not None else self.inputs.spending_baseline
        spend = int(round(base * (1 + self.a.inflation_bp / 10000) ** ((k - 1) // 12)))
        self.cash -= spend
        f["spending"] += spend
        f["baseline_spending"] = spend
        self.apply_changes_monthly(start, end, f)
        # Interest on liabilities without a schedule.
        for lid in sorted(self.balance):
            liab = self.liabs[lid]
            if lid in self.terms or liab.schedule is not None:
                continue
            interest = _nominal_interest(self.balance[lid], liab.interest_bp or 0)
            self.balance[lid] += interest
            f["interest_charged"] += interest
        # Growth.
        for aid in sorted(self.asset_value):
            old = self.asset_value[aid]
            new = old * _monthly_factor(self.a.growth_for(self.assets[aid]))
            self.asset_value[aid] = max(0.0, new)
            f["growth"] += int(round(self.asset_value[aid])) - int(round(old))
        received = _nominal_interest(self.cash, self.a.cash_interest_bp)
        self.cash += received
        f["cash_interest"] = received
        # Shortfall.
        owed = _nominal_interest(self.shortfall, self.a.shortfall_rate_bp)
        self.shortfall += owed
        f["shortfall_interest"] = owed
        f["interest_charged"] += owed
        if self.cash < 0:
            if self.shortfall == 0 and not any(e["kind"] == "cash_runs_out" for e in self.events):
                self.event(end, "cash_runs_out", f"Cash runs out {_month_label(end)}",
                           amount_pence=-self.cash)
            self.shortfall += -self.cash
            self.cash = 0
        elif self.shortfall and self.cash > 0:
            repay = min(self.cash, self.shortfall)
            self.cash -= repay
            self.shortfall -= repay
        self.reconcile(k, f, before_nw, before_liquid)
        return self.point(end, f)

    def reconcile(self, k: int, f: dict, before_nw: int, before_liquid: int):
        liquid_change = self.liquid() - before_liquid
        expected_liquid = (f["income"] - f["spending"] - (f["loan_payments"] - f["sale_to_loans"])
                           - f["contributions"] + f["cash_interest"] - f["shortfall_interest"] + f["sale_proceeds"])
        if liquid_change != expected_liquid:
            raise ProjectionError(f"month {k}: cash moved by {liquid_change} but the flows add up to {expected_liquid}")
        nw_change = self.net_worth() - before_nw
        expected_nw = (f["income"] - f["spending"] - f["interest_charged"] + f["cash_interest"] + f["growth"]
                       + f["external_contributions"] + f["payroll_loan_repaid"] + f["gains"])
        if nw_change != expected_nw:
            raise ProjectionError(f"month {k}: net worth moved by {nw_change} but the flows add up to {expected_nw}")

    def point(self, end: date, f: dict) -> dict:
        return {
            "date": end.isoformat(), "month": end.strftime("%Y-%m"), "cash_pence": self.cash,
            "assets_pence": self.assets_total(), "liabilities_pence": self.liabilities_total(),
            "net_worth_pence": self.net_worth(), "shortfall_pence": self.shortfall,
            "cards_pence": sum(self.cards.values()),
            "flows": {k: v for k, v in f.items() if k != "asset_contributions"},
            "assets": {aid: int(round(v)) for aid, v in self.asset_value.items()},
            "liabilities": dict(self.balance),
            "asset_contributions": dict(f["asset_contributions"]),
        }


def _monthly_equivalent(r: Regular) -> int:
    per_year = {"weekly": 52, "fortnightly": 26, "four_weekly": 13, "monthly": 12, "quarterly": 4, "yearly": 1}
    return int(round(r.amount_pence * per_year.get(r.frequency, 12) / 12))


FLOW_KEYS = ("income", "spending", "loan_payments", "loan_interest", "loan_capital", "overpayments",
             "contributions", "external_contributions", "card_payments", "interest_charged", "cash_interest",
             "shortfall_interest", "growth", "gains", "salary_gross", "tax", "ni", "student_loan",
             "pension_employee", "pension_employer", "pension_relief")


def run(inputs: Inputs, assumptions: Optional[Assumptions] = None, *, months: Optional[int] = None,
        changes: Optional[list] = None) -> dict:
    """Run the projection. Pure: the same inputs always give the same output."""
    a = replace(assumptions or Assumptions(), growth_bp=dict((assumptions or Assumptions()).growth_bp),
                asset_growth_bp=dict((assumptions or Assumptions()).asset_growth_bp))
    months = a.horizon_years * 12 if months is None else months
    if not 1 <= months <= MAX_YEARS * 12:
        raise FinanceError(f"horizon must be between 1 and {MAX_YEARS} years")
    engine = _Run(inputs, a, months, list(changes or []))
    start = {
        "date": inputs.today.isoformat(), "month": inputs.today.strftime("%Y-%m"), "cash_pence": engine.cash,
        "assets_pence": engine.assets_total(), "liabilities_pence": engine.liabilities_total(),
        "net_worth_pence": engine.net_worth(), "shortfall_pence": 0, "cards_pence": sum(engine.cards.values()),
        "assets": {aid: int(round(v)) for aid, v in engine.asset_value.items()},
        "liabilities": dict(engine.balance),
    }
    points = [engine.step(k) for k in range(1, months + 1)]
    return _report(inputs, a, engine, start, points)


def _report(inputs: Inputs, a: Assumptions, engine: _Run, start: dict, points: list) -> dict:
    names = {x.id: x.name for x in inputs.assets}
    liab_names = {x.id: x.name for x in inputs.liabilities}
    mortgages: dict = {}
    for l in inputs.liabilities:
        if l.linked_asset_id:
            mortgages.setdefault(l.linked_asset_id, []).append(l.id)
    years = []
    for y in range(1, len(points) // 12 + (1 if len(points) % 12 else 0) + 1):
        chunk = points[(y - 1) * 12: y * 12]
        if not chunk:
            break
        last = chunk[-1]
        flows = {k: sum(p["flows"].get(k, 0) for p in chunk) for k in FLOW_KEYS}
        contributions: dict = {}
        for p in chunk:
            for aid, v in p["asset_contributions"].items():
                contributions[aid] = contributions.get(aid, 0) + v
        assets = [{"id": aid, "name": names.get(aid, aid), "type": engine.assets[aid].type, "value_pence": v,
                   "contributions_pence": contributions.get(aid, 0)}
                  for aid, v in last["assets"].items()]
        equity = [{"asset_id": aid, "name": names.get(aid, aid),
                   "equity_pence": last["assets"].get(aid, 0) - sum(last["liabilities"].get(m, 0) for m in ids)}
                  for aid, ids in mortgages.items() if aid in last["assets"]]
        years.append({
            "year": y, "label": f"Year {y}", "months": len(chunk), "start": chunk[0]["date"] if y > 1 else start["date"],
            "end": last["date"], "end_label": _month_label(date.fromisoformat(last["date"])),
            "net_worth_pence": last["net_worth_pence"], "cash_pence": last["cash_pence"],
            "shortfall_pence": last["shortfall_pence"], "cards_pence": last["cards_pence"],
            "assets_pence": last["assets_pence"], "liabilities_pence": last["liabilities_pence"],
            "assets": assets,
            "liabilities": [{"id": lid, "name": liab_names.get(lid, lid), "balance_pence": v}
                            for lid, v in last["liabilities"].items()],
            "property_equity": equity, **{f"{k}_pence": v for k, v in flows.items()},
        })
    # Cash flow changes when payments end.
    for e in engine.events:
        if e["kind"] in ("paid_off", "card_cleared") and e.get("monthly_change_pence"):
            k = next(i for i, p in enumerate(points) if p["date"] >= e["date"])
            window = points[max(0, k - 12):k] or points[:1]
            before = int(round(sum(_cash_flow(p["flows"]) for p in window) / len(window)))
            e["cash_flow_before_pence"] = before
            e["cash_flow_after_pence"] = before + e["monthly_change_pence"]
            ended = date.fromisoformat(e["date"])
            e["from_month"] = _add_months(date(ended.year, ended.month, 1), 1).strftime("%Y-%m")
            e["followup"] = (f"Monthly cash flow improves by {_pounds(e['monthly_change_pence'])} from "
                             f"{_month_label(_add_months(date(ended.year, ended.month, 1), 1))} "
                             f"(about {_pounds(before)} to {_pounds(before + e['monthly_change_pence'])} a month)")
    shortfalls = [p["shortfall_pence"] for p in points]
    peak = max(shortfalls) if shortfalls else 0
    runs_out = next((e for e in engine.events if e["kind"] == "cash_runs_out"), None)
    return {
        "today": inputs.today.isoformat(), "months": len(points), "years_count": len(years),
        "start": start, "points": [{k: v for k, v in p.items() if k not in ("assets", "liabilities", "asset_contributions")}
                                   for p in points],
        "years": years, "events": sorted(engine.events, key=lambda e: (e["date"], e["kind"])),
        "shortfall": {"name": SHORTFALL_NAME, "rate_bp": a.shortfall_rate_bp,
                      "runs_out": runs_out["date"] if runs_out else None,
                      "runs_out_month": runs_out["month"] if runs_out else None,
                      "peak_pence": peak,
                      "peak_date": points[shortfalls.index(peak)]["date"] if peak else None,
                      "year_end_pence": [y["shortfall_pence"] for y in years]},
        "assumptions": assumptions_used(inputs, a),
        "warnings": list(inputs.warnings),
        "end": points[-1] if points else start,
    }


def _cash_flow(f: dict) -> int:
    return f["income"] - f["spending"] - f["loan_payments"] - f["contributions"]


def assumptions_used(inputs: Inputs, a: Assumptions) -> list:
    rows = [{"key": f"growth:{t}", "label": ASSUMPTION_LABELS[f"growth:{t}"], "value": _percent(a.growth_bp[t]) + " a year"}
            for t in GROWTH_TYPES]
    for asset in inputs.assets:
        if asset.id in a.asset_growth_bp:
            rows.append({"key": f"growth_asset:{asset.id}", "label": f"{asset.name} growth",
                         "value": _percent(a.asset_growth_bp[asset.id]) + " a year"})
    if a.spending_override is not None:
        spend = f"{_pounds(a.spending_override)} a month (set by you)"
    else:
        spend = (f"{_pounds(inputs.spending_baseline)} a month (average of the last {inputs.spending_months_used} "
                 f"full month{'s' if inputs.spending_months_used != 1 else ''} of spending not covered by "
                 "recurring items)")
    rows += [
        {"key": "spending", "label": "Day-to-day spending", "value": spend},
        {"key": "inflation", "label": "Inflation on spending",
         "value": _percent(a.inflation_bp) + " a year" if a.inflation_bp else "Off"},
        {"key": "cash_interest", "label": "Cash interest", "value": _percent(a.cash_interest_bp)},
        {"key": "shortfall_rate", "label": "Shortfall interest", "value": _percent(a.shortfall_rate_bp)},
        {"key": "cards", "label": "Credit cards", "value": "Held at today's balance unless a regular transfer pays them"},
    ]
    return rows


# ── Assumptions: stored per user ───────────────────────────────────────


def _settings(db: Session, owner: str) -> dict:
    return {row.key: row.value for row in db.query(FinanceProjectionSetting).filter(
        FinanceProjectionSetting.owner == owner)}


def get_assumptions(db: Session, owner) -> Assumptions:
    owner = _require_owner(owner)
    stored = _settings(db, owner)

    def value(key):
        raw = stored.get(key)
        if raw is None or raw == "":
            return ASSUMPTION_DEFAULTS[key]
        return int(raw)

    return Assumptions(
        growth_bp={t: value(f"growth:{t}") for t in GROWTH_TYPES},
        asset_growth_bp={k.split(":", 1)[1]: int(v) for k, v in stored.items() if k.startswith("growth_asset:")},
        cash_interest_bp=value("cash_interest"), inflation_bp=value("inflation"),
        shortfall_rate_bp=value("shortfall_rate"), spending_months=value("spending_months"),
        spending_override=value("spending_override"), horizon_years=value("horizon_years"),
    )


def assumptions_to_dict(a: Assumptions) -> dict:
    return {
        "growth_percent": {t: a.growth_bp[t] / 100 for t in GROWTH_TYPES},
        "asset_growth_percent": {k: v / 100 for k, v in sorted(a.asset_growth_bp.items())},
        "cash_interest_percent": a.cash_interest_bp / 100, "inflation_percent": a.inflation_bp / 100,
        "shortfall_rate_percent": a.shortfall_rate_bp / 100, "spending_months": a.spending_months,
        "spending_override_pence": a.spending_override, "horizon_years": a.horizon_years,
        "defaults": {"growth_percent": {t: ASSUMPTION_DEFAULTS[f"growth:{t}"] / 100 for t in GROWTH_TYPES},
                     "spending_months": ASSUMPTION_DEFAULTS["spending_months"],
                     "horizon_years": ASSUMPTION_DEFAULTS["horizon_years"]},
    }


def parse_rate(value, *, field: str, low: int, high: int) -> int:
    """'1.5', '1.5%', -15 -> basis points, within [low, high]."""
    text = str(value if value is not None else "").strip().rstrip("%").strip()
    try:
        bp = int(round(float(text) * 100))
    except ValueError as exc:
        raise FinanceError(f"{field} must be a percentage such as 1.5") from exc
    if not low <= bp <= high:
        raise FinanceError(f"{field} must be between {low / 100:g}% and {high / 100:g}%")
    return bp


def _put(db: Session, owner: str, key: str, value) -> None:
    row = db.query(FinanceProjectionSetting).filter(FinanceProjectionSetting.owner == owner,
                                                    FinanceProjectionSetting.key == key).first()
    if value is None:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        db.add(FinanceProjectionSetting(owner=owner, key=key, value=str(value)))
    else:
        row.value = str(value)


def set_assumptions(db: Session, owner, changes: dict) -> Assumptions:
    """Save assumptions. Rates are percentages; ``None`` or "" resets one to its default.

    Accepts ``growth`` ({type: percent}), ``asset_growth`` ({asset id: percent or None}),
    ``cash_interest``, ``inflation``, ``shortfall_rate``, ``spending_months``,
    ``spending_override`` (pounds a month, "" to use the baseline) and ``horizon_years``.
    """
    from src.finance.money import MoneyError, parse_pence

    owner = _require_owner(owner)
    changes = dict(changes or {})
    for t, v in (changes.pop("growth", None) or {}).items():
        if t not in GROWTH_TYPES:
            raise FinanceError(f"growth type must be one of: {', '.join(GROWTH_TYPES)}")
        _put(db, owner, f"growth:{t}", None if v in (None, "") else
             parse_rate(v, field=f"{t} growth", low=-5000, high=5000))
    for asset_id, v in (changes.pop("asset_growth", None) or {}).items():
        asset = db.query(FinanceAsset).filter(FinanceAsset.owner == owner, FinanceAsset.id == str(asset_id)).first()
        if asset is None:
            raise FinanceNotFound("asset not found")
        _put(db, owner, f"growth_asset:{asset.id}", None if v in (None, "") else
             parse_rate(v, field=f"{asset.name} growth", low=-5000, high=5000))
    for key in ("cash_interest", "inflation", "shortfall_rate"):
        if key in changes:
            v = changes.pop(key)
            low, high = RATE_LIMITS[key]
            _put(db, owner, key, None if v in (None, "") else
                 parse_rate(v, field=ASSUMPTION_LABELS[key].lower(), low=low, high=high))
    if "spending_months" in changes:
        v = changes.pop("spending_months")
        if v in (None, ""):
            _put(db, owner, "spending_months", None)
        else:
            try:
                n = int(v)
            except (TypeError, ValueError) as exc:
                raise FinanceError("spending baseline months must be a whole number") from exc
            if not 1 <= n <= 12:
                raise FinanceError("spending baseline months must be between 1 and 12")
            _put(db, owner, "spending_months", n)
    if "spending_override" in changes:
        v = changes.pop("spending_override")
        if v in (None, ""):
            _put(db, owner, "spending_override", None)
        else:
            try:
                pence = parse_pence(v)
            except MoneyError as exc:
                raise FinanceError(f"day-to-day spending: {exc}") from exc
            if pence < 0:
                raise FinanceError("day-to-day spending can't be negative")
            _put(db, owner, "spending_override", pence)
    if "horizon_years" in changes:
        v = changes.pop("horizon_years")
        if v in (None, ""):
            _put(db, owner, "horizon_years", None)
        else:
            _put(db, owner, "horizon_years", parse_years(v))
    if changes:
        raise FinanceError(f"unknown assumption: {', '.join(sorted(changes))}")
    db.flush()
    return get_assumptions(db, owner)


def parse_years(value) -> int:
    try:
        years = int(value)
    except (TypeError, ValueError) as exc:
        raise FinanceError("years must be a whole number") from exc
    if not 1 <= years <= MAX_YEARS:
        raise FinanceError(f"years must be between 1 and {MAX_YEARS}")
    return years


# ── Loading inputs from the database ───────────────────────────────────


def spending_baseline(db: Session, owner: str, today: date, months: int) -> tuple[int, int]:
    """Average monthly money out over the last *months* full months that is not a
    transfer, a recurring item, a loan payment or a contribution.

    Returns (pence a month, months of data used).
    """
    first_this_month = date(today.year, today.month, 1)
    start = _add_months(first_this_month, -months)
    claimed = {r[0] for r in db.query(FinanceRecurringEvent.transaction_id).filter(
        FinanceRecurringEvent.owner == owner, FinanceRecurringEvent.transaction_id.isnot(None))}
    claimed |= {r[0] for r in db.query(FinanceRepayment.transaction_id).filter(
        FinanceRepayment.owner == owner, FinanceRepayment.transaction_id.isnot(None))}
    claimed |= {r[0] for r in db.query(FinanceContribution.transaction_id).filter(
        FinanceContribution.owner == owner, FinanceContribution.transaction_id.isnot(None))}
    rows = db.query(FinanceTransaction.id, FinanceTransaction.date, FinanceTransaction.amount_pence).filter(
        FinanceTransaction.owner == owner, FinanceTransaction.direction == "out",
        FinanceTransaction.transfer_id.is_(None), FinanceTransaction.date >= start,
        FinanceTransaction.date < first_this_month).all()
    earliest = db.query(FinanceTransaction.date).filter(FinanceTransaction.owner == owner).order_by(
        FinanceTransaction.date).first()
    if earliest is None:
        return 0, 0
    # Only count months that started after the first recorded transaction.
    used = sum(1 for i in range(months)
               if _add_months(start, i) >= date(earliest[0].year, earliest[0].month, 1))
    if used == 0:
        return 0, 0
    total = sum(amount for tx_id, _day, amount in rows if tx_id not in claimed)
    return int(round(total / used)), used


def load_inputs(db: Session, owner, *, today: Optional[date] = None,
                assumptions: Optional[Assumptions] = None) -> Inputs:
    from src.finance import loans
    from src.finance.networth import latest_valuation
    from src.finance.service import account_balances

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    a = assumptions or get_assumptions(db, owner)
    warnings: list = []

    accounts = db.query(FinanceAccount).filter(FinanceAccount.owner == owner).order_by(
        FinanceAccount.sort_order, FinanceAccount.name).all()
    balances = account_balances(db, owner, [x.id for x in accounts], as_of=today)
    acc = [Account(x.id, x.name, x.account_type, balances.get(x.id, 0)) for x in accounts
           if not x.archived or balances.get(x.id, 0)]

    assets = []
    for x in db.query(FinanceAsset).filter(FinanceAsset.owner == owner).order_by(FinanceAsset.name):
        snap = latest_valuation(db, owner, "asset", x.id, today)
        value = snap.value_pence if snap else 0
        if x.archived and not value:
            continue
        assets.append(Asset(x.id, x.name, x.asset_type, value, snap.date if snap else None, x.subtype or ""))
        if snap is None:
            warnings.append(_warning("no_value", f"{x.name} has no value recorded, so it counts as £0.",
                                     "asset", x.id))
        elif (today - snap.date).days > STALE_VALUE_DAYS:
            warnings.append(_warning("stale_value", f"{x.name}'s value is from {snap.date.strftime('%d %b %Y')}, "
                                     "over a year ago. Update it for a better projection.", "asset", x.id))

    regular_rows = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner,
                                                     FinanceRecurring.archived.is_(False)).order_by(
        FinanceRecurring.name, FinanceRecurring.id).all()
    regulars = [Regular(r.id, r.name, r.account_id, r.amount_pence, r.direction, r.frequency, r.anchor_date,
                        r.end_date, r.to_account_id, r.liability_id, r.asset_id, getattr(r, "job_id", None))
                for r in regular_rows]
    paid_by_regular = {r.liability_id for r in regulars if r.liability_id}

    liabilities = []
    for x in db.query(FinanceLiability).filter(FinanceLiability.owner == owner).order_by(FinanceLiability.name):
        bal, _ = loans.balance(db, owner, x, today)
        if x.archived and not bal:
            continue
        s = loans.get_schedule(db, owner, x.id)
        terms = None
        if s is not None:
            terms = LoanTerms(s.apr_bp, loans.monthly_payment_for(s), s.payment_day, loans.term_end_date(s),
                              s.balloon_pence, s.start_date)
            if not terms.payment and bal > 0:
                warnings.append(_warning("no_payment", f"{x.name} has a schedule with no monthly payment or term, "
                                         "so its balance is held flat.", "liability", x.id))
        liabilities.append(Liability(x.id, x.name, x.liability_type, bal, terms, x.interest_bp,
                                     x.linked_asset_id, x.subtype or ""))
        if s is None and bal > 0 and x.id not in paid_by_regular and x.liability_type != "student_loan":
            warnings.append(_warning("flat_liability", f"{x.name} has no repayment schedule and no recurring "
                                     "payment linked, so its balance is held flat.", "liability", x.id))
    inputs = Inputs(today=today, accounts=acc, assets=assets, liabilities=liabilities, regulars=regulars,
                    warnings=warnings)
    inputs.spending_baseline, inputs.spending_months_used = spending_baseline(db, owner, today, a.spending_months)
    if inputs.spending_months_used < a.spending_months and a.spending_override is None:
        warnings.append(_warning("short_history", f"Only {inputs.spending_months_used} full month"
                                 f"{'s' if inputs.spending_months_used != 1 else ''} of spending recorded; the "
                                 "day-to-day spending estimate may be off. You can set it yourself.", None, None))
    if not any(r.direction == "in" and not r.to_account_id for r in regulars):
        warnings.append(_warning("no_income", "No recurring income is recorded, so the projection has no money "
                                 "coming in. Add your salary as a recurring item.", None, None))
    has_vehicle = any(x.type == "vehicle" for x in assets)
    for l in liabilities:
        car = l.subtype in ("pcp", "hp", "car", "car finance") or any(w in l.name.lower() for w in ("pcp", "car finance"))
        if car and not has_vehicle:
            warnings.append(_warning("no_vehicle", f"{l.name} looks like car finance but no vehicle asset is "
                                     "recorded, so net worth leaves out the car.", "liability", l.id))
        if l.schedule and l.schedule.payment:
            regular = [r for r in regulars if r.liability_id == l.id and not r.to_account_id]
            for r in regular:
                if abs(_monthly_equivalent(r) - l.schedule.payment) > 100:
                    warnings.append(_warning(
                        "payment_mismatch", f"{r.name} ({_pounds(r.amount_pence)}) differs from {l.name}'s "
                        f"scheduled payment ({_pounds(l.schedule.payment)}); the projection uses the schedule.",
                        "liability", l.id))
    return inputs


def _warning(kind: str, text: str, item_kind: Optional[str], item_id: Optional[str]) -> dict:
    return {"kind": kind, "text": text, "item_kind": item_kind, "item_id": item_id}


# ── Scenarios ──────────────────────────────────────────────────────────


def _pence_field(value, field: str) -> int:
    from src.finance.money import MoneyError, parse_pence

    try:
        pence = parse_pence(value)
    except MoneyError as exc:
        raise FinanceError(f"{field}: {exc}") from exc
    if pence <= 0:
        raise FinanceError(f"{field} must be more than zero")
    return pence


def _date_field(value, field: str, required=False) -> Optional[date]:
    if value in (None, ""):
        if required:
            raise FinanceError(f"{field} is required")
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 4 and text.isdigit():
        return date(int(text), 1, 1)
    if len(text) == 7 and text[4] == "-":
        return date(int(text[:4]), int(text[5:7]), 1)
    return parse_date(text, field=field)


def normalise_change(db: Session, owner: str, raw: dict) -> dict:
    """Validate one scenario change and return it with pence, ids and dates resolved."""
    if not isinstance(raw, dict):
        raise FinanceError("each change must be an object")
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in CHANGE_TYPES:
        raise FinanceError(f"change type must be one of: {', '.join(CHANGE_TYPES)}")
    out: dict = {"type": kind}
    if kind in ("extra_income", "reduce_spending"):
        out["amount"] = _pence_field(raw.get("amount"), "monthly amount")
        out["from"] = _date_field(raw.get("from"), "from")
        out["until"] = _date_field(raw.get("until"), "until")
    elif kind == "one_off":
        out["amount"] = _pence_field(raw.get("amount"), "amount")
        out["on"] = _date_field(raw.get("on"), "date", required=True)
        direction = str(raw.get("direction") or "out").lower()
        if direction not in ("in", "out"):
            raise FinanceError("direction must be in or out")
        out["direction"] = direction
    elif kind == "overpay":
        out["liability_id"] = _item(db, owner, "liability", raw.get("liability_id")).id
        out["amount"] = _pence_field(raw.get("amount"), "overpayment")
        out["monthly"] = bool(raw.get("monthly"))
        if out["monthly"]:
            out["from"] = _date_field(raw.get("from"), "from")
            out["until"] = _date_field(raw.get("until"), "until")
        else:
            out["on"] = _date_field(raw.get("on"), "date", required=True)
    elif kind == "sell":
        out["asset_id"] = _item(db, owner, "asset", raw.get("asset_id")).id
        out["on"] = _date_field(raw.get("on"), "sale date", required=True)
        out["price"] = _pence_field(raw["price"], "sale price") if raw.get("price") not in (None, "") else None
        lid = raw.get("clear_liability_id")
        out["clear_liability_id"] = _item(db, owner, "liability", lid).id if lid not in (None, "") else None
    elif kind == "balloon":
        out["liability_id"] = _item(db, owner, "liability", raw.get("liability_id")).id
        choice = str(raw.get("choice") or "pay").lower().replace(" ", "_").replace("-", "_")
        choice = {"handback": "hand_back", "return": "hand_back", "give_back": "hand_back"}.get(choice, choice)
        if choice not in BALLOON_CHOICES:
            raise FinanceError("balloon choice must be pay, hand back or refinance")
        out["choice"] = choice
        if choice == "refinance":
            try:
                out["months"] = int(raw.get("months") or 36)
            except (TypeError, ValueError) as exc:
                raise FinanceError("refinance months must be a whole number") from exc
            if not 1 <= out["months"] <= 120:
                raise FinanceError("refinance months must be between 1 and 120")
            out["rate_bp"] = parse_rate(raw.get("rate") or 0, field="refinance rate", low=0, high=10000)
        aid = raw.get("asset_id")
        out["asset_id"] = _item(db, owner, "asset", aid).id if aid not in (None, "") else None
    elif kind == "growth":
        out["rate_bp"] = parse_rate(raw.get("rate"), field="growth rate", low=-5000, high=5000)
        if raw.get("asset_id") not in (None, ""):
            out["asset_id"] = _item(db, owner, "asset", raw.get("asset_id")).id
        else:
            t = str(raw.get("asset_type") or "").lower()
            if t not in GROWTH_TYPES:
                raise FinanceError(f"growth needs an asset or an asset type ({', '.join(GROWTH_TYPES)})")
            out["asset_type"] = t
    elif kind == "salary":
        from src.finance.payroll_jobs import normalise_salary_change

        out.update(normalise_salary_change(db, owner, raw))
    if raw.get("label"):
        out["label"] = _clean_text(raw.get("label"), field="label", max_len=120)
    return out


def _item(db: Session, owner: str, kind: str, item_id):
    from src.finance.networth import get_item

    return get_item(db, owner, kind, item_id)


def change_to_json(c: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in c.items()}


def change_from_json(c: dict) -> dict:
    out = dict(c)
    for key in ("from", "until", "on"):
        if out.get(key):
            out[key] = date.fromisoformat(out[key])
    return out


def describe_change(c: dict, names: Optional[dict] = None) -> str:
    names = names or {}
    n = lambda i: names.get(i, "item")  # noqa: E731
    when = lambda d: d.strftime("%b %Y") if d else "now"  # noqa: E731
    kind = c["type"]
    if c.get("label"):
        return c["label"]
    if kind == "extra_income":
        return f"{_pounds(c['amount'])} a month more income from {when(c.get('from'))}"
    if kind == "reduce_spending":
        return f"{_pounds(c['amount'])} a month less spending from {when(c.get('from'))}"
    if kind == "one_off":
        return f"One-off {'income' if c['direction'] == 'in' else 'expense'} of {_pounds(c['amount'])} in {when(c['on'])}"
    if kind == "overpay":
        if c.get("monthly"):
            return f"Overpay {n(c['liability_id'])} by {_pounds(c['amount'])} a month from {when(c.get('from'))}"
        return f"Overpay {n(c['liability_id'])} by {_pounds(c['amount'])} in {when(c['on'])}"
    if kind == "sell":
        text = f"Sell {n(c['asset_id'])} in {when(c['on'])}"
        if c.get("price") is not None:
            text += f" for {_pounds(c['price'])}"
        if c.get("clear_liability_id"):
            text += f", clearing {n(c['clear_liability_id'])}"
        return text
    if kind == "balloon":
        if c["choice"] == "refinance":
            return f"Refinance {n(c['liability_id'])}'s balloon over {c['months']} months at {_percent(c['rate_bp'])}"
        return f"{'Pay' if c['choice'] == 'pay' else 'Hand the car back at'} {n(c['liability_id'])}'s balloon"
    if kind == "growth":
        target = n(c["asset_id"]) if c.get("asset_id") else c["asset_type"]
        return f"{target} growth at {_percent(c['rate_bp'])} a year"
    if kind == "salary":
        from src.finance.payroll_jobs import describe_salary_change

        return describe_salary_change(c, names)
    return kind


def scenario_to_dict(s: FinanceProjectionScenario, names: Optional[dict] = None) -> dict:
    changes = [change_from_json(c) for c in json.loads(s.changes_json or "[]")]
    return {"id": s.id, "name": s.name, "notes": s.notes or "",
            "changes": [change_to_json(c) for c in changes],
            "descriptions": [describe_change(c, names) for c in changes]}


def item_names(db: Session, owner: str) -> dict:
    names = {}
    for model in (FinanceAsset, FinanceLiability, FinanceAccount):
        names.update({row.id: row.name for row in db.query(model.id, model.name).filter(model.owner == owner)})
    try:
        from src.finance.models import FinanceJob

        names.update({row.id: row.employer for row in db.query(FinanceJob.id, FinanceJob.employer).filter(
            FinanceJob.owner == owner)})
    except ImportError:  # pragma: no cover - before Stage 2
        pass
    return names


def list_scenarios(db: Session, owner) -> list[dict]:
    owner = _require_owner(owner)
    names = item_names(db, owner)
    return [scenario_to_dict(s, names) for s in db.query(FinanceProjectionScenario).filter(
        FinanceProjectionScenario.owner == owner).order_by(FinanceProjectionScenario.name)]


def get_scenario(db: Session, owner, scenario_id) -> FinanceProjectionScenario:
    row = db.query(FinanceProjectionScenario).filter(FinanceProjectionScenario.owner == owner,
                                                     FinanceProjectionScenario.id == str(scenario_id or "")).first()
    if row is None:
        raise FinanceNotFound("scenario not found")
    return row


def find_scenario(db: Session, owner, name) -> FinanceProjectionScenario:
    """A saved scenario by id or loosely by name ("sell the car" finds "Sell car")."""
    from src.finance.matching import match_name

    owner = _require_owner(owner)
    rows = db.query(FinanceProjectionScenario).filter(FinanceProjectionScenario.owner == owner).order_by(
        FinanceProjectionScenario.name).all()
    text = str(name or "").strip()
    by_id = [r for r in rows if r.id == text]
    if by_id:
        return by_id[0]
    found = match_name(text, rows, name=lambda r: r.name)
    if found.ok:
        return found.item
    if found.status == "ambiguous":
        raise FinanceError(f"'{text}' matches several scenarios: {', '.join(found.options)}")
    raise FinanceNotFound(f"no saved scenario called '{text}'"
                          + (f"; saved: {', '.join(r.name for r in rows)}" if rows else ""))


def _changes(db: Session, owner: str, changes) -> list:
    if not isinstance(changes, list):
        raise FinanceError("changes must be a list")
    if len(changes) > 20:
        raise FinanceError("a scenario can have up to 20 changes")
    return [normalise_change(db, owner, c) for c in changes]


def save_scenario(db: Session, owner, *, name, changes, notes="", scenario_id=None) -> FinanceProjectionScenario:
    owner = _require_owner(owner)
    clean = _clean_text(name, field="name", max_len=80, required=True)
    normal = _changes(db, owner, changes)
    if not normal:
        raise FinanceError("a scenario needs at least one change")
    clash = db.query(FinanceProjectionScenario).filter(FinanceProjectionScenario.owner == owner).all()
    if any(s.name.lower() == clean.lower() and s.id != scenario_id for s in clash):
        raise FinanceError(f"there's already a scenario called '{clean}'")
    row = get_scenario(db, owner, scenario_id) if scenario_id else None
    if row is None:
        row = FinanceProjectionScenario(id=_new_id(), owner=owner)
        db.add(row)
    row.name = clean
    row.changes_json = json.dumps([change_to_json(c) for c in normal])
    row.notes = _clean_text(notes or "", field="notes", max_len=1000)
    db.flush()
    return row


def delete_scenario(db: Session, owner, scenario_id) -> None:
    owner = _require_owner(owner)
    db.delete(get_scenario(db, owner, scenario_id))
    db.flush()


# ── Public entry point ─────────────────────────────────────────────────


def projection(db: Session, owner, *, years=None, scenario_ids=(), changes=None, today: Optional[date] = None,
               assumptions: Optional[Assumptions] = None) -> dict:
    """The base projection plus each saved scenario (and/or ad-hoc *changes*) side by side."""
    owner = _require_owner(owner)
    a = assumptions or get_assumptions(db, owner)
    horizon = parse_years(years) if years not in (None, "") else a.horizon_years
    inputs = load_inputs(db, owner, today=today, assumptions=a)
    _attach_payroll(db, owner, inputs)
    base = run(inputs, a, months=horizon * 12)
    names = item_names(db, owner)
    out = {"years": horizon, "assumption_settings": assumptions_to_dict(a), "base": base, "scenarios": [],
           "names": names}
    ids = list(scenario_ids or [])
    if len(ids) > 3:
        raise FinanceError("compare up to three scenarios at a time")
    for sid in ids:
        row = get_scenario(db, owner, sid)
        ch = [change_from_json(c) for c in json.loads(row.changes_json or "[]")]
        out["scenarios"].append({"id": row.id, "name": row.name,
                                 "descriptions": [describe_change(c, names) for c in ch],
                                 **run(inputs, a, months=horizon * 12, changes=ch)})
    if changes:
        ch = _changes(db, owner, changes)
        out["scenarios"].append({"id": None, "name": "What if", "descriptions": [describe_change(c, names) for c in ch],
                                 **run(inputs, a, months=horizon * 12, changes=ch)})
    return out


def _attach_payroll(db: Session, owner: str, inputs: Inputs) -> None:
    """Stage 2 hook: salary from payroll jobs."""
    try:
        from src.finance.payroll_jobs import attach_payroll
    except ImportError:  # pragma: no cover - before Stage 2
        return
    attach_payroll(db, owner, inputs)


def value_at(result: dict, item_id: str, on: date) -> Optional[dict]:
    """An asset's value or liability's balance at the month end on/after *on* (for "in 2030" questions)."""
    for y in result["years"]:
        if y["end"] >= on.isoformat():
            for a in y["assets"]:
                if a["id"] == item_id:
                    return {"date": y["end"], "value_pence": a["value_pence"], "kind": "asset"}
            for l in y["liabilities"]:
                if l["id"] == item_id:
                    return {"date": y["end"], "value_pence": l["balance_pence"], "kind": "liability"}
    return None


__all__ = ["Account", "Asset", "Assumptions", "Inputs", "Liability", "LoanTerms", "ProjectionError", "Regular",
           "get_assumptions", "load_inputs", "projection", "run", "set_assumptions", "save_scenario",
           "list_scenarios", "delete_scenario", "find_scenario"]
