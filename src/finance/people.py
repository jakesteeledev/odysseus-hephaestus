"""Per-person views of money moving in and out.

A transaction belongs to its account, so each owner of the account gets an
equal share of it (see ``members.split_pence``). Shares are taken from
per-account totals, so a household figure is always exactly the sum of
everyone's figures.

Transfers between accounts with the same owners change nobody's position.
A transfer between accounts with different owners (say, from a parent's
current account to a child's savings) moves money from one member to
another: it shows as "moved in" for one and "moved out" for the other, and
nets to zero for the household.
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Hashable, Iterable, Optional

from sqlalchemy.orm import Session

from src.finance.members import owners_of, share_of
from src.finance.models import FinanceTransaction


def account_owners(db: Session, owner: str) -> dict:
    """Map account id -> member ids that own it, in split order."""
    return owners_of(db, owner, "account")


def owned_accounts(owners: dict, person: str) -> list[str]:
    return [account_id for account_id, ids in owners.items() if person in ids]


def shared_totals(rows: Iterable, owners: dict, person: Optional[str]) -> dict:
    """Sum ``(key, account_id, total, count)`` rows into key -> [share, count].

    With a *person*, each per-account total is replaced by their share, and
    accounts they don't own are skipped.
    """
    out: dict = {}
    for key, account_id, total, count in rows:
        ids = owners.get(account_id, [])
        if person is not None and person not in ids:
            continue
        slot = out.setdefault(key, [0, 0])
        slot[0] += share_of(int(total or 0), ids, person)
        slot[1] += int(count or 0)
    return out


def transfer_pairs(db: Session, owner: str, *, start: Optional[date] = None, end: Optional[date] = None) -> list:
    """Complete transfers as ``(date, amount, from_account, to_account)``; *end* is exclusive."""
    query = db.query(FinanceTransaction.transfer_id, FinanceTransaction.account_id, FinanceTransaction.direction,
                     FinanceTransaction.amount_pence, FinanceTransaction.date).filter(
        FinanceTransaction.owner == owner, FinanceTransaction.transfer_id.isnot(None))
    if start is not None:
        query = query.filter(FinanceTransaction.date >= start)
    if end is not None:
        query = query.filter(FinanceTransaction.date < end)
    sides: dict = {}
    for transfer_id, account_id, direction, amount, day in query:
        sides.setdefault(transfer_id, {})[direction] = (account_id, int(amount), day)
    pairs = []
    for pair in sides.values():
        if "in" in pair and "out" in pair:
            pairs.append((pair["out"][2], pair["out"][1], pair["out"][0], pair["in"][0]))
    return pairs


def moved(pairs: Iterable, owners: dict, person: Optional[str], *,
          key: Callable[[date], Hashable] = lambda day: None, account_id: Optional[str] = None) -> dict:
    """Money moved to and from *person* by transfers, as key -> {"in", "out"}.

    Always zero for the household (None): transfers only move money around.
    """
    out: dict = {}
    if person is None:
        return out
    for day, amount, from_id, to_id in pairs:
        if account_id and account_id not in (from_id, to_id):
            continue
        effect = share_of(amount, owners.get(to_id, []), person) - share_of(amount, owners.get(from_id, []), person)
        if effect:
            slot = out.setdefault(key(day), {"in": 0, "out": 0})
            slot["in" if effect > 0 else "out"] += abs(effect)
    return out


def between_members(owners: dict, from_id: Optional[str], to_id: Optional[str]) -> bool:
    """True when a transfer's two accounts have different owners."""
    if not from_id or not to_id:
        return False
    return sorted(owners.get(from_id, [])) != sorted(owners.get(to_id, []))
