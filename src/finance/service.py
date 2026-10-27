"""Finance tracker business logic.

Routes, agent tools and the codex API all go through these functions, so
owner scoping, validation and the balance/transfer rules live in one place.
Functions take an open SQLAlchemy session and an owner string; they flush but
never commit, so the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from src.finance.models import (
    ACCOUNT_TYPES,
    CATEGORY_COLORS,
    CATEGORY_KINDS,
    DIRECTIONS,
    FinanceAccount,
    FinanceCategory,
    FinanceImportBatch,
    FinanceRule,
    FinanceTransaction,
)
from src.finance.money import MoneyError, parse_pence, parse_positive_pence

UNCATEGORISED = "Uncategorised"

# Seeded once per owner, the first time finance is used.
DEFAULT_CATEGORIES = (
    ("Salary", "income", "green"),
    ("Other income", "income", "blue"),
    ("Rent/Mortgage", "expense", "red"),
    ("Bills", "expense", "orange"),
    ("Groceries", "expense", "green"),
    ("Eating out", "expense", "orange"),
    ("Transport", "expense", "blue"),
    ("Subscriptions", "expense", "purple"),
    ("Shopping", "expense", "yellow"),
    ("Health", "expense", "red"),
    ("Entertainment", "expense", "purple"),
)

MAX_NAME_LEN = 80
MAX_DESCRIPTION_LEN = 200
MAX_NOTES_LEN = 2000


# ── Errors ──────────────────────────────────────────────────────────────


class FinanceError(ValueError):
    """Invalid input; routes map this to 400."""


class FinanceNotFound(FinanceError):
    """Unknown id for this owner; routes map this to 404."""


class FinanceConflict(FinanceError):
    """The action breaks a rule (e.g. deleting a used account); routes map this to 409."""


# ── Small helpers ───────────────────────────────────────────────────────


def _new_id() -> str:
    return uuid.uuid4().hex


def _require_owner(owner) -> str:
    owner = str(owner or "").strip()
    if not owner:
        raise FinanceError("finance needs a signed-in owner")
    return owner


def _clean_text(value, *, field: str, max_len: int, required: bool = False) -> str:
    text = " ".join(str(value or "").split()) if field != "notes" else str(value or "").strip()
    if required and not text:
        raise FinanceError(f"{field} is required")
    if len(text) > max_len:
        raise FinanceError(f"{field} must be at most {max_len} characters")
    return text


def parse_date(value, *, field: str = "date") -> date:
    """Accept a ``date``/``datetime`` or an ISO ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise FinanceError(f"{field} must be a date like 2026-10-27") from exc


def _amount(value) -> int:
    try:
        return parse_positive_pence(value)
    except MoneyError as exc:
        raise FinanceError(str(exc)) from exc


def _direction(value) -> str:
    direction = str(value or "").strip().lower()
    if direction not in DIRECTIONS:
        raise FinanceError("direction must be 'in' or 'out'")
    return direction


# ── Serialisation ───────────────────────────────────────────────────────


def account_to_dict(account: FinanceAccount, balance_pence: Optional[int] = None) -> dict:
    data = {
        "id": account.id,
        "name": account.name,
        "account_type": account.account_type,
        "opening_balance_pence": account.opening_balance_pence,
        "archived": bool(account.archived),
        "sort_order": account.sort_order,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
    }
    if balance_pence is not None:
        data["balance_pence"] = balance_pence
    return data


def category_to_dict(category: FinanceCategory) -> dict:
    return {
        "id": category.id,
        "name": category.name,
        "kind": category.kind,
        "color": category.color or "",
        "is_system": bool(category.is_system),
    }


def transaction_to_dict(tx: FinanceTransaction, *, transfer_account_id: Optional[str] = None) -> dict:
    data = {
        "id": tx.id,
        "account_id": tx.account_id,
        "date": tx.date.isoformat(),
        "amount_pence": tx.amount_pence,
        "direction": tx.direction,
        "category_id": tx.category_id,
        "description": tx.description or "",
        "notes": tx.notes or "",
        "transfer_id": tx.transfer_id,
        "import_batch_id": tx.import_batch_id,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
        "updated_at": tx.updated_at.isoformat() if tx.updated_at else None,
    }
    if tx.transfer_id:
        data["transfer_account_id"] = transfer_account_id
    return data


