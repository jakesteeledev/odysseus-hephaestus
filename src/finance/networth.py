"""Assets, liabilities, valuations, contributions and net worth.

Net worth = all assets - all liabilities, as of a date:

- Accounts count automatically: current, savings and cash balances are
  assets; credit card balances are liabilities (a card balance of -£250 is
  £250 owed). Archived accounts are included until their balance is zero.
- Assets and liabilities take the latest dated valuation on or before the
  date. Archived ones are included until their latest value is zero.
- Liabilities are stored as positive amounts owed.

Functions take an open session and an owner, flush but never commit.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.finance.models import (
    ASSET_TYPES,
    LIABILITY_TYPES,
    FinanceAccount,
    FinanceAsset,
    FinanceContribution,
    FinanceLiability,
    FinanceTransaction,
    FinanceValuation,
)
from src.finance.money import MoneyError, parse_pence
from src.finance.service import (
    MAX_NAME_LEN,
    MAX_NOTES_LEN,
    FinanceConflict,
    FinanceError,
    FinanceNotFound,
    _clean_text,
    _new_id,
    _require_owner,
    account_balances,
    parse_date,
)

CONTRIBUTION_TYPES = ("investment", "pension")
ACCOUNT_ASSET_GROUP = "accounts"
CARD_LIABILITY_GROUP = "credit_card"

ASSET_GROUP_LABELS = {
    "accounts": "Cash and accounts", "property": "Property", "investment": "Investments",
    "pension": "Pensions", "vehicle": "Vehicles", "other": "Other assets",
}
LIABILITY_GROUP_LABELS = {
    "credit_card": "Credit cards", "mortgage": "Mortgages", "loan": "Loans",
    "student_loan": "Student loans", "bnpl": "Buy now, pay later", "other": "Other debts",
}


def _value(value, *, field="value") -> int:
    try:
        pence = parse_pence(value)
    except MoneyError as exc:
        raise FinanceError(f"{field}: {exc}") from exc
    if pence < 0:
        raise FinanceError(f"{field} cannot be negative")
    return pence


def _kind_type(kind: str, value) -> str:
    allowed = ASSET_TYPES if kind == "asset" else LIABILITY_TYPES
    text = str(value or "other").strip().lower().replace(" ", "_").replace("-", "_")
    text = {"house": "property", "home": "property", "flat": "property", "isa": "investment",
            "shares": "investment", "crypto": "investment", "car": "vehicle", "sipp": "pension",
            "student": "student_loan", "klarna": "bnpl", "debt": "other"}.get(text, text)
    if text not in allowed:
        raise FinanceError(f"{kind} type must be one of: {', '.join(allowed)}")
    return text


# ── Lookups ─────────────────────────────────────────────────────────────


def _model(kind: str):
    if kind == "asset":
        return FinanceAsset
    if kind == "liability":
        return FinanceLiability
    raise FinanceError("kind must be 'asset' or 'liability'")


def get_item(db: Session, owner, kind: str, item_id):
    model = _model(kind)
    item = db.query(model).filter(model.owner == owner, model.id == str(item_id or "")).first()
    if item is None:
        raise FinanceNotFound(f"{kind} not found")
    return item


def latest_valuation(db: Session, owner: str, kind: str, item_id: str,
                     as_of: Optional[date] = None) -> Optional[FinanceValuation]:
    query = db.query(FinanceValuation).filter(
        FinanceValuation.owner == owner, FinanceValuation.item_kind == kind,
        FinanceValuation.item_id == item_id)
    if as_of is not None:
        query = query.filter(FinanceValuation.date <= as_of)
    return query.order_by(FinanceValuation.date.desc(), FinanceValuation.created_at.desc()).first()


def liability_balance(db: Session, owner: str, liability: FinanceLiability,
                      as_of: Optional[date] = None) -> tuple[int, Optional[date]]:
    """Balance owed and the date it comes from.

    Stage 1: the latest balance snapshot. Loan schedules (stage 2) extend this.
    """
    snap = latest_valuation(db, owner, "liability", liability.id, as_of)
    return (snap.value_pence, snap.date) if snap else (0, None)


def contributions_total(db: Session, owner: str, asset_id: str, as_of: Optional[date] = None) -> int:
    query = db.query(func.coalesce(func.sum(FinanceContribution.amount_pence), 0)).filter(
        FinanceContribution.owner == owner, FinanceContribution.asset_id == asset_id)
    if as_of is not None:
        query = query.filter(FinanceContribution.date <= as_of)
    return int(query.scalar() or 0)


# ── Assets and liabilities ──────────────────────────────────────────────


def _fields(kind: str, data: dict, item=None) -> dict:
    out = {}
    if data.get("name") is not None or item is None:
        out["name"] = _clean_text(data.get("name"), field="name", max_len=MAX_NAME_LEN, required=True)
    type_key = "asset_type" if kind == "asset" else "liability_type"
    if data.get(type_key) is not None or data.get("type") is not None or item is None:
        out[type_key] = _kind_type(kind, data.get(type_key) or data.get("type"))
    for key, limit in (("subtype", 40), ("provider" if kind == "asset" else "lender", MAX_NAME_LEN)):
        if data.get(key) is not None:
            out[key] = _clean_text(data[key], field=key, max_len=limit).lower() if key == "subtype" else \
                _clean_text(data[key], field=key, max_len=limit)
    if data.get("notes") is not None:
        out["notes"] = _clean_text(data["notes"], field="notes", max_len=MAX_NOTES_LEN)
    if data.get("archived") is not None and item is not None:
        out["archived"] = bool(data["archived"])
    return out


def _check_link(db: Session, owner: str, liability_type: str, asset_id) -> Optional[str]:
    if asset_id in (None, ""):
        return None
    if liability_type != "mortgage":
        raise FinanceError("only a mortgage can be linked to a property")
    asset = get_item(db, owner, "asset", asset_id)
    if asset.asset_type != "property":
        raise FinanceError("a mortgage can only be linked to a property asset")
    return asset.id


def create_item(db: Session, owner, kind: str, *, value=None, value_date=None, **data):
    """Create an asset or liability, optionally with a first valuation."""
    owner = _require_owner(owner)
    model = _model(kind)
    fields = _fields(kind, data)
    item = model(id=_new_id(), owner=owner, **fields)
    if kind == "liability":
        item.linked_asset_id = _check_link(db, owner, item.liability_type, data.get("linked_asset_id"))
    db.add(item)
    db.flush()
    if value not in (None, ""):
        set_valuation(db, owner, kind, item.id, value=value, on=value_date)
    return item


def update_item(db: Session, owner, kind: str, item_id, **data):
    owner = _require_owner(owner)
    item = get_item(db, owner, kind, item_id)
    for key, value in _fields(kind, data, item).items():
        setattr(item, key, value)
    if kind == "liability" and "linked_asset_id" in data:
        item.linked_asset_id = _check_link(db, owner, item.liability_type, data.get("linked_asset_id"))
    elif kind == "liability" and item.linked_asset_id and item.liability_type != "mortgage":
        item.linked_asset_id = None
    db.flush()
    return item


def delete_item(db: Session, owner, kind: str, item_id) -> None:
    """Delete an asset/liability with no history; anything with history must be archived."""
    owner = _require_owner(owner)
    item = get_item(db, owner, kind, item_id)
    has_history = db.query(FinanceValuation.id).filter(
        FinanceValuation.owner == owner, FinanceValuation.item_kind == kind,
        FinanceValuation.item_id == item.id).first() is not None
    if kind == "asset":
        has_history = has_history or db.query(FinanceContribution.id).filter(
            FinanceContribution.owner == owner, FinanceContribution.asset_id == item.id).first() is not None
        has_history = has_history or db.query(FinanceLiability.id).filter(
            FinanceLiability.owner == owner, FinanceLiability.linked_asset_id == item.id).first() is not None
    if has_history:
        raise FinanceConflict(f"this {kind} has history or links; archive it instead")
    db.delete(item)
    db.flush()


# ── Valuations ──────────────────────────────────────────────────────────


def valuation_to_dict(v: FinanceValuation) -> dict:
    return {"id": v.id, "item_kind": v.item_kind, "item_id": v.item_id, "date": v.date.isoformat(),
            "value_pence": v.value_pence, "note": v.note or ""}


def set_valuation(db: Session, owner, kind: str, item_id, *, value, on=None, note="") -> FinanceValuation:
    """Record a value (asset) or balance owed (liability) for a day; replaces that day's entry."""
    from src.finance.service import _today_for_owner

    owner = _require_owner(owner)
    item = get_item(db, owner, kind, item_id)
    day = parse_date(on) if on not in (None, "") else _today_for_owner()
    pence = _value(value, field="balance" if kind == "liability" else "value")
    existing = db.query(FinanceValuation).filter(
        FinanceValuation.owner == owner, FinanceValuation.item_kind == kind,
        FinanceValuation.item_id == item.id, FinanceValuation.date == day).first()
    note = _clean_text(note, field="note", max_len=200)
    if existing is not None:
        existing.value_pence = pence
        existing.note = note
        db.flush()
        return existing
    snap = FinanceValuation(id=_new_id(), owner=owner, item_kind=kind, item_id=item.id, date=day,
                            value_pence=pence, note=note)
    db.add(snap)
    db.flush()
    return snap


