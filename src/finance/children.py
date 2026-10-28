"""Children: Junior ISA locks and allowances, settings, and the Children section.

- A Junior ISA is an investment asset with the subtype "junior isa"; a
  Junior SIPP is a pension with the subtype "junior sipp".
- A Junior ISA is locked until the child's 18th birthday (someone born on
  29 February turns 18 on 1 March in a non-leap year).
- Contributions are limited per child per tax year (6 April to 5 April).
  The allowance is a setting (``junior_isa_limit_pence``, default £9,000)
  so it can follow future budgets.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.finance.models import (
    FinanceAccount,
    FinanceAsset,
    FinanceContribution,
    FinanceMember,
    FinanceRecurring,
    FinanceSetting,
)
from src.finance.money import MoneyError, parse_pence
from src.finance.service import FinanceError, _require_owner, _today_for_owner

ADULT_AGE = 18
CHILD_ACCOUNT_TYPES = ("child_savings", "child_current")
SETTING_DEFAULTS = {"junior_isa_limit_pence": 900_000}
MAX_LIMIT_PENCE = 100_000_000  # £1m: anything larger is a typo


# ── Junior ISA / SIPP ───────────────────────────────────────────────────


def _subtype(value) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def is_junior_isa(asset) -> bool:
    return asset.asset_type == "investment" and _subtype(asset.subtype) in ("junior isa", "jisa")


def is_junior_sipp(asset) -> bool:
    return asset.asset_type == "pension" and _subtype(asset.subtype) in ("junior sipp", "jsipp")


def birthday(born: date, age: int) -> date:
    """The day someone born on *born* turns *age* (29 Feb -> 1 Mar in other years)."""
    try:
        return born.replace(year=born.year + age)
    except ValueError:
        return date(born.year + age, 3, 1)


def unlock_date(born: Optional[date]) -> Optional[date]:
    """When a Junior ISA opens up: the child's 18th birthday."""
    return birthday(born, ADULT_AGE) if born else None


def tax_year(day: date) -> dict:
    """The UK tax year containing *day*: 6 April to 5 April."""
    first = day.year if day >= date(day.year, 4, 6) else day.year - 1
    return {"start": date(first, 4, 6), "end": date(first + 1, 4, 5), "label": f"{first}/{str(first + 1)[2:]}"}


# ── Settings ────────────────────────────────────────────────────────────


def get_setting(db: Session, owner: str, key: str) -> int:
    if key not in SETTING_DEFAULTS:
        raise FinanceError(f"unknown setting: {key}")
    row = db.query(FinanceSetting).filter(FinanceSetting.owner == owner, FinanceSetting.key == key).first()
    return int(row.value) if row else SETTING_DEFAULTS[key]


def settings(db: Session, owner) -> dict:
    owner = _require_owner(owner)
    return {key: get_setting(db, owner, key) for key in SETTING_DEFAULTS}


def set_setting(db: Session, owner, key: str, value) -> int:
    """Store a money setting given in pounds, e.g. "9000" or "£9,000"."""
    owner = _require_owner(owner)
    if key not in SETTING_DEFAULTS:
        raise FinanceError(f"unknown setting: {key}")
    try:
        pence = parse_pence(value)
    except MoneyError as exc:
        raise FinanceError(str(exc)) from exc
    if pence < 0 or pence > MAX_LIMIT_PENCE:
        raise FinanceError("enter an amount between £0 and £1,000,000")
    row = db.query(FinanceSetting).filter(FinanceSetting.owner == owner, FinanceSetting.key == key).first()
    if row is None:
        db.add(FinanceSetting(owner=owner, key=key, value=str(pence)))
    else:
        row.value = str(pence)
    db.flush()
    return pence


# ── Contributions ───────────────────────────────────────────────────────


def contributed(db: Session, owner: str, asset_ids, start: date, end: date) -> dict:
    """Map asset id -> contributions dated start..end (inclusive)."""
    ids = list(asset_ids)
    if not ids:
        return {}
    rows = db.query(FinanceContribution.asset_id, func.sum(FinanceContribution.amount_pence)).filter(
        FinanceContribution.owner == owner, FinanceContribution.asset_id.in_(ids),
        FinanceContribution.date >= start, FinanceContribution.date <= end).group_by(FinanceContribution.asset_id)
    return {asset_id: int(total or 0) for asset_id, total in rows}