# ── Accounts ────────────────────────────────────────────────────────────


def _get_account(db: Session, owner: str, account_id) -> FinanceAccount:
    account = (
        db.query(FinanceAccount)
        .filter(FinanceAccount.owner == owner, FinanceAccount.id == str(account_id or ""))
        .first()
    )
    if account is None:
        raise FinanceNotFound("account not found")
    return account


def _account_type(value) -> str:
    account_type = str(value or "current").strip().lower().replace(" ", "_").replace("-", "_")
    if account_type == "credit":
        account_type = "credit_card"
    if account_type not in ACCOUNT_TYPES:
        raise FinanceError("account type must be current, savings, credit_card or cash")
    return account_type


def create_account(db: Session, owner, *, name, account_type="current", opening_balance="0") -> FinanceAccount:
    owner = _require_owner(owner)
    try:
        opening = parse_pence(opening_balance if opening_balance not in (None, "") else "0")
    except MoneyError as exc:
        raise FinanceError(f"opening balance: {exc}") from exc
    next_order = (
        db.query(func.coalesce(func.max(FinanceAccount.sort_order), -1))
        .filter(FinanceAccount.owner == owner)
        .scalar()
    )
    account = FinanceAccount(
        id=_new_id(),
        owner=owner,
        name=_clean_text(name, field="name", max_len=MAX_NAME_LEN, required=True),
        account_type=_account_type(account_type),
        opening_balance_pence=opening,
        archived=False,
        sort_order=int(next_order) + 1,
    )
    db.add(account)
    db.flush()
    return account


def update_account(db: Session, owner, account_id, **changes) -> FinanceAccount:
    owner = _require_owner(owner)
    account = _get_account(db, owner, account_id)
    if "name" in changes and changes["name"] is not None:
        account.name = _clean_text(changes["name"], field="name", max_len=MAX_NAME_LEN, required=True)
    if "account_type" in changes and changes["account_type"] is not None:
        account.account_type = _account_type(changes["account_type"])
    if "opening_balance" in changes and changes["opening_balance"] is not None:
        try:
            account.opening_balance_pence = parse_pence(changes["opening_balance"])
        except MoneyError as exc:
            raise FinanceError(f"opening balance: {exc}") from exc
    if "archived" in changes and changes["archived"] is not None:
        account.archived = bool(changes["archived"])
    if "sort_order" in changes and changes["sort_order"] is not None:
        account.sort_order = int(changes["sort_order"])
    db.flush()
    return account


def delete_account(db: Session, owner, account_id) -> None:
    """Delete an account. Only empty accounts can be deleted; others must be archived."""
    owner = _require_owner(owner)
    account = _get_account(db, owner, account_id)
    used = (
        db.query(FinanceTransaction.id)
        .filter(FinanceTransaction.owner == owner, FinanceTransaction.account_id == account.id)
        .first()
    )
    if used is not None:
        raise FinanceConflict("this account has transactions; archive it instead")
    db.query(FinanceImportBatch).filter(
        FinanceImportBatch.owner == owner, FinanceImportBatch.account_id == account.id
    ).delete(synchronize_session=False)
    db.delete(account)
    db.flush()


def account_balances(db: Session, owner, account_ids: Optional[Iterable[str]] = None) -> dict:
    """Map account id -> current balance in pence (opening balance + transactions).

    Transfers count here: they move money between accounts.
    """
    owner = _require_owner(owner)
    signed = case((FinanceTransaction.direction == "in", FinanceTransaction.amount_pence),
                  else_=-FinanceTransaction.amount_pence)
    query = db.query(FinanceAccount.id, FinanceAccount.opening_balance_pence,
                     func.coalesce(func.sum(signed), 0)).outerjoin(
        FinanceTransaction,
        and_(FinanceTransaction.account_id == FinanceAccount.id,
             FinanceTransaction.owner == owner),
    ).filter(FinanceAccount.owner == owner).group_by(FinanceAccount.id)
    if account_ids is not None:
        query = query.filter(FinanceAccount.id.in_(list(account_ids)))
    return {account_id: int(opening) + int(moved) for account_id, opening, moved in query.all()}