def list_valuations(db: Session, owner, kind: str, item_id) -> list[dict]:
    owner = _require_owner(owner)
    item = get_item(db, owner, kind, item_id)
    rows = db.query(FinanceValuation).filter(
        FinanceValuation.owner == owner, FinanceValuation.item_kind == kind,
        FinanceValuation.item_id == item.id).order_by(FinanceValuation.date.desc()).all()
    return [valuation_to_dict(v) for v in rows]


def delete_valuation(db: Session, owner, valuation_id) -> None:
    owner = _require_owner(owner)
    snap = db.query(FinanceValuation).filter(
        FinanceValuation.owner == owner, FinanceValuation.id == str(valuation_id or "")).first()
    if snap is None:
        raise FinanceNotFound("valuation not found")
    db.delete(snap)
    db.flush()


# ── Contributions ───────────────────────────────────────────────────────


def contribution_to_dict(c: FinanceContribution) -> dict:
    return {"id": c.id, "asset_id": c.asset_id, "date": c.date.isoformat(), "amount_pence": c.amount_pence,
            "note": c.note or "", "transaction_id": c.transaction_id}


def _contribution_asset(db: Session, owner: str, asset_id) -> FinanceAsset:
    asset = get_item(db, owner, "asset", asset_id)
    if asset.asset_type not in CONTRIBUTION_TYPES:
        raise FinanceError("contributions can only go to an investment or pension")
    return asset


