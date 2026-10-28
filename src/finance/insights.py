"""Data for the finance charts: net worth history and monthly totals.

All amounts are pence; months are "YYYY-MM". Transfers between the owner's
own accounts are excluded from income and spending, as everywhere else.
"""

from __future__ import annotations

import calendar
from datetime import date
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


def net_worth_history(db: Session, owner, *, range_key: str = "1y", today: Optional[date] = None) -> dict:
    """Net worth, assets and liabilities at each month end (today for this month)."""
    from src.finance.networth import net_worth

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    points = []
    for year, month0 in _months_back(today, _month_count(db, owner, range_key, today)):
        as_of = min(_month_end(year, month0 + 1), today)
        worth = net_worth(db, owner, as_of=as_of)
        points.append({
            "month": f"{year}-{month0 + 1:02d}", "as_of": as_of.isoformat(),
            "net_worth_pence": worth["net_worth_pence"], "assets_pence": worth["assets_total_pence"],
            "liabilities_pence": worth["liabilities_total_pence"],
        })
    return {"range": range_key, "points": points}


def monthly_totals(db: Session, owner, *, range_key: str = "1y", category_id: Optional[str] = None,
                   today: Optional[date] = None) -> dict:
    """Money in and out per month (zero-filled), and optionally one category's spending."""
    owner = _require_owner(owner)
    today = today or _today_for_owner()
    months = _months_back(today, _month_count(db, owner, range_key, today))
    first = date(months[0][0], months[0][1] + 1, 1)
    key = func.strftime("%Y-%m", FinanceTransaction.date)
    base = db.query(key, FinanceTransaction.direction, func.sum(FinanceTransaction.amount_pence)).filter(
        FinanceTransaction.owner == owner, FinanceTransaction.transfer_id.is_(None),
        FinanceTransaction.date >= first, FinanceTransaction.date <= today)
    totals: dict = {}
    for month, direction, total in base.group_by(key, FinanceTransaction.direction):
        totals.setdefault(month, {"in": 0, "out": 0})[direction] = int(total or 0)
    category_totals: dict = {}
    if category_id:
        rows = db.query(key, func.sum(FinanceTransaction.amount_pence)).filter(
            FinanceTransaction.owner == owner, FinanceTransaction.transfer_id.is_(None),
            FinanceTransaction.direction == "out", FinanceTransaction.category_id == category_id,
            FinanceTransaction.date >= first, FinanceTransaction.date <= today).group_by(key)
        category_totals = {month: int(total or 0) for month, total in rows}
    points = []
    for year, month0 in months:
        label = f"{year}-{month0 + 1:02d}"
        row = totals.get(label, {"in": 0, "out": 0})
        point = {"month": label, "in_pence": row["in"], "out_pence": row["out"], "net_pence": row["in"] - row["out"]}
        if category_id:
            point["category_out_pence"] = category_totals.get(label, 0)
        points.append(point)
    return {"range": range_key, "category_id": category_id, "points": points}
