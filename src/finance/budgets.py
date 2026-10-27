"""Monthly budgets per expense category.

- A budget is a monthly limit for one category, effective from a month until
  changed; past months keep the limit they had. No rollover.
- Spent = money out in that category during the month, transfers excluded
  (the same rule as the v1 totals).
- Committed = recurring items still expected (not confirmed or skipped) in
  that category for the rest of the month. Only the current and future
  months have commitments.
- State: "over" when spent is above the limit; "warning" when spent plus
  committed reaches 80% of the limit; otherwise "ok".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.finance.models import FinanceBudget, FinanceCategory, FinanceTransaction
from src.finance.money import MoneyError, parse_pence
from src.finance.service import (
    FinanceError,
    _get_category,
    _new_id,
    _require_owner,
    _today_for_owner,
    month_range,
)

WARNING_SHARE = 0.8


def _month(year=None, month=None) -> date:
    today = _today_for_owner()
    year = int(year) if year not in (None, "") else today.year
    month = int(month) if month not in (None, "") else today.month
    start, _ = month_range(year, month)
    return start


def parse_month(value) -> date:
    """'2026-10', '2026-10-01' or a date -> the first day of that month."""
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    text = str(value or "").strip()
    try:
        year, month = int(text[:4]), int(text[5:7])
        return _month(year, month)
    except (ValueError, IndexError) as exc:
        raise FinanceError("month must look like 2026-10") from exc


def _limit(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        pence = parse_pence(value)
    except MoneyError as exc:
        raise FinanceError(f"budget: {exc}") from exc
    if pence < 0:
        raise FinanceError("a budget cannot be negative")
    return pence


def set_budget(db: Session, owner, category_id, limit, *, month=None) -> FinanceBudget:
    """Set (or with limit None, remove) a category's budget from `month` onwards."""
    owner = _require_owner(owner)
    category = _get_category(db, owner, category_id)
    if category.kind != "expense":
        raise FinanceError("budgets are for spending categories")
    start = parse_month(month) if month not in (None, "") else _month()
    row = db.query(FinanceBudget).filter(FinanceBudget.owner == owner, FinanceBudget.category_id == category.id,
                                         FinanceBudget.month == start).first()
    if row is None:
        row = FinanceBudget(id=_new_id(), owner=owner, category_id=category.id, month=start)
        db.add(row)
    row.limit_pence = _limit(limit)
    db.flush()
    return row


def limits_for_month(db: Session, owner: str, start: date) -> dict:
    """Map category id -> limit in pence for the month (removed budgets omitted)."""
    rows = db.query(FinanceBudget).filter(FinanceBudget.owner == owner, FinanceBudget.month <= start).order_by(
        FinanceBudget.category_id, FinanceBudget.month).all()
    latest: dict = {}
    for row in rows:
        latest[row.category_id] = row.limit_pence
    return {cid: limit for cid, limit in latest.items() if limit is not None}


def _spent_by_category(db: Session, owner: str, start: date, end: date) -> dict:
    rows = db.query(FinanceTransaction.category_id, func.sum(FinanceTransaction.amount_pence)).filter(
        FinanceTransaction.owner == owner, FinanceTransaction.direction == "out",
        FinanceTransaction.transfer_id.is_(None), FinanceTransaction.date >= start,
        FinanceTransaction.date < end).group_by(FinanceTransaction.category_id).all()
    return {cid: int(total or 0) for cid, total in rows}


def budget_status(db: Session, owner, *, year=None, month=None, today: Optional[date] = None) -> dict:
    owner = _require_owner(owner)
    start = _month(year, month)
    _, end = month_range(start.year, start.month)
    today = today or _today_for_owner()
    limits = limits_for_month(db, owner, start)
    spent = _spent_by_category(db, owner, start, end)
    committed: dict = {}
    if end > today:  # current or future month
        from src.finance.recurring import committed_out

        committed = committed_out(db, owner, start, end - timedelta(days=1), today=today)
    categories = {c.id: c for c in db.query(FinanceCategory).filter(FinanceCategory.owner == owner)}
    rows = []
    for cid, limit in limits.items():
        category = categories.get(cid)
        if category is None:
            continue
        used = spent.get(cid, 0)
        planned = committed.get(cid, 0)
        if used > limit:
            state = "over"
        elif (limit and used + planned >= WARNING_SHARE * limit) or (not limit and planned):
            state = "warning"
        else:
            state = "ok"
        rows.append({
            "category_id": cid, "name": category.name, "color": category.color or "",
            "limit_pence": limit, "spent_pence": used, "committed_pence": planned,
            "remaining_pence": limit - used, "left_after_committed_pence": limit - used - planned,
            "percent_spent": round(100 * used / limit) if limit else (100 if used else 0), "state": state,
        })
    rows.sort(key=lambda r: ({"over": 0, "warning": 1, "ok": 2}[r["state"]], r["name"].lower()))
    budgeted = set(limits)
    unbudgeted = sum(v for cid, v in spent.items() if cid not in budgeted)
    return {
        "month": start.isoformat()[:7], "start": start.isoformat(), "end": end.isoformat(),
        "is_past": end <= today, "budgets": rows,
        "total_limit_pence": sum(r["limit_pence"] for r in rows),
        "total_spent_pence": sum(r["spent_pence"] for r in rows),
        "total_committed_pence": sum(r["committed_pence"] for r in rows),
        "unbudgeted_spent_pence": unbudgeted,
    }
