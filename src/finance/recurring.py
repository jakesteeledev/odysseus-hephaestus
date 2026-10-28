"""Recurring items: expected payments and income, confirmed by the user.

Nothing is posted automatically. Each due date is "expected" until the user
confirms it (creating a transaction, or matching one already recorded) or
skips it. Due dates follow the frequency from the first due date:

- weekly / every 4 weeks: +7 / +28 days;
- monthly / quarterly / yearly: +1 / +3 / +12 months on the first due
  date's day of month, clamped to shorter months (31 Jan -> 28 Feb -> 31 Mar).

A transaction matches an occurrence when it is on the same account and in
the same direction, its amount is within 10% (or £2), its date is within a
few days of the due date, and its description shares a word with the item's
match text (an exact amount on the right day also counts).
"""

from __future__ import annotations

import calendar
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from src.finance.models import (
    FREQUENCIES,
    FinanceAccount,
    FinanceRecurring,
    FinanceRecurringEvent,
    FinanceTransaction,
)
from src.finance.service import (
    MAX_DESCRIPTION_LEN,
    MAX_NAME_LEN,
    FinanceConflict,
    FinanceError,
    FinanceNotFound,
    _amount,
    _clean_text,
    _direction,
    _get_account,
    _get_category,
    _get_transaction,
    _new_id,
    _require_owner,
    _today_for_owner,
    account_balances,
    create_transaction,
    delete_transaction,
    parse_date,
)

FREQUENCY_LABELS = {"weekly": "Weekly", "four_weekly": "Every 4 weeks", "monthly": "Monthly",
                    "quarterly": "Quarterly", "yearly": "Yearly"}
FREQUENCY_DAYS = {"weekly": 7, "four_weekly": 28, "monthly": 30.44, "quarterly": 91.31, "yearly": 365.25}
MATCH_DAYS_BEFORE = 5
MATCH_DAYS_AFTER = 7
LOOKBACK_DAYS = 60  # unresolved occurrences older than this are no longer shown
_STOP_WORDS = {"the", "and", "ltd", "limited", "plc", "payment", "card", "direct", "debit", "dd", "so", "fpi",
               "fpo", "bgc", "bp", "pos", "www", "com", "co", "uk", "ref", "to", "from", "for"}


# ── Dates ───────────────────────────────────────────────────────────────


def _frequency(value) -> str:
    text = str(value or "monthly").strip().lower().replace(" ", "_").replace("-", "_")
    text = {"week": "weekly", "every_week": "weekly", "fortnightly_x2": "four_weekly", "4_weekly": "four_weekly",
            "every_4_weeks": "four_weekly", "four_weeks": "four_weekly", "month": "monthly",
            "every_month": "monthly", "quarter": "quarterly", "annual": "yearly", "annually": "yearly",
            "year": "yearly", "every_year": "yearly"}.get(text, text)
    if text not in FREQUENCIES:
        raise FinanceError("frequency must be weekly, every 4 weeks, monthly, quarterly or yearly")
    return text


def _shift_months(anchor: date, months: int) -> date:
    index = anchor.year * 12 + anchor.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def occurrence(anchor: date, frequency: str, n: int) -> date:
    """The n-th due date (n = 0 is the first)."""
    if frequency == "weekly":
        return anchor + timedelta(days=7 * n)
    if frequency == "four_weekly":
        return anchor + timedelta(days=28 * n)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[frequency]
    return _shift_months(anchor, months * n)


def due_dates(item: FinanceRecurring, start: date, end: date) -> list[date]:
    """Due dates of an item with start <= date <= end (and not after its end date)."""
    out = []
    last = min(end, item.end_date) if item.end_date else end
    if last < item.anchor_date:
        return out
    # Jump close to `start` without walking from the beginning every time.
    approx = max(0, int((start - item.anchor_date).days / FREQUENCY_DAYS[item.frequency]) - 2)
    n = approx
    while True:
        day = occurrence(item.anchor_date, item.frequency, n)
        if day > last:
            return out
        if day >= start:
            out.append(day)
        n += 1


