"""Loan schedules, payments, balances and payoff projections.

How a liability's balance is worked out (as of a date):

1. Start from the schedule's start date and starting balance, if there is a
   schedule. A dated balance snapshot (e.g. from a statement) replaces the
   balance from its date onwards, so drift can be corrected.
2. Walk payments in date order. Interest accrues at APR / 12 per month,
   pro-rated by days since the previous event (30.4375 days = one month),
   and is charged when the next payment lands. Each payment covers that
   interest first; the rest reduces the capital.
3. Liabilities without a schedule (student loans, informal debts) use the
   latest snapshot and any payments after it, with interest only if a rate
   is set.

Projections assume the schedule's fixed monthly payment (or the standard
repayment worked out from the starting balance, rate, term and any PCP
balloon) continues on the payment day each month.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from src.finance.models import (
    FinanceLiability,
    FinanceLoanSchedule,
    FinanceRepayment,
    FinanceTransaction,
    FinanceValuation,
)
from src.finance.money import MoneyError, parse_pence
from src.finance.service import (
    FinanceConflict,
    FinanceError,
    FinanceNotFound,
    _amount,
    _clean_text,
    _get_transaction,
    _new_id,
    _require_owner,
    _today_for_owner,
    parse_date,
)

DAYS_PER_MONTH = 30.4375
SCHEDULE_TYPES = ("mortgage", "loan", "bnpl", "other")  # not student loans
MISSED_GRACE_DAYS = 7
MATCH_WINDOW_DAYS = 10
MAX_PROJECTION_MONTHS = 600


# ── Helpers ─────────────────────────────────────────────────────────────


def _liability(db: Session, owner: str, liability_id) -> FinanceLiability:
    item = db.query(FinanceLiability).filter(FinanceLiability.owner == owner,
                                             FinanceLiability.id == str(liability_id or "")).first()
    if item is None:
        raise FinanceNotFound("liability not found")
    return item


def parse_rate_bp(value) -> int:
    """'4.5', '4.5%', 4.5 -> 450 basis points."""
    text = str(value if value is not None else "").strip().rstrip("%").strip()
    if not text:
        return 0
    try:
        rate = float(text)
    except ValueError as exc:
        raise FinanceError("interest rate must be a percentage such as 4.5") from exc
    if not 0 <= rate <= 100:
        raise FinanceError("interest rate must be between 0 and 100%")
    return int(round(rate * 100))


def _pence(value, *, field: str, allow_zero=True) -> int:
    try:
        pence = parse_pence(value)
    except MoneyError as exc:
        raise FinanceError(f"{field}: {exc}") from exc
    if pence < 0 or (pence == 0 and not allow_zero):
        raise FinanceError(f"{field} must be {'zero or more' if allow_zero else 'more than zero'}")
    return pence


def add_months(day: date, months: int, payment_day: Optional[int] = None) -> date:
    """Same day-of-month `months` later (clamped to the month's last day)."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    want = payment_day or day.day
    return date(year, month, min(want, calendar.monthrange(year, month)[1]))


def next_payment_date(after: date, payment_day: int) -> date:
    """First payment day strictly after `after`."""
    candidate = date(after.year, after.month, min(payment_day, calendar.monthrange(after.year, after.month)[1]))
    return candidate if candidate > after else add_months(candidate, 1, payment_day)


def standard_payment(balance: int, apr_bp: int, months: int, balloon: int = 0) -> int:
    """Monthly payment that clears `balance` (leaving `balloon`) over `months`."""
    if months <= 0:
        return balance
    r = apr_bp / 10000 / 12
    if r == 0:
        return int(round((balance - balloon) / months))
    factor = (1 + r) ** months
    return int(round((balance - balloon / factor) * r / (1 - 1 / factor)))


# ── Schedules ───────────────────────────────────────────────────────────


def schedule_to_dict(s: FinanceLoanSchedule) -> dict:
    return {
        "liability_id": s.liability_id, "start_date": s.start_date.isoformat(),
        "start_balance_pence": s.start_balance_pence, "apr_bp": s.apr_bp,
        "apr_percent": s.apr_bp / 100, "term_months": s.term_months,
        "monthly_payment_pence": s.monthly_payment_pence, "payment_day": s.payment_day,
        "balloon_pence": s.balloon_pence,
    }


def get_schedule(db: Session, owner: str, liability_id: str) -> Optional[FinanceLoanSchedule]:
    return db.query(FinanceLoanSchedule).filter(FinanceLoanSchedule.owner == owner,
                                                FinanceLoanSchedule.liability_id == liability_id).first()


def set_schedule(db: Session, owner, liability_id, *, start_date, start_balance, apr=0, term_months=None,
                 monthly_payment=None, payment_day=None, balloon=0) -> FinanceLoanSchedule:
    owner = _require_owner(owner)
    item = _liability(db, owner, liability_id)
    if item.liability_type not in SCHEDULE_TYPES:
        raise FinanceError("student loans are repaid through payroll, so they don't have a repayment "
                           "schedule; record balance snapshots and repayments instead")
    start = parse_date(start_date, field="start date")
    term = None
    if term_months not in (None, ""):
        try:
            term = int(term_months)
        except (TypeError, ValueError) as exc:
            raise FinanceError("term must be a whole number of months") from exc
        if not 1 <= term <= 600:
            raise FinanceError("term must be between 1 and 600 months")
    payment = None if monthly_payment in (None, "") else _pence(monthly_payment, field="monthly payment",
                                                                 allow_zero=False)
    day = start.day if payment_day in (None, "") else int(payment_day)
    if not 1 <= day <= 31:
        raise FinanceError("payment day must be between 1 and 31")
    balloon_pence = _pence(balloon or 0, field="final (balloon) payment")
    balance = _pence(start_balance, field="starting balance")
    if balloon_pence and not term:
        raise FinanceError("a final (balloon) payment needs a term in months")
    if balloon_pence > balance:
        raise FinanceError("the final payment cannot be more than the starting balance")
    schedule = get_schedule(db, owner, item.id)
    if schedule is None:
        schedule = FinanceLoanSchedule(liability_id=item.id, owner=owner)
        db.add(schedule)
    schedule.start_date, schedule.start_balance_pence = start, balance
    schedule.apr_bp, schedule.term_months = parse_rate_bp(apr), term
    schedule.monthly_payment_pence, schedule.payment_day, schedule.balloon_pence = payment, day, balloon_pence
    db.flush()
    return schedule


def delete_schedule(db: Session, owner, liability_id) -> None:
    owner = _require_owner(owner)
    item = _liability(db, owner, liability_id)
    schedule = get_schedule(db, owner, item.id)
    if schedule is not None:
        db.delete(schedule)
        db.flush()


def set_interest_rate(db: Session, owner, liability_id, rate) -> FinanceLiability:
    """Rate for a liability without a schedule (e.g. a student loan); None clears it."""
    owner = _require_owner(owner)
    item = _liability(db, owner, liability_id)
    item.interest_bp = None if rate in (None, "") else parse_rate_bp(rate)
    db.flush()
    return item


# ── Payments ────────────────────────────────────────────────────────────


def repayment_to_dict(r: FinanceRepayment) -> dict:
    return {"id": r.id, "liability_id": r.liability_id, "date": r.date.isoformat(),
            "amount_pence": r.amount_pence, "note": r.note or "", "transaction_id": r.transaction_id}


def add_repayment(db: Session, owner, liability_id, *, amount, on=None, note="") -> FinanceRepayment:
    """A payment that is not an account transaction (e.g. student loan via payroll)."""
    owner = _require_owner(owner)
    item = _liability(db, owner, liability_id)
    row = FinanceRepayment(id=_new_id(), owner=owner, liability_id=item.id,
                           date=parse_date(on) if on not in (None, "") else _today_for_owner(),
                           amount_pence=_amount(amount), note=_clean_text(note, field="note", max_len=200))
    db.add(row)
    db.flush()
    return row


def delete_repayment(db: Session, owner, repayment_id) -> None:
    owner = _require_owner(owner)
    row = db.query(FinanceRepayment).filter(FinanceRepayment.owner == owner,
                                            FinanceRepayment.id == str(repayment_id or "")).first()
    if row is None:
        raise FinanceNotFound("payment not found")
    if row.transaction_id:
        raise FinanceConflict("this payment comes from a transaction; unlink it from the transaction")
    db.delete(row)
    db.flush()


def link_transaction_payment(db: Session, owner, transaction_id, liability_id) -> Optional[FinanceRepayment]:
    """Count a money-out transaction as a payment towards a liability (None unlinks)."""
    owner = _require_owner(owner)
    tx = _get_transaction(db, owner, transaction_id)
    existing = db.query(FinanceRepayment).filter(FinanceRepayment.owner == owner,
                                                 FinanceRepayment.transaction_id == tx.id).first()
    if liability_id in (None, ""):
        if existing is not None:
            db.delete(existing)
            db.flush()
        return None
    if tx.direction != "out":
        raise FinanceError("only money going out can be a payment towards a debt")
    item = _liability(db, owner, liability_id)
    if existing is None:
        existing = FinanceRepayment(id=_new_id(), owner=owner, transaction_id=tx.id, liability_id=item.id,
                                    date=tx.date, amount_pence=tx.amount_pence)
        db.add(existing)
    existing.liability_id = item.id
    existing.date, existing.amount_pence = tx.date, tx.amount_pence
    db.flush()
    return existing


def sync_transaction_payment(db: Session, owner: str, tx: FinanceTransaction) -> None:
    """Keep a linked payment's date/amount in step after the transaction is edited."""
    row = db.query(FinanceRepayment).filter(FinanceRepayment.owner == owner,
                                            FinanceRepayment.transaction_id == tx.id).first()
    if row is None:
        return
    if tx.direction != "out":
        db.delete(row)
    else:
        row.date, row.amount_pence = tx.date, tx.amount_pence
    db.flush()


def payment_links(db: Session, owner: str, transaction_ids) -> dict:
    """Map transaction id -> liability id for linked payments."""
    ids = list(transaction_ids)
    if not ids:
        return {}
    rows = db.query(FinanceRepayment.transaction_id, FinanceRepayment.liability_id).filter(
        FinanceRepayment.owner == owner, FinanceRepayment.transaction_id.in_(ids)).all()
    return dict(rows)


# ── Balance walk ────────────────────────────────────────────────────────


@dataclass
class Walk:
    balance: int = 0
    last_date: Optional[date] = None
    payments: list = field(default_factory=list)  # dicts with the split
    started: bool = False


def _walk(db: Session, owner: str, item: FinanceLiability, schedule: Optional[FinanceLoanSchedule],
          as_of: Optional[date]) -> Walk:
    rate_bp = schedule.apr_bp if schedule else (item.interest_bp or 0)
    monthly_rate = rate_bp / 10000 / 12
    events = []  # (date, order, kind, payload); snapshots apply before same-day payments
    if schedule is not None:
        events.append((schedule.start_date, 0, "start", schedule.start_balance_pence))
    snaps = db.query(FinanceValuation).filter(FinanceValuation.owner == owner, FinanceValuation.item_kind == "liability",
                                              FinanceValuation.item_id == item.id).all()
    events += [(s.date, 1, "snapshot", s.value_pence) for s in snaps]
    pays = db.query(FinanceRepayment).filter(FinanceRepayment.owner == owner,
                                             FinanceRepayment.liability_id == item.id).all()
    events += [(p.date, 2, "payment", p) for p in pays]
    events.sort(key=lambda e: (e[0], e[1], getattr(e[3], "created_at", None) or datetime.min))

    walk = Walk()
    for day, _order, kind, payload in events:
        if as_of is not None and day > as_of:
            break
        if kind in ("start", "snapshot"):
            walk.balance, walk.last_date, walk.started = payload, day, True
            continue
        # Payment.
        interest = 0
        if walk.started and walk.balance > 0 and monthly_rate and walk.last_date is not None:
            months = max((day - walk.last_date).days, 0) / DAYS_PER_MONTH
            interest = int(round(walk.balance * monthly_rate * months))
        interest_part = min(interest, payload.amount_pence) if walk.started else 0
        capital = payload.amount_pence - interest_part
        if walk.started:
            walk.balance = max(0, walk.balance + interest - payload.amount_pence)
            walk.last_date = day
        walk.payments.append({
            **repayment_to_dict(payload), "interest_pence": interest_part, "capital_pence": capital,
            "balance_after_pence": walk.balance if walk.started else None,
        })
    return walk


def balance(db: Session, owner: str, item: FinanceLiability, as_of: Optional[date] = None) -> tuple[int, Optional[date]]:
    """Balance owed as of a date, and the date of the last event it reflects."""
    schedule = get_schedule(db, owner, item.id)
    walk = _walk(db, owner, item, schedule, as_of)
    return walk.balance, walk.last_date


# ── Projection and missed payments ──────────────────────────────────────


def monthly_payment_for(schedule: FinanceLoanSchedule) -> Optional[int]:
    if schedule.monthly_payment_pence:
        return schedule.monthly_payment_pence
    if schedule.term_months:
        return standard_payment(schedule.start_balance_pence, schedule.apr_bp, schedule.term_months,
                                schedule.balloon_pence)
    return None


def payment_step(balance: int, monthly_rate: float, payment: int, *, final: bool = False) -> tuple[int, int, int]:
    """One scheduled monthly payment: (interest charged, amount paid, balance after).

    Interest is a whole month at APR / 12. The last payment (by term end,
    which includes any balloon, or when the balance is smaller than the
    payment) clears everything. Shared by :func:`project` and the
    multi-year projection engine so both use the same maths.
    """
    interest = int(round(balance * monthly_rate))
    if final or balance + interest <= payment:
        return interest, balance + interest, 0
    return interest, payment, balance + interest - payment


def term_end_date(schedule: FinanceLoanSchedule) -> Optional[date]:
    if not schedule.term_months:
        return None
    return add_months(schedule.start_date, schedule.term_months, schedule.payment_day)


def project(schedule: FinanceLoanSchedule, current_balance: int, from_date: date) -> dict:
    """Remaining payments, payoff date and interest still to pay from `from_date`."""
    payment = monthly_payment_for(schedule)
    out = {"monthly_payment_pence": payment, "next_payment_date": None, "remaining_payments": 0,
           "payoff_date": None, "interest_remaining_pence": 0, "final_payment_pence": None,
           "pays_off": True}
    if current_balance <= 0:
        return out
    if not payment:
        out["pays_off"] = None  # unknown without a payment or term
        return out
    r = schedule.apr_bp / 10000 / 12
    term_end = term_end_date(schedule)
    due = next_payment_date(from_date, schedule.payment_day)
    out["next_payment_date"] = due.isoformat()
    bal = current_balance
    total_interest = 0
    for count in range(1, MAX_PROJECTION_MONTHS + 1):
        interest, paid, bal = payment_step(bal, r, payment, final=term_end is not None and due >= term_end)
        total_interest += interest
        if bal == 0:
            out.update(remaining_payments=count, payoff_date=due.isoformat(),
                       interest_remaining_pence=total_interest, final_payment_pence=paid)
            return out
        due = add_months(due, 1, schedule.payment_day)
    out.update(pays_off=False, interest_remaining_pence=None)
    return out


def missed_payments(schedule: FinanceLoanSchedule, payments: list, today: date, *, months_back: int = 12) -> list[str]:
    """Expected payment dates (most recent first, up to six) with no payment within a few days."""
    if not monthly_payment_for(schedule):
        return []
    earliest = max(schedule.start_date, add_months(today, -months_back))
    due = next_payment_date(schedule.start_date, schedule.payment_day)
    unused = sorted(date.fromisoformat(p["date"]) for p in payments)
    missed = []
    while due <= today - timedelta(days=MISSED_GRACE_DAYS):
        if schedule.term_months and due > add_months(schedule.start_date, schedule.term_months, schedule.payment_day):
            break
        hit = next((d for d in unused if abs((d - due).days) <= MATCH_WINDOW_DAYS), None)
        if hit is not None:
            unused.remove(hit)
        elif due >= earliest:
            missed.append(due.isoformat())
        due = add_months(due, 1, schedule.payment_day)
    return list(reversed(missed))[:6]  # most recent six


# ── Details for views and tools ─────────────────────────────────────────


def loan_details(db: Session, owner, liability_id, *, as_of: Optional[date] = None) -> dict:
    owner = _require_owner(owner)
    item = _liability(db, owner, liability_id)
    schedule = get_schedule(db, owner, item.id)
    today = as_of or _today_for_owner()
    walk = _walk(db, owner, item, schedule, today)
    data = {
        "liability_id": item.id, "name": item.name, "type": item.liability_type, "subtype": item.subtype or "",
        "balance_pence": walk.balance, "balance_date": walk.last_date.isoformat() if walk.last_date else None,
        "interest_rate_percent": (schedule.apr_bp if schedule else (item.interest_bp or 0)) / 100
        if (schedule or item.interest_bp) else None,
        "schedule": schedule_to_dict(schedule) if schedule else None,
        "payments": list(reversed(walk.payments)),
        "total_paid_pence": sum(p["amount_pence"] for p in walk.payments),
        "interest_paid_pence": sum(p["interest_pence"] for p in walk.payments),
        "projection": None, "missed_payments": [],
    }
    if schedule is not None:
        data["projection"] = project(schedule, walk.balance, today)
        if walk.balance > 0:
            data["missed_payments"] = missed_payments(schedule, walk.payments, today)
    return data


def has_loan_history(db: Session, owner: str, liability_id: str) -> bool:
    return (get_schedule(db, owner, liability_id) is not None
            or db.query(FinanceRepayment.id).filter(FinanceRepayment.owner == owner,
                                                    FinanceRepayment.liability_id == liability_id).first() is not None)