def list_accounts(db: Session, owner, *, include_archived: bool = False) -> list[dict]:
    owner = _require_owner(owner)
    query = db.query(FinanceAccount).filter(FinanceAccount.owner == owner)
    if not include_archived:
        query = query.filter(FinanceAccount.archived.is_(False))
    accounts = query.order_by(FinanceAccount.sort_order, FinanceAccount.name).all()
    balances = account_balances(db, owner, [a.id for a in accounts])
    return [account_to_dict(a, balances.get(a.id, a.opening_balance_pence)) for a in accounts]


def accounts_overview(db: Session, owner) -> dict:
    """Active accounts with balances, plus their total."""
    accounts = list_accounts(db, owner)
    return {"accounts": accounts, "total_pence": sum(a["balance_pence"] for a in accounts)}


# ── Categories ──────────────────────────────────────────────────────────


def ensure_default_categories(db: Session, owner) -> None:
    """Seed the default categories the first time an owner uses finance, and
    make sure the protected Uncategorised category always exists."""
    owner = _require_owner(owner)
    existing = db.query(FinanceCategory).filter(FinanceCategory.owner == owner).all()
    if not existing:
        for name, kind, color in DEFAULT_CATEGORIES:
            db.add(FinanceCategory(id=_new_id(), owner=owner, name=name, kind=kind, color=color))
    if not any(c.is_system for c in existing):
        db.add(FinanceCategory(id=_new_id(), owner=owner, name=UNCATEGORISED, kind="expense",
                               color="", is_system=True))
    db.flush()


def uncategorised(db: Session, owner) -> FinanceCategory:
    owner = _require_owner(owner)
    category = (
        db.query(FinanceCategory)
        .filter(FinanceCategory.owner == owner, FinanceCategory.is_system.is_(True))
        .first()
    )
    if category is None:
        ensure_default_categories(db, owner)
        category = (
            db.query(FinanceCategory)
            .filter(FinanceCategory.owner == owner, FinanceCategory.is_system.is_(True))
            .first()
        )
    return category


def _get_category(db: Session, owner: str, category_id) -> FinanceCategory:
    category = (
        db.query(FinanceCategory)
        .filter(FinanceCategory.owner == owner, FinanceCategory.id == str(category_id or ""))
        .first()
    )
    if category is None:
        raise FinanceNotFound("category not found")
    return category


def _category_kind(value) -> str:
    kind = str(value or "expense").strip().lower()
    if kind not in CATEGORY_KINDS:
        raise FinanceError("category kind must be 'income' or 'expense'")
    return kind


def _category_color(value) -> str:
    color = str(value or "").strip().lower()
    if color not in CATEGORY_COLORS:
        raise FinanceError("colour must be one of: " + ", ".join(c for c in CATEGORY_COLORS if c))
    return color


def _check_category_name_free(db: Session, owner: str, name: str, exclude_id: Optional[str] = None) -> None:
    query = db.query(FinanceCategory.id).filter(
        FinanceCategory.owner == owner, func.lower(FinanceCategory.name) == name.lower()
    )
    if exclude_id:
        query = query.filter(FinanceCategory.id != exclude_id)
    if query.first() is not None:
        raise FinanceConflict(f"a category called {name!r} already exists")


def list_categories(db: Session, owner) -> list[dict]:
    owner = _require_owner(owner)
    ensure_default_categories(db, owner)
    categories = (
        db.query(FinanceCategory)
        .filter(FinanceCategory.owner == owner)
        .order_by(FinanceCategory.is_system, FinanceCategory.kind.desc(), FinanceCategory.name)
        .all()
    )
    return [category_to_dict(c) for c in categories]


def create_category(db: Session, owner, *, name, kind="expense", color="") -> FinanceCategory:
    owner = _require_owner(owner)
    ensure_default_categories(db, owner)
    name = _clean_text(name, field="name", max_len=MAX_NAME_LEN, required=True)
    _check_category_name_free(db, owner, name)
    category = FinanceCategory(id=_new_id(), owner=owner, name=name, kind=_category_kind(kind),
                               color=_category_color(color))
    db.add(category)
    db.flush()
    return category


