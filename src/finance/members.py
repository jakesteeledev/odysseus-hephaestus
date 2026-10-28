"""Household members and who owns each account, asset and liability.

- A member is a tracked profile (name, relationship, optional date of birth,
  avatar colour/initials). Only the Odysseus user logs in; every member's
  data stays under that user's ``owner``.
- Each owner has exactly one primary member ("me"), named after their
  Odysseus account. It is created on first use (or by the v6 migration) and
  owns anything that has no owner yet.
- Ownership rows live in ``finance_item_owners``; joint items have several.
  Owners are always listed in a stable order (member sort order, then id),
  which is the order leftover pence go in when amounts are split.

Functions take an open session and an owner, flush but never commit.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.finance.models import (
    CATEGORY_COLORS,
    OWNED_KINDS,
    RELATIONSHIPS,
    FinanceAccount,
    FinanceAsset,
    FinanceItemOwner,
    FinanceLiability,
    FinanceMember,
)
from src.finance.service import (
    MAX_NAME_LEN,
    FinanceConflict,
    FinanceError,
    FinanceNotFound,
    _clean_text,
    _new_id,
    _require_owner,
    _today_for_owner,
    parse_date,
)

RELATIONSHIP_LABELS = {"me": "Me", "partner": "Partner", "child": "Child", "other": "Other"}
# New members get the next colour in turn; the frontend maps names to theme colours.
DEFAULT_COLORS = ("blue", "green", "purple", "orange", "red", "yellow")
MAX_INITIALS = 3

_ITEM_MODELS = {"account": FinanceAccount, "asset": FinanceAsset, "liability": FinanceLiability}


# ── Names ───────────────────────────────────────────────────────────────


def _admin_username() -> str:
    """The admin account's username from auth.json, or the first user's."""
    try:
        from src.constants import AUTH_FILE

        if not os.path.exists(AUTH_FILE):
            return ""
        with open(AUTH_FILE, "r", encoding="utf-8") as handle:
            users = (json.load(handle) or {}).get("users", {}) or {}
    except Exception:
        return ""
    for username, data in users.items():
        if isinstance(data, dict) and data.get("is_admin") is True:
            return str(username)
    return str(next(iter(users), ""))


def default_member_name(owner: str) -> str:
    """The primary member's starting name: the Odysseus account it belongs to.

    With login turned off, data belongs to the reserved local owner, so the
    admin account's name is used. Falls back to "Me".
    """
    from src.owner_identity import is_default_local_owner

    name = "" if is_default_local_owner(owner) else str(owner or "")
    if not name:
        name = _admin_username()
    name = " ".join(name.split())[:MAX_NAME_LEN]
    return name[:1].upper() + name[1:] if name else "Me"


def initials_for(member: FinanceMember) -> str:
    if member.initials:
        return member.initials
    words = [w for w in (member.name or "").split() if w[:1].isalnum()]
    if not words:
        return "?"
    letters = words[0][0] + (words[-1][0] if len(words) > 1 else "")
    return letters.upper()


def age_on(born: Optional[date], today: date) -> Optional[int]:
    if born is None:
        return None
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def member_to_dict(member: FinanceMember, *, today: Optional[date] = None, owns: Optional[int] = None) -> dict:
    today = today or _today_for_owner()
    data = {
        "id": member.id,
        "name": member.name,
        # "Jake (Me)" for the primary member; everyone else by name.
        "display_name": f"{member.name} (Me)" if member.is_primary else member.name,
        "relationship": member.relationship,
        "relationship_label": RELATIONSHIP_LABELS.get(member.relationship, ""),
        "date_of_birth": member.date_of_birth.isoformat() if member.date_of_birth else None,
        "age": age_on(member.date_of_birth, today),
        "color": member.color or "",
        "initials": initials_for(member),
        "custom_initials": member.initials or "",
        "is_primary": bool(member.is_primary),
        "archived": bool(member.archived),
        "sort_order": member.sort_order,
    }
    if owns is not None:
        data["owns_count"] = owns
    return data


# ── Primary member and unowned items ────────────────────────────────────


def primary_member(db: Session, owner: str) -> Optional[FinanceMember]:
    return db.query(FinanceMember).filter(FinanceMember.owner == owner,
                                          FinanceMember.is_primary.is_(True)).first()


def _claim_unowned(db: Session, owner: str, member: FinanceMember) -> int:
    """Give every account, asset and liability without an owner to *member*."""
    claimed = 0
    for kind, model in _ITEM_MODELS.items():
        owned = db.query(FinanceItemOwner.item_id).filter(FinanceItemOwner.owner == owner,
                                                          FinanceItemOwner.item_kind == kind)
        rows = db.query(model.id).filter(model.owner == owner, ~model.id.in_(owned)).all()
        for (item_id,) in rows:
            db.add(FinanceItemOwner(owner=owner, item_kind=kind, item_id=item_id, member_id=member.id))
            claimed += 1
    if claimed:
        db.flush()
    return claimed


def ensure_household(db: Session, owner) -> FinanceMember:
    """Make sure the owner has a primary member and that every item has an owner.

    Safe to call often: it only adds rows that are missing.
    """
    owner = _require_owner(owner)
    member = primary_member(db, owner)
    if member is None:
        member = FinanceMember(id=_new_id(), owner=owner, name=default_member_name(owner), relationship="me",
                               color=DEFAULT_COLORS[0], initials="", is_primary=True, archived=False,
                               sort_order=0, linked_user=None)
        db.add(member)
        db.flush()
    _claim_unowned(db, owner, member)
    return member


# ── Members ─────────────────────────────────────────────────────────────


def get_member(db: Session, owner: str, member_id) -> FinanceMember:
    member = db.query(FinanceMember).filter(FinanceMember.owner == owner,
                                            FinanceMember.id == str(member_id or "")).first()
    if member is None:
        raise FinanceNotFound("member not found")
    return member


def _ordered(query):
    return query.order_by(FinanceMember.sort_order, FinanceMember.id)


def _owns_counts(db: Session, owner: str) -> dict:
    rows = db.query(FinanceItemOwner.member_id, func.count()).filter(
        FinanceItemOwner.owner == owner).group_by(FinanceItemOwner.member_id)
    return {member_id: int(count) for member_id, count in rows}


def list_members(db: Session, owner, *, include_archived: bool = False) -> list[dict]:
    owner = _require_owner(owner)
    ensure_household(db, owner)
    query = db.query(FinanceMember).filter(FinanceMember.owner == owner)
    if not include_archived:
        query = query.filter(FinanceMember.archived.is_(False))
    counts = _owns_counts(db, owner)
    today = _today_for_owner()
    return [member_to_dict(m, today=today, owns=counts.get(m.id, 0)) for m in _ordered(query).all()]


def _relationship(value) -> str:
    text = str(value or "other").strip().lower()
    text = {"wife": "partner", "husband": "partner", "spouse": "partner", "son": "child",
            "daughter": "child", "kid": "child"}.get(text, text)
    if text not in RELATIONSHIPS:
        raise FinanceError("relationship must be partner, child or other")
    if text == "me":
        raise FinanceError("only the primary member can be 'me'")
    return text


def _color(value) -> str:
    color = str(value or "").strip().lower()
    if color not in CATEGORY_COLORS:
        raise FinanceError("colour must be one of: " + ", ".join(c for c in CATEGORY_COLORS if c))
    return color


def _initials(value) -> str:
    text = "".join(str(value or "").split()).upper()
    if len(text) > MAX_INITIALS:
        raise FinanceError(f"initials must be at most {MAX_INITIALS} characters")
    return text


def _birth_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    born = parse_date(value, field="date of birth")
    if born > _today_for_owner():
        raise FinanceError("date of birth can't be in the future")
    return born


def _check_name_free(db: Session, owner: str, name: str, exclude_id: Optional[str] = None) -> None:
    query = db.query(FinanceMember.id).filter(FinanceMember.owner == owner,
                                              func.lower(FinanceMember.name) == name.lower())
    if exclude_id:
        query = query.filter(FinanceMember.id != exclude_id)
    if query.first() is not None:
        raise FinanceConflict(f"a member called {name!r} already exists")


def create_member(db: Session, owner, *, name, relationship="other", date_of_birth=None, color=None,
                  initials="") -> FinanceMember:
    owner = _require_owner(owner)
    ensure_household(db, owner)
    name = _clean_text(name, field="name", max_len=MAX_NAME_LEN, required=True)
    _check_name_free(db, owner, name)
    count, last = db.query(func.count(FinanceMember.id), func.max(FinanceMember.sort_order)).filter(
        FinanceMember.owner == owner).one()
    member = FinanceMember(
        id=_new_id(), owner=owner, name=name, relationship=_relationship(relationship),
        date_of_birth=_birth_date(date_of_birth),
        color=_color(color) if color not in (None, "") else DEFAULT_COLORS[int(count or 0) % len(DEFAULT_COLORS)],
        initials=_initials(initials), is_primary=False, archived=False, sort_order=int(last or 0) + 1,
        linked_user=None,
    )
    db.add(member)
    db.flush()
    return member


def update_member(db: Session, owner, member_id, **changes) -> FinanceMember:
    owner = _require_owner(owner)
    member = get_member(db, owner, member_id)
    if changes.get("name") is not None:
        name = _clean_text(changes["name"], field="name", max_len=MAX_NAME_LEN, required=True)
        _check_name_free(db, owner, name, exclude_id=member.id)
        member.name = name
    if changes.get("relationship") is not None:
        if member.is_primary:
            if str(changes["relationship"]).strip().lower() != "me":
                raise FinanceError("the primary member is always 'me'")
        else:
            member.relationship = _relationship(changes["relationship"])
    if "date_of_birth" in changes:
        member.date_of_birth = _birth_date(changes["date_of_birth"])
    if changes.get("color") is not None:
        member.color = _color(changes["color"])
    if changes.get("initials") is not None:
        member.initials = _initials(changes["initials"])
    if changes.get("archived") is not None:
        if member.is_primary and changes["archived"]:
            raise FinanceConflict("the primary member can't be archived")
        member.archived = bool(changes["archived"])
    if changes.get("sort_order") is not None and not member.is_primary:
        member.sort_order = max(1, int(changes["sort_order"]))
    db.flush()
    return member


def delete_member(db: Session, owner, member_id) -> None:
    """Delete a member who owns nothing; anyone who owns something must be archived."""
    owner = _require_owner(owner)
    member = get_member(db, owner, member_id)
    if member.is_primary:
        raise FinanceConflict("the primary member can't be deleted")
    owns = db.query(FinanceItemOwner.item_id).filter(FinanceItemOwner.owner == owner,
                                                     FinanceItemOwner.member_id == member.id).first()
    if owns is not None:
        raise FinanceConflict(f"{member.name} owns accounts or items; archive them instead")
    db.delete(member)
    db.flush()


# ── Ownership ───────────────────────────────────────────────────────────


def _kind(kind: str) -> str:
    if kind not in OWNED_KINDS:
        raise FinanceError("kind must be account, asset or liability")
    return kind


def owners_of(db: Session, owner: str, kind: str, item_ids: Optional[Iterable[str]] = None) -> dict:
    """Map item id -> member ids that own it, in the stable split order."""
    query = db.query(FinanceItemOwner.item_id, FinanceItemOwner.member_id).join(
        FinanceMember, FinanceMember.id == FinanceItemOwner.member_id).filter(
        FinanceItemOwner.owner == owner, FinanceItemOwner.item_kind == _kind(kind))
    if item_ids is not None:
        query = query.filter(FinanceItemOwner.item_id.in_(list(item_ids)))
    out: dict = {}
    for item_id, member_id in query.order_by(FinanceMember.sort_order, FinanceMember.id):
        out.setdefault(item_id, []).append(member_id)
    return out


def assign_default_owner(db: Session, owner, kind: str, item_id: str) -> None:
    """New items belong to the primary member."""
    member = ensure_household(db, owner)
    exists = db.query(FinanceItemOwner.item_id).filter(
        FinanceItemOwner.owner == owner, FinanceItemOwner.item_kind == _kind(kind),
        FinanceItemOwner.item_id == item_id).first()
    if exists is None:
        db.add(FinanceItemOwner(owner=owner, item_kind=kind, item_id=item_id, member_id=member.id))
        db.flush()


def remove_owners(db: Session, owner: str, kind: str, item_id: str) -> None:
    """Drop ownership rows when the item itself is deleted."""
    db.query(FinanceItemOwner).filter(FinanceItemOwner.owner == owner, FinanceItemOwner.item_kind == _kind(kind),
                                      FinanceItemOwner.item_id == item_id).delete(synchronize_session=False)
    db.flush()