def add_contribution(db: Session, owner, asset_id, *, amount, on=None, note="") -> FinanceContribution:
    """A contribution not linked to an account transaction (e.g. employer pension)."""
    from src.finance.service import _amount, _today_for_owner

    owner = _require_owner(owner)
    asset = _contribution_asset(db, owner, asset_id)
    row = FinanceContribution(
        id=_new_id(), owner=owner, asset_id=asset.id,
        date=parse_date(on) if on not in (None, "") else _today_for_owner(),
        amount_pence=_amount(amount), note=_clean_text(note, field="note", max_len=200))
    db.add(row)
    db.flush()
    return row


def link_transaction_contribution(db: Session, owner, transaction_id, asset_id) -> Optional[FinanceContribution]:
    """Mark a money-out transaction as a contribution to an investment/pension.

    ``asset_id=None`` removes the link. The contribution follows the
    transaction's date and amount and is removed with it.
    """
    from src.finance.service import _get_transaction

    owner = _require_owner(owner)
    tx = _get_transaction(db, owner, transaction_id)
    existing = db.query(FinanceContribution).filter(
        FinanceContribution.owner == owner, FinanceContribution.transaction_id == tx.id).first()
    if asset_id in (None, ""):
        if existing is not None:
            db.delete(existing)
            db.flush()
        return None
    if tx.direction != "out":
        raise FinanceError("only money going out can be a contribution")
    asset = _contribution_asset(db, owner, asset_id)
    if existing is None:
        existing = FinanceContribution(id=_new_id(), owner=owner, transaction_id=tx.id, asset_id=asset.id,
                                       date=tx.date, amount_pence=tx.amount_pence)
        db.add(existing)
    else:
        existing.asset_id = asset.id
    existing.date, existing.amount_pence = tx.date, tx.amount_pence
    db.flush()
    return existing


def sync_transaction_contribution(db: Session, owner: str, tx: FinanceTransaction) -> None:
    """Keep a linked contribution's date/amount in step after the transaction is edited."""
    row = db.query(FinanceContribution).filter(
        FinanceContribution.owner == owner, FinanceContribution.transaction_id == tx.id).first()
    if row is None:
        return
    if tx.direction != "out":
        db.delete(row)
    else:
        row.date, row.amount_pence = tx.date, tx.amount_pence
    db.flush()


def list_contributions(db: Session, owner, asset_id) -> list[dict]:
    owner = _require_owner(owner)
    asset = get_item(db, owner, "asset", asset_id)
    rows = db.query(FinanceContribution).filter(
        FinanceContribution.owner == owner, FinanceContribution.asset_id == asset.id).order_by(
        FinanceContribution.date.desc()).all()
    return [contribution_to_dict(c) for c in rows]


def contribution_links(db: Session, owner: str, transaction_ids) -> dict:
    """Map transaction id -> asset id for linked contributions."""
    ids = list(transaction_ids)
    if not ids:
        return {}
    rows = db.query(FinanceContribution.transaction_id, FinanceContribution.asset_id).filter(
        FinanceContribution.owner == owner, FinanceContribution.transaction_id.in_(ids)).all()
    return dict(rows)