# ── Items ───────────────────────────────────────────────────────────────


def recurring_to_dict(item: FinanceRecurring) -> dict:
    return {
        "id": item.id, "name": item.name, "account_id": item.account_id, "amount_pence": item.amount_pence,
        "direction": item.direction, "category_id": item.category_id, "frequency": item.frequency,
        "frequency_label": FREQUENCY_LABELS[item.frequency], "anchor_date": item.anchor_date.isoformat(),
        "end_date": item.end_date.isoformat() if item.end_date else None, "match_text": item.match_text or "",
        "liability_id": item.liability_id, "asset_id": item.asset_id, "archived": bool(item.archived),
    }


def get_recurring(db: Session, owner: str, recurring_id) -> FinanceRecurring:
    item = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner,
                                             FinanceRecurring.id == str(recurring_id or "")).first()
    if item is None:
        raise FinanceNotFound("recurring item not found")
    return item


def _links(db: Session, owner: str, direction: str, liability_id, asset_id) -> tuple[Optional[str], Optional[str]]:
    from src.finance import networth as nw

    liability = asset = None
    if liability_id not in (None, ""):
        if direction != "out":
            raise FinanceError("only money going out can pay towards a debt")
        liability = nw.get_item(db, owner, "liability", liability_id).id
    if asset_id not in (None, ""):
        if direction != "out":
            raise FinanceError("only money going out can be a contribution")
        asset = nw._contribution_asset(db, owner, asset_id).id
    return liability, asset


def create_recurring(db: Session, owner, *, name, account_id, amount, direction="out", frequency="monthly",
                     next_due=None, end_date=None, category_id=None, match_text="", liability_id=None,
                     asset_id=None) -> FinanceRecurring:
    owner = _require_owner(owner)
    direction = _direction(direction)
    item = FinanceRecurring(
        id=_new_id(), owner=owner,
        name=_clean_text(name, field="name", max_len=MAX_NAME_LEN, required=True),
        account_id=_get_account(db, owner, account_id).id, amount_pence=_amount(amount), direction=direction,
        category_id=_get_category(db, owner, category_id).id if category_id not in (None, "") else None,
        frequency=_frequency(frequency),
        anchor_date=parse_date(next_due, field="next due date") if next_due not in (None, "") else _today_for_owner(),
        end_date=parse_date(end_date, field="end date") if end_date not in (None, "") else None,
        match_text=_clean_text(match_text or name, field="match text", max_len=MAX_DESCRIPTION_LEN),
    )
    if item.end_date and item.end_date < item.anchor_date:
        raise FinanceError("end date must be after the first due date")
    item.liability_id, item.asset_id = _links(db, owner, direction, liability_id, asset_id)
    db.add(item)
    db.flush()
    return item


def update_recurring(db: Session, owner, recurring_id, **changes) -> FinanceRecurring:
    owner = _require_owner(owner)
    item = get_recurring(db, owner, recurring_id)
    if changes.get("name") is not None:
        item.name = _clean_text(changes["name"], field="name", max_len=MAX_NAME_LEN, required=True)
    if changes.get("account_id") is not None:
        item.account_id = _get_account(db, owner, changes["account_id"]).id
    if changes.get("amount") is not None:
        item.amount_pence = _amount(changes["amount"])
    if changes.get("direction") is not None:
        item.direction = _direction(changes["direction"])
    if "category_id" in changes:
        cid = changes["category_id"]
        item.category_id = _get_category(db, owner, cid).id if cid not in (None, "") else None
    if changes.get("frequency") is not None:
        item.frequency = _frequency(changes["frequency"])
    if changes.get("next_due") not in (None, ""):
        # Re-anchor on the new next due date; past decisions are kept.
        item.anchor_date = parse_date(changes["next_due"], field="next due date")
    if "end_date" in changes:
        item.end_date = parse_date(changes["end_date"], field="end date") if changes["end_date"] not in (None, "") else None
    if changes.get("match_text") is not None:
        item.match_text = _clean_text(changes["match_text"], field="match text", max_len=MAX_DESCRIPTION_LEN)
    if changes.get("archived") is not None:
        item.archived = bool(changes["archived"])
    if "liability_id" in changes or "asset_id" in changes or changes.get("direction") is not None:
        item.liability_id, item.asset_id = _links(
            db, owner, item.direction, changes.get("liability_id", item.liability_id),
            changes.get("asset_id", item.asset_id))
    if item.end_date and item.end_date < item.anchor_date:
        raise FinanceError("end date must be after the next due date")
    db.flush()
    return item


