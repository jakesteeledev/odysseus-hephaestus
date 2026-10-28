"""Data for the finance charts: net worth history and monthly totals.

All amounts are pence; months are "YYYY-MM". Transfers between the owner's
own accounts are excluded from income and spending, as everywhere else.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.finance.models import (
    FinanceAccount,
    FinanceContribution,
    FinanceLoanSchedule,
    FinanceRepayment,
    FinanceTransaction,
    FinanceValuation,
)
from src.finance.service import FinanceError, _require_owner, _today_for_owner

RANGES = {"6m": 6, "1y": 12, "all": None}
MAX_MONTHS = 120


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _months_back(today: date, count: int) -> list[tuple[int, int]]:
    index = today.year * 12 + today.month - 1
    return [divmod(i, 12) for i in range(index - count + 1, index + 1)]


def _earliest(db: Session, owner: str) -> Optional[date]:
    candidates = []
    for column, model in ((FinanceTransaction.date, FinanceTransaction), (FinanceValuation.date, FinanceValuation),
                          (FinanceContribution.date, FinanceContribution),
                          (FinanceRepayment.date, FinanceRepayment),
                          (FinanceLoanSchedule.start_date, FinanceLoanSchedule)):
        value = db.query(func.min(column)).filter(model.owner == owner).scalar()
        if value:
            candidates.append(value)
    created = db.query(func.min(FinanceAccount.created_at)).filter(FinanceAccount.owner == owner).scalar()
    if created:
        candidates.append(created.date() if hasattr(created, "date") else created)
    return min(candidates) if candidates else None


def _month_count(db: Session, owner: str, range_key: str, today: date) -> int:
    if range_key not in RANGES:
        raise FinanceError("range must be 6m, 1y or all")
    count = RANGES[range_key]
    if count is None:
        first = _earliest(db, owner) or today
        count = (today.year - first.year) * 12 + (today.month - first.month) + 1
    return max(1, min(count, MAX_MONTHS))


def net_worth_history(db: Session, owner, *, range_key: str = "1y", today: Optional[date] = None,
                      person: Optional[str] = None) -> dict:
    """Net worth, assets and liabilities at each month end (today for this month).

    With *person*, the figures are their share. For the household, each
    point also has ``by_member``: member id -> net worth, for one line each.
    """
    from src.finance.networth import net_worth

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    points = []
    for year, month0 in _months_back(today, _month_count(db, owner, range_key, today)):
        as_of = min(_month_end(year, month0 + 1), today)
        worth = net_worth(db, owner, as_of=as_of, person=person)
        point = {
            "month": f"{year}-{month0 + 1:02d}", "as_of": as_of.isoformat(),
            "net_worth_pence": worth["net_worth_pence"], "assets_pence": worth["assets_total_pence"],
            "liabilities_pence": worth["liabilities_total_pence"],
        }
        if person is None:
            point["by_member"] = {row["member"]["id"]: row["net_worth_pence"] for row in worth.get("by_member", [])}
        points.append(point)
    return {"range": range_key, "person": person, "points": points}


def monthly_totals(db: Session, owner, *, range_key: str = "1y", category_id: Optional[str] = None,
                   today: Optional[date] = None, person: Optional[str] = None) -> dict:
    """Money in and out per month (zero-filled), and optionally one category's spending.

    With *person*, amounts are their share, and ``moved_in_pence`` /
    ``moved_out_pence`` are transfers to and from other members.
    """
    from src.finance.people import account_owners, moved, shared_totals, transfer_pairs

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    months = _months_back(today, _month_count(db, owner, range_key, today))
    first = date(months[0][0], months[0][1] + 1, 1)
    owners = account_owners(db, owner)
    key = func.strftime("%Y-%m", FinanceTransaction.date)
    base = db.query(key, FinanceTransaction.direction, FinanceTransaction.account_id,
                    func.sum(FinanceTransaction.amount_pence), func.count(FinanceTransaction.id)).filter(
        FinanceTransaction.owner == owner, FinanceTransaction.transfer_id.is_(None),
        FinanceTransaction.date >= first, FinanceTransaction.date <= today)
    grouped = base.group_by(key, FinanceTransaction.direction, FinanceTransaction.account_id)
    totals: dict = {}
    for (month, direction), (total, _count) in shared_totals(
            (((m, d), a, t, n) for m, d, a, t, n in grouped), owners, person).items():
        totals.setdefault(month, {"in": 0, "out": 0})[direction] = total
    category_totals: dict = {}
    if category_id:
        rows = base.filter(FinanceTransaction.direction == "out", FinanceTransaction.category_id == category_id
                           ).group_by(key, FinanceTransaction.direction, FinanceTransaction.account_id)
        category_totals = {month: total for month, (total, _count) in shared_totals(
            ((m, a, t, n) for m, _d, a, t, n in rows), owners, person).items()}
    moves = moved(transfer_pairs(db, owner, start=first, end=today + timedelta(days=1)), owners, person,
                  key=lambda day: day.strftime("%Y-%m"))
    points = []
    for year, month0 in months:
        label = f"{year}-{month0 + 1:02d}"
        row = totals.get(label, {"in": 0, "out": 0})
        point = {"month": label, "in_pence": row["in"], "out_pence": row["out"], "net_pence": row["in"] - row["out"]}
        if person is not None:
            move = moves.get(label, {"in": 0, "out": 0})
            point["moved_in_pence"], point["moved_out_pence"] = move["in"], move["out"]
        if category_id:
            point["category_out_pence"] = category_totals.get(label, 0)
        points.append(point)
    return {"range": range_key, "category_id": category_id, "person": person, "points": points}