def update_category(db: Session, owner, category_id, **changes) -> FinanceCategory:
    owner = _require_owner(owner)
    category = _get_category(db, owner, category_id)
    if changes.get("name") is not None:
        name = _clean_text(changes["name"], field="name", max_len=MAX_NAME_LEN, required=True)
        _check_category_name_free(db, owner, name, exclude_id=category.id)
        category.name = name
    if changes.get("kind") is not None:
        category.kind = _category_kind(changes["kind"])
    if changes.get("color") is not None:
        category.color = _category_color(changes["color"])
    db.flush()
    return category


def category_usage(db: Session, owner, category_id) -> int:
    owner = _require_owner(owner)
    return (
        db.query(func.count(FinanceTransaction.id))
        .filter(FinanceTransaction.owner == owner, FinanceTransaction.category_id == str(category_id))
        .scalar()
        or 0
    )


def delete_category(db: Session, owner, category_id) -> int:
    """Delete a category, moving its transactions and rules to Uncategorised.

    Returns how many transactions were reassigned.
    """
    owner = _require_owner(owner)
    category = _get_category(db, owner, category_id)
    if category.is_system:
        raise FinanceConflict("Uncategorised cannot be deleted")
    fallback = uncategorised(db, owner)
    moved = (
        db.query(FinanceTransaction)
        .filter(FinanceTransaction.owner == owner, FinanceTransaction.category_id == category.id)
        .update({FinanceTransaction.category_id: fallback.id}, synchronize_session=False)
    )
    db.query(FinanceRule).filter(
        FinanceRule.owner == owner, FinanceRule.category_id == category.id
    ).delete(synchronize_session=False)
    db.delete(category)
    db.flush()
    return int(moved or 0)


# ── Transactions ────────────────────────────────────────────────────────


def _get_transaction(db: Session, owner: str, transaction_id) -> FinanceTransaction:
    tx = (
        db.query(FinanceTransaction)
        .filter(FinanceTransaction.owner == owner, FinanceTransaction.id == str(transaction_id or ""))
        .first()
    )
    if tx is None:
        raise FinanceNotFound("transaction not found")
    return tx


def _resolve_category_id(db: Session, owner: str, category_id) -> str:
    if category_id in (None, ""):
        return uncategorised(db, owner).id
    return _get_category(db, owner, category_id).id


def create_transaction(
    db: Session,
    owner,
    *,
    account_id,
    date,
    amount,
    direction,
    category_id=None,
    description="",
    notes="",
    import_batch_id=None,
) -> FinanceTransaction:
    owner = _require_owner(owner)
    account = _get_account(db, owner, account_id)
    tx = FinanceTransaction(
        id=_new_id(),
        owner=owner,
        account_id=account.id,
        date=parse_date(date),
        amount_pence=_amount(amount),
        direction=_direction(direction),
        category_id=_resolve_category_id(db, owner, category_id),
        description=_clean_text(description, field="description", max_len=MAX_DESCRIPTION_LEN),
        notes=_clean_text(notes, field="notes", max_len=MAX_NOTES_LEN),
        import_batch_id=import_batch_id,
    )
    db.add(tx)
    db.flush()
    return tx


def _transfer_pair(db: Session, owner: str, transfer_id: str) -> tuple[FinanceTransaction, FinanceTransaction]:
    sides = (
        db.query(FinanceTransaction)
        .filter(FinanceTransaction.owner == owner, FinanceTransaction.transfer_id == transfer_id)
        .all()
    )
    out_side = next((t for t in sides if t.direction == "out"), None)
    in_side = next((t for t in sides if t.direction == "in"), None)
    if out_side is None or in_side is None or len(sides) != 2:
        raise FinanceConflict("transfer is incomplete")
    return out_side, in_side