def delete_recurring(db: Session, owner, recurring_id) -> None:
    """Delete an item and its decisions; transactions it created or matched stay."""
    owner = _require_owner(owner)
    item = get_recurring(db, owner, recurring_id)
    db.query(FinanceRecurringEvent).filter(FinanceRecurringEvent.owner == owner,
                                           FinanceRecurringEvent.recurring_id == item.id).delete(synchronize_session=False)
    db.delete(item)
    db.flush()


def list_recurring(db: Session, owner, *, include_archived: bool = False) -> list[dict]:
    owner = _require_owner(owner)
    query = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner)
    if not include_archived:
        query = query.filter(FinanceRecurring.archived.is_(False))
    today = _today_for_owner()
    out = []
    for item in query.order_by(FinanceRecurring.name).all():
        row = recurring_to_dict(item)
        upcoming = next((d for d in due_dates(item, today, today + timedelta(days=400))
                         if _event(db, owner, item.id, d) is None), None)
        row["next_due"] = upcoming.isoformat() if upcoming else None
        out.append(row)
    return out


# ── Matching ────────────────────────────────────────────────────────────


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(w) >= 3 and w not in _STOP_WORDS}


def _amount_close(a: int, b: int) -> bool:
    return abs(a - b) <= max(200, round(0.10 * max(a, b)))


def _score(item: FinanceRecurring, tx: FinanceTransaction, due: date) -> Optional[float]:
    if tx.account_id != item.account_id or tx.direction != item.direction or tx.transfer_id:
        return None
    gap = (tx.date - due).days
    if gap < -MATCH_DAYS_BEFORE or gap > MATCH_DAYS_AFTER:
        return None
    if not _amount_close(tx.amount_pence, item.amount_pence):
        return None
    shared = _words(item.match_text or item.name) & _words(tx.description)
    exact = tx.amount_pence == item.amount_pence
    if not shared and not (exact and abs(gap) <= 2):
        return None
    return len(shared) * 2 + (2 if exact else 0) - abs(gap) * 0.2


def _event(db: Session, owner: str, recurring_id: str, due: date) -> Optional[FinanceRecurringEvent]:
    return db.query(FinanceRecurringEvent).filter(FinanceRecurringEvent.owner == owner,
                                                  FinanceRecurringEvent.recurring_id == recurring_id,
                                                  FinanceRecurringEvent.due_date == due).first()


def _claimed_transactions(db: Session, owner: str) -> set[str]:
    rows = db.query(FinanceRecurringEvent.transaction_id).filter(
        FinanceRecurringEvent.owner == owner, FinanceRecurringEvent.transaction_id.isnot(None)).all()
    return {r[0] for r in rows}


def match_candidates(db: Session, owner: str, item: FinanceRecurring, due: date,
                     claimed: Optional[set] = None, limit: int = 3) -> list[dict]:
    """Recorded transactions that look like this occurrence, best first."""
    from src.finance.service import transaction_to_dict

    claimed = _claimed_transactions(db, owner) if claimed is None else claimed
    rows = db.query(FinanceTransaction).filter(
        FinanceTransaction.owner == owner, FinanceTransaction.account_id == item.account_id,
        FinanceTransaction.date >= due - timedelta(days=MATCH_DAYS_BEFORE),
        FinanceTransaction.date <= due + timedelta(days=MATCH_DAYS_AFTER)).all()
    scored = []
    for tx in rows:
        if tx.id in claimed:
            continue
        score = _score(item, tx, due)
        if score is not None:
            scored.append((score, tx))
    scored.sort(key=lambda pair: -pair[0])
    return [transaction_to_dict(tx) for _, tx in scored[:limit]]