def delete_contribution(db: Session, owner, contribution_id) -> None:
    owner = _require_owner(owner)
    row = db.query(FinanceContribution).filter(
        FinanceContribution.owner == owner, FinanceContribution.id == str(contribution_id or "")).first()
    if row is None:
        raise FinanceNotFound("contribution not found")
    if row.transaction_id:
        raise FinanceConflict("this contribution comes from a transaction; unlink it from the transaction")
    db.delete(row)
    db.flush()


# ── Net worth ───────────────────────────────────────────────────────────


def list_items(db: Session, owner, kind: str, *, include_archived: bool = False,
               as_of: Optional[date] = None) -> list[dict]:
    """Assets or liabilities with their current value (and extras by type)."""
    owner = _require_owner(owner)
    model = _model(kind)
    query = db.query(model).filter(model.owner == owner)
    if not include_archived:
        query = query.filter(model.archived.is_(False))
    items = query.order_by(func.lower(model.name)).all()
    return [_item_dict(db, owner, kind, item, as_of) for item in items]


def _item_dict(db: Session, owner: str, kind: str, item, as_of: Optional[date]) -> dict:
    data = {
        "kind": kind, "id": item.id, "name": item.name,
        "type": item.asset_type if kind == "asset" else item.liability_type,
        "subtype": item.subtype or "", "notes": item.notes or "", "archived": bool(item.archived),
    }
    if kind == "asset":
        snap = latest_valuation(db, owner, "asset", item.id, as_of)
        data.update(provider=item.provider or "", value_pence=snap.value_pence if snap else 0,
                    value_date=snap.date.isoformat() if snap else None)
        if item.asset_type in CONTRIBUTION_TYPES:
            contributed = contributions_total(db, owner, item.id, as_of)
            data.update(contributions_pence=contributed, gain_pence=data["value_pence"] - contributed)
        if item.asset_type == "property":
            owed = 0
            mortgages = []
            for liability in db.query(FinanceLiability).filter(
                    FinanceLiability.owner == owner, FinanceLiability.linked_asset_id == item.id):
                balance, _ = liability_balance(db, owner, liability, as_of)
                owed += balance
                mortgages.append({"id": liability.id, "name": liability.name, "balance_pence": balance})
            data.update(mortgages=mortgages, equity_pence=data["value_pence"] - owed)
    else:
        balance, when = liability_balance(db, owner, item, as_of)
        data.update(lender=item.lender or "", value_pence=balance,
                    value_date=when.isoformat() if when else None, linked_asset_id=item.linked_asset_id)
    return data


def net_worth(db: Session, owner, *, as_of: Optional[date] = None) -> dict:
    """Net worth with assets and liabilities grouped by type."""
    owner = _require_owner(owner)
    asset_groups: dict[str, list] = {}
    liability_groups: dict[str, list] = {}

    accounts = db.query(FinanceAccount).filter(FinanceAccount.owner == owner).order_by(
        FinanceAccount.sort_order, FinanceAccount.name).all()
    balances = account_balances(db, owner, [a.id for a in accounts], as_of=as_of)
    for account in accounts:
        balance = balances.get(account.id, account.opening_balance_pence)
        if account.archived and balance == 0:
            continue
        row = {"kind": "account", "id": account.id, "name": account.name, "type": account.account_type,
               "archived": bool(account.archived)}
        if account.account_type == "credit_card":
            liability_groups.setdefault(CARD_LIABILITY_GROUP, []).append({**row, "value_pence": -balance})
        else:
            asset_groups.setdefault(ACCOUNT_ASSET_GROUP, []).append({**row, "value_pence": balance})

    for kind, groups in (("asset", asset_groups), ("liability", liability_groups)):
        for item in list_items(db, owner, kind, include_archived=True, as_of=as_of):
            if item["archived"] and item["value_pence"] == 0:
                continue
            groups.setdefault(item["type"], []).append(item)

    def grouped(groups, labels):
        out = []
        for key in labels:
            if key in groups:
                items = groups[key]
                out.append({"type": key, "label": labels[key], "items": items,
                            "total_pence": sum(i["value_pence"] for i in items)})
        return out

    assets = grouped(asset_groups, ASSET_GROUP_LABELS)
    liabilities = grouped(liability_groups, LIABILITY_GROUP_LABELS)
    assets_total = sum(g["total_pence"] for g in assets)
    liabilities_total = sum(g["total_pence"] for g in liabilities)
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "net_worth_pence": assets_total - liabilities_total,
        "assets_total_pence": assets_total,
        "liabilities_total_pence": liabilities_total,
        "assets": assets,
        "liabilities": liabilities,
    }