def create_transfer(
    db: Session, owner, *, from_account_id, to_account_id, date, amount, description="", notes=""
) -> tuple[FinanceTransaction, FinanceTransaction]:
    """Move money between two of the owner's accounts as a linked pair."""
    owner = _require_owner(owner)
    source = _get_account(db, owner, from_account_id)
    target = _get_account(db, owner, to_account_id)
    if source.id == target.id:
        raise FinanceError("choose two different accounts for a transfer")
    transfer_id = _new_id()
    common = dict(
        owner=owner,
        date=parse_date(date),
        amount_pence=_amount(amount),
        category_id=None,
        description=_clean_text(description, field="description", max_len=MAX_DESCRIPTION_LEN)
        or f"Transfer: {source.name} to {target.name}",
        notes=_clean_text(notes, field="notes", max_len=MAX_NOTES_LEN),
        transfer_id=transfer_id,
    )
    out_side = FinanceTransaction(id=_new_id(), account_id=source.id, direction="out", **common)
    in_side = FinanceTransaction(id=_new_id(), account_id=target.id, direction="in", **common)
    db.add_all([out_side, in_side])
    db.flush()
    return out_side, in_side


def update_transaction(db: Session, owner, transaction_id, **changes) -> FinanceTransaction:
    """Edit a transaction. Editing either side of a transfer updates both sides."""
    owner = _require_owner(owner)
    tx = _get_transaction(db, owner, transaction_id)
    if tx.transfer_id:
        return _update_transfer(db, owner, tx, changes)
    if changes.get("account_id") is not None:
        tx.account_id = _get_account(db, owner, changes["account_id"]).id
    if changes.get("date") is not None:
        tx.date = parse_date(changes["date"])
    if changes.get("amount") is not None:
        tx.amount_pence = _amount(changes["amount"])
    if changes.get("direction") is not None:
        tx.direction = _direction(changes["direction"])
    if "category_id" in changes:
        tx.category_id = _resolve_category_id(db, owner, changes["category_id"])
    if changes.get("description") is not None:
        tx.description = _clean_text(changes["description"], field="description", max_len=MAX_DESCRIPTION_LEN)
    if changes.get("notes") is not None:
        tx.notes = _clean_text(changes["notes"], field="notes", max_len=MAX_NOTES_LEN)
    db.flush()
    return tx


def _update_transfer(db: Session, owner: str, tx: FinanceTransaction, changes: dict) -> FinanceTransaction:
    out_side, in_side = _transfer_pair(db, owner, tx.transfer_id)
    if changes.get("direction") is not None and _direction(changes["direction"]) != tx.direction:
        raise FinanceError("swap the from/to accounts to change a transfer's direction")
    if changes.get("category_id"):
        raise FinanceError("transfers have no category")
    if changes.get("from_account_id") is not None:
        out_side.account_id = _get_account(db, owner, changes["from_account_id"]).id
    if changes.get("to_account_id") is not None:
        in_side.account_id = _get_account(db, owner, changes["to_account_id"]).id
    if changes.get("account_id") is not None:
        tx.account_id = _get_account(db, owner, changes["account_id"]).id
    if out_side.account_id == in_side.account_id:
        raise FinanceError("choose two different accounts for a transfer")
    for side in (out_side, in_side):
        if changes.get("date") is not None:
            side.date = parse_date(changes["date"])
        if changes.get("amount") is not None:
            side.amount_pence = _amount(changes["amount"])
        if changes.get("description") is not None:
            side.description = _clean_text(changes["description"], field="description",
                                           max_len=MAX_DESCRIPTION_LEN)
        if changes.get("notes") is not None:
            side.notes = _clean_text(changes["notes"], field="notes", max_len=MAX_NOTES_LEN)
    db.flush()
    return tx


def delete_transaction(db: Session, owner, transaction_id) -> int:
    """Delete a transaction (both sides for a transfer). Returns rows deleted."""
    owner = _require_owner(owner)
    tx = _get_transaction(db, owner, transaction_id)
    if tx.transfer_id:
        deleted = (
            db.query(FinanceTransaction)
            .filter(FinanceTransaction.owner == owner, FinanceTransaction.transfer_id == tx.transfer_id)
            .delete(synchronize_session=False)
        )
    else:
        db.delete(tx)
        deleted = 1
    db.flush()
    return int(deleted)