def junior_isa_allowance(db: Session, owner, member_id: str, *, today: Optional[date] = None) -> dict:
    """How much has gone into a child's Junior ISAs this tax year, against the allowance.

    Joint Junior ISAs (unusual) count at the child's share.
    """
    from src.finance.members import owners_of, share_of

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    year = tax_year(today)
    owners = owners_of(db, owner, "asset")
    isas = [a for a in db.query(FinanceAsset).filter(FinanceAsset.owner == owner).all()
            if is_junior_isa(a) and member_id in owners.get(a.id, [])]
    totals = contributed(db, owner, [a.id for a in isas], year["start"], year["end"])
    paid = sum(share_of(totals.get(a.id, 0), owners.get(a.id, []), member_id) for a in isas)
    limit = get_setting(db, owner, "junior_isa_limit_pence")
    return {
        "tax_year": year["label"], "tax_year_start": year["start"].isoformat(), "tax_year_end": year["end"].isoformat(),
        "contributed_pence": paid, "limit_pence": limit, "remaining_pence": max(0, limit - paid),
        "over_pence": max(0, paid - limit), "accounts": len(isas),
    }


# ── Children section ────────────────────────────────────────────────────


def children_overview(db: Session, owner, *, today: Optional[date] = None) -> dict:
    """Each child with their age, what they own (at their share), Junior ISA
    status and allowance, and regular transfers into their accounts."""
    from src.finance import networth as nw
    from src.finance.members import ensure_household, member_to_dict, owner_map, share_of
    from src.finance.recurring import FREQUENCY_LABELS

    owner = _require_owner(owner)
    ensure_household(db, owner)
    today = today or _today_for_owner()
    owners = owner_map(db, owner)
    owned_by: dict = {}
    for (kind, item_id), ids in owners.items():
        for member_id in ids:
            owned_by.setdefault(member_id, set()).add((kind, item_id))
    kids = [m for m in db.query(FinanceMember).filter(
        FinanceMember.owner == owner, FinanceMember.relationship == "child").order_by(
        FinanceMember.sort_order, FinanceMember.id) if not m.archived or owned_by.get(m.id)]
    year = tax_year(today)
    assets = {a.id: a for a in db.query(FinanceAsset).filter(FinanceAsset.owner == owner)}
    accounts = {a.id: a for a in db.query(FinanceAccount).filter(FinanceAccount.owner == owner)}
    jisa_ids = [a.id for a in assets.values() if is_junior_isa(a)]
    paid = contributed(db, owner, jisa_ids, year["start"], year["end"])
    limit = get_setting(db, owner, "junior_isa_limit_pence")
    transfers = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner,
                                                  FinanceRecurring.archived.is_(False),
                                                  FinanceRecurring.to_account_id.isnot(None)).all()

    out = []
    for kid in kids:
        worth = nw.net_worth(db, owner, person=kid.id, as_of=None)
        items = [{**row, "side": side}
                 for side, groups in (("asset", worth["assets"]), ("liability", worth["liabilities"]))
                 for group in groups for row in group["items"]]
        opens = unlock_date(kid.date_of_birth)
        isas = []
        for row in items:
            asset = assets.get(row["id"]) if row["kind"] == "asset" else None
            if asset is None or not is_junior_isa(asset):
                continue
            isas.append({"id": asset.id, "name": asset.name, "value_pence": row["value_pence"],
                         "contributed_pence": share_of(paid.get(asset.id, 0), owners.get(("asset", asset.id), []),
                                                       kid.id),
                         "locked": opens is None or today < opens,
                         "unlock_date": opens.isoformat() if opens else None})
        sipps = [{"id": row["id"], "name": row["name"], "value_pence": row["value_pence"]}
                 for row in items if row["kind"] == "asset" and assets.get(row["id"]) is not None
                 and is_junior_sipp(assets[row["id"]])]
        kid_accounts = {item_id for kind, item_id in owned_by.get(kid.id, set()) if kind == "account"}
        regular = [{"id": r.id, "name": r.name, "amount_pence": r.amount_pence, "frequency": r.frequency,
                    "frequency_label": FREQUENCY_LABELS[r.frequency],
                    "from_account": accounts[r.account_id].name if r.account_id in accounts else "",
                    "to_account": accounts[r.to_account_id].name if r.to_account_id in accounts else ""}
                   for r in transfers if r.to_account_id in kid_accounts and r.account_id not in kid_accounts]
        jisa_paid = sum(i["contributed_pence"] for i in isas)
        out.append({
            "member": member_to_dict(kid, today=today),
            "net_worth_pence": worth["net_worth_pence"],
            "items": items,
            "junior_isas": isas,
            "junior_sipps": sipps,
            "junior_isa_allowance": {
                "contributed_pence": jisa_paid, "limit_pence": limit, "remaining_pence": max(0, limit - jisa_paid),
                "over_pence": max(0, jisa_paid - limit),
            } if isas else None,
            "adult_on": opens.isoformat() if opens else None,
            "regular_transfers": regular,
        })
    return {"today": today.isoformat(), "tax_year": year["label"], "tax_year_start": year["start"].isoformat(),
            "tax_year_end": year["end"].isoformat(), "junior_isa_limit_pence": limit,
            "total_pence": sum(c["net_worth_pence"] for c in out), "children": out}