# ── Confirm, match, skip, undo ──────────────────────────────────────────


def _check_due(item: FinanceRecurring, due) -> date:
    day = parse_date(due, field="due date")
    if day not in due_dates(item, day, day):
        raise FinanceError(f"{item.name} is not due on {day.isoformat()}")
    return day


def _record(db: Session, owner: str, item: FinanceRecurring, due: date, status: str,
            transaction_id: Optional[str] = None, created: bool = False) -> FinanceRecurringEvent:
    if _event(db, owner, item.id, due) is not None:
        raise FinanceConflict(f"{item.name} on {due.isoformat()} is already confirmed or skipped")
    event = FinanceRecurringEvent(id=_new_id(), owner=owner, recurring_id=item.id, due_date=due, status=status,
                                  transaction_id=transaction_id, created_transaction=created)
    db.add(event)
    db.flush()
    return event


def _apply_links(db: Session, owner: str, item: FinanceRecurring, tx_id: str) -> None:
    if item.liability_id:
        from src.finance.loans import link_transaction_payment

        link_transaction_payment(db, owner, tx_id, item.liability_id)
    if item.asset_id:
        from src.finance.networth import link_transaction_contribution

        link_transaction_contribution(db, owner, tx_id, item.asset_id)


def confirm(db: Session, owner, recurring_id, due, *, on=None, amount=None) -> FinanceRecurringEvent:
    """Record the occurrence as a new transaction (amount/date can be adjusted)."""
    owner = _require_owner(owner)
    item = get_recurring(db, owner, recurring_id)
    day = _check_due(item, due)
    if _event(db, owner, item.id, day) is not None:
        raise FinanceConflict(f"{item.name} on {day.isoformat()} is already confirmed or skipped")
    tx = create_transaction(db, owner, account_id=item.account_id, date=on if on not in (None, "") else day,
                            amount=amount if amount not in (None, "") else Decimal(item.amount_pence) / 100,
                            direction=item.direction, category_id=item.category_id, description=item.name)
    _apply_links(db, owner, item, tx.id)
    return _record(db, owner, item, day, "confirmed", tx.id, created=True)


def match(db: Session, owner, recurring_id, due, transaction_id) -> FinanceRecurringEvent:
    """Record that an existing transaction is this occurrence."""
    owner = _require_owner(owner)
    item = get_recurring(db, owner, recurring_id)
    day = _check_due(item, due)
    tx = _get_transaction(db, owner, transaction_id)
    if tx.id in _claimed_transactions(db, owner):
        raise FinanceConflict("that transaction is already matched to a recurring item")
    if tx.account_id != item.account_id or tx.direction != item.direction:
        raise FinanceError("the transaction must be on the same account and in the same direction")
    _apply_links(db, owner, item, tx.id)
    return _record(db, owner, item, day, "confirmed", tx.id)


def skip(db: Session, owner, recurring_id, due) -> FinanceRecurringEvent:
    owner = _require_owner(owner)
    item = get_recurring(db, owner, recurring_id)
    return _record(db, owner, item, _check_due(item, due), "skipped")


def undo(db: Session, owner, recurring_id, due) -> None:
    """Make an occurrence expected again. A transaction made by confirm is deleted."""
    owner = _require_owner(owner)
    item = get_recurring(db, owner, recurring_id)
    day = parse_date(due, field="due date")
    event = _event(db, owner, item.id, day)
    if event is None:
        raise FinanceNotFound("nothing to undo for that date")
    tx_id, created = event.transaction_id, event.created_transaction
    db.delete(event)
    db.flush()
    if created and tx_id:
        delete_transaction(db, owner, tx_id)