def month_range(year: int, month: int) -> tuple[date, date]:
    """First day of the month and first day of the next month (exclusive end)."""
    if not 1 <= int(month) <= 12:
        raise FinanceError("month must be 1-12")
    start = date(int(year), int(month), 1)
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


@dataclass
class TransactionFilter:
    start: Optional[date] = None  # inclusive
    end: Optional[date] = None  # exclusive
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    direction: Optional[str] = None
    search: str = ""
    include_transfers: bool = True


def _filtered(db: Session, owner: str, flt: TransactionFilter):
    query = db.query(FinanceTransaction).filter(FinanceTransaction.owner == owner)
    if flt.start is not None:
        query = query.filter(FinanceTransaction.date >= flt.start)
    if flt.end is not None:
        query = query.filter(FinanceTransaction.date < flt.end)
    if flt.account_id:
        query = query.filter(FinanceTransaction.account_id == flt.account_id)
    if flt.category_id:
        query = query.filter(FinanceTransaction.category_id == flt.category_id)
    if flt.direction:
        query = query.filter(FinanceTransaction.direction == _direction(flt.direction))
    if flt.search:
        needle = f"%{flt.search.strip().lower()}%"
        query = query.filter(or_(func.lower(FinanceTransaction.description).like(needle),
                                 func.lower(FinanceTransaction.notes).like(needle)))
    if not flt.include_transfers:
        query = query.filter(FinanceTransaction.transfer_id.is_(None))
    return query


def list_transactions(db: Session, owner, flt: Optional[TransactionFilter] = None, *, limit: Optional[int] = None) -> list[dict]:
    """Transactions newest first. Transfer rows include the other side's account."""
    owner = _require_owner(owner)
    flt = flt or TransactionFilter()
    query = _filtered(db, owner, flt).order_by(
        FinanceTransaction.date.desc(), FinanceTransaction.created_at.desc(), FinanceTransaction.id
    )
    if limit:
        query = query.limit(int(limit))
    rows = query.all()
    transfer_ids = {t.transfer_id for t in rows if t.transfer_id}
    other_side: dict[tuple[str, str], str] = {}
    if transfer_ids:
        for side in db.query(FinanceTransaction).filter(
            FinanceTransaction.owner == owner, FinanceTransaction.transfer_id.in_(transfer_ids)
        ):
            other_side[(side.transfer_id, side.direction)] = side.account_id
    result = []
    for tx in rows:
        counterpart = None
        if tx.transfer_id:
            counterpart = other_side.get((tx.transfer_id, "in" if tx.direction == "out" else "out"))
        result.append(transaction_to_dict(tx, transfer_account_id=counterpart))
    return result


def summary(db: Session, owner, *, start: date, end: date, account_id: Optional[str] = None) -> dict:
    """Totals in/out, net and breakdown by category for ``start <= date < end``.

    Transfers are excluded: they move money, they are not income or spending.
    """
    owner = _require_owner(owner)
    flt = TransactionFilter(start=start, end=end, account_id=account_id, include_transfers=False)
    rows = (
        _filtered(db, owner, flt)
        .with_entities(FinanceTransaction.direction, FinanceTransaction.category_id,
                       func.sum(FinanceTransaction.amount_pence), func.count(FinanceTransaction.id))
        .group_by(FinanceTransaction.direction, FinanceTransaction.category_id)
        .all()
    )
    categories = {c.id: c for c in db.query(FinanceCategory).filter(FinanceCategory.owner == owner)}
    total_in = total_out = 0
    spend, income = [], []
    for direction, category_id, total, count in rows:
        category = categories.get(category_id)
        item = {
            "category_id": category_id,
            "name": category.name if category else UNCATEGORISED,
            "color": (category.color if category else "") or "",
            "total_pence": int(total or 0),
            "count": int(count or 0),
        }
        if direction == "in":
            total_in += item["total_pence"]
            income.append(item)
        else:
            total_out += item["total_pence"]
            spend.append(item)
    by_total = lambda item: (-item["total_pence"], item["name"].lower())  # noqa: E731
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_in_pence": total_in,
        "total_out_pence": total_out,
        "net_pence": total_in - total_out,
        "spend_by_category": sorted(spend, key=by_total),
        "income_by_category": sorted(income, key=by_total),
    }