def make_recurring_from_transaction(db: Session, owner, transaction_id, *, frequency="monthly",
                                    name=None) -> FinanceRecurring:
    """Create an item from a transaction, with that transaction as its first occurrence."""
    owner = _require_owner(owner)
    tx = _get_transaction(db, owner, transaction_id)
    if tx.transfer_id:
        raise FinanceError("transfers can't be made recurring yet")
    from src.finance.loans import payment_links
    from src.finance.networth import contribution_links

    item = create_recurring(
        db, owner, name=name or tx.description or "Recurring payment", account_id=tx.account_id,
        amount=Decimal(tx.amount_pence) / 100, direction=tx.direction, frequency=frequency, next_due=tx.date,
        category_id=tx.category_id, match_text=tx.description,
        liability_id=payment_links(db, owner, [tx.id]).get(tx.id),
        asset_id=contribution_links(db, owner, [tx.id]).get(tx.id))
    if tx.id not in _claimed_transactions(db, owner):
        _record(db, owner, item, tx.date, "confirmed", tx.id)
    return item


# ── Upcoming and projections ────────────────────────────────────────────


@dataclass
class Occurrence:
    item: FinanceRecurring
    due: date
    status: str  # expected | overdue | confirmed | skipped
    transaction_id: Optional[str] = None


def occurrences(db: Session, owner: str, start: date, end: date, *, today: Optional[date] = None,
                items: Optional[Iterable[FinanceRecurring]] = None) -> list[Occurrence]:
    today = today or _today_for_owner()
    if items is None:
        items = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner,
                                                  FinanceRecurring.archived.is_(False)).all()
    events = {(e.recurring_id, e.due_date): e for e in db.query(FinanceRecurringEvent).filter(
        FinanceRecurringEvent.owner == owner, FinanceRecurringEvent.due_date >= start,
        FinanceRecurringEvent.due_date <= end)}
    out = []
    for item in items:
        for due in due_dates(item, start, end):
            event = events.get((item.id, due))
            if event is not None:
                out.append(Occurrence(item, due, event.status, event.transaction_id))
            else:
                out.append(Occurrence(item, due, "overdue" if due < today else "expected"))
    out.sort(key=lambda o: (o.due, o.item.name.lower()))
    return out


def _signed(item: FinanceRecurring) -> int:
    return item.amount_pence if item.direction == "in" else -item.amount_pence


def upcoming(db: Session, owner, *, days: int = 30, today: Optional[date] = None,
             person: Optional[str] = None) -> dict:
    """Overdue and next-N-days occurrences with match suggestions, plus
    projected end-of-month balances per account (expected items only).

    With *person*, only items and accounts they own are included, and each
    carries ``share_pence``: their share of the amount or balance.
    """
    from src.finance.members import share_of
    from src.finance.people import account_owners

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    horizon = today + timedelta(days=max(1, min(int(days), 366)))
    month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    items = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner,
                                              FinanceRecurring.archived.is_(False)).all()
    owners = account_owners(db, owner)
    if person is not None:
        items = [i for i in items if person in owners.get(i.account_id, [])]
    occ = occurrences(db, owner, today - timedelta(days=LOOKBACK_DAYS), max(horizon, month_end), today=today,
                      items=items)
    claimed = _claimed_transactions(db, owner)
    rows = []
    for o in occ:
        if o.due > horizon or o.status in ("confirmed", "skipped") and o.due < today:
            continue
        row = {**recurring_to_dict(o.item), "recurring_id": o.item.id, "due_date": o.due.isoformat(),
               "status": o.status, "transaction_id": o.transaction_id, "matches": [],
               "owner_ids": owners.get(o.item.account_id, []),
               "share_pence": share_of(o.item.amount_pence, owners.get(o.item.account_id, []), person)}
        if o.status in ("expected", "overdue") and o.due <= today + timedelta(days=MATCH_DAYS_BEFORE):
            row["matches"] = match_candidates(db, owner, o.item, o.due, claimed)
        rows.append(row)

    accounts = db.query(FinanceAccount).filter(FinanceAccount.owner == owner,
                                               FinanceAccount.archived.is_(False)).order_by(
        FinanceAccount.sort_order, FinanceAccount.name).all()
    balances = account_balances(db, owner, [a.id for a in accounts])
    projected = []
    for account in accounts:
        if person is not None and person not in owners.get(account.id, []):
            continue
        pending = sum(_signed(o.item) for o in occ
                      if o.item.account_id == account.id and o.status in ("expected", "overdue") and o.due <= month_end)
        projected.append({"account_id": account.id, "name": account.name,
                          "balance_pence": balances.get(account.id, 0),
                          "expected_pence": pending,
                          "projected_pence": balances.get(account.id, 0) + pending,
                          "owner_ids": owners.get(account.id, []),
                          "projected_share_pence": share_of(balances.get(account.id, 0) + pending,
                                                            owners.get(account.id, []), person)})
    return {"today": today.isoformat(), "until": horizon.isoformat(), "month_end": month_end.isoformat(),
            "items": rows, "projected_balances": projected, "person": person}


def committed_out(db: Session, owner: str, start: date, end: date, *, today: Optional[date] = None) -> dict:
    """Expected (unconfirmed) money out per category in a period, for budgets."""
    totals: dict = defaultdict(int)
    for o in occurrences(db, owner, start, end, today=today):
        if o.item.direction == "out" and o.status in ("expected", "overdue"):
            totals[o.item.category_id] += o.item.amount_pence
    return dict(totals)


# ── Suggestions ─────────────────────────────────────────────────────────


def _label(text: str) -> str:
    return " ".join(sorted(_words(text))) or str(text or "").strip().lower()


def suggestions(db: Session, owner, *, today: Optional[date] = None, months: int = 6) -> list[dict]:
    """Likely recurring items from repeated transactions not yet covered by one."""
    from src.finance.service import transaction_to_dict

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    rows = db.query(FinanceTransaction).filter(
        FinanceTransaction.owner == owner, FinanceTransaction.transfer_id.is_(None),
        FinanceTransaction.date >= today - timedelta(days=int(months * 30.5) + 7)).order_by(FinanceTransaction.date).all()
    existing = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner).all()
    groups: dict = defaultdict(list)
    for tx in rows:
        key = (tx.account_id, tx.direction, _label(tx.description))
        if key[2]:
            groups[key].append(tx)
    found = []
    for (account_id, direction, _key), txs in groups.items():
        if len(txs) < 3:
            continue
        amounts = [t.amount_pence for t in txs]
        if max(amounts) > min(amounts) * 1.15:
            continue
        gaps = [(b.date - a.date).days for a, b in zip(txs, txs[1:])]
        typical = statistics.median(gaps)
        frequency = next((f for f, days in (("weekly", 7), ("four_weekly", 28), ("monthly", 30.44),
                                            ("quarterly", 91.31), ("yearly", 365.25))
                          if abs(typical - days) <= max(2, days * 0.1)), None)
        if frequency is None:
            continue
        # Every gap must fit the pattern, not just the typical one.
        interval = FREQUENCY_DAYS[frequency]
        if any(abs(gap - interval) > max(3, interval * 0.15) for gap in gaps):
            continue
        latest = txs[-1]
        if any(r.account_id == account_id and r.direction == direction
               and (_words(r.match_text or r.name) & _words(latest.description)) for r in existing):
            continue
        next_due = occurrence(latest.date, frequency, 1)
        while next_due < today - timedelta(days=MATCH_DAYS_AFTER):
            next_due = occurrence(next_due, frequency, 1)
        found.append({
            "name": latest.description, "account_id": account_id, "direction": direction,
            "amount_pence": latest.amount_pence, "frequency": frequency,
            "frequency_label": FREQUENCY_LABELS[frequency], "category_id": latest.category_id,
            "next_due": next_due.isoformat(), "count": len(txs),
            "latest_transaction": transaction_to_dict(latest),
        })
    found.sort(key=lambda s: (-s["count"], s["name"].lower()))
    return found[:20]
