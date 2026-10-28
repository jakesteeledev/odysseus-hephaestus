"""Household finance for the agent: whose figures, members and children.

- ``resolve_person`` turns the model's ``person`` argument into the whole
  household, one member, or "the children". "me"/"my" is the primary member
  (the user); no person is the household; names are matched loosely and an
  ambiguous or unknown name comes back as options to ask about.
- ``do_manage_members`` lists household members (a read) and adds, updates,
  archives or restores them, or sets who owns an account, asset or debt
  (writes, gated like every other finance write).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Optional

from src.finance import members as mem
from src.finance import service as fin
from src.finance.matching import match_name
from src.finance.money import format_pence
from src.tools._common import _parse_tool_args

logger = logging.getLogger(__name__)

_HOUSEHOLD_WORDS = {"household", "family", "everyone", "everybody", "all", "us", "we", "our", "ours", "both",
                    "together", "combined", "whole household", "whole family", "joint", "total"}
_ME_WORDS = {"me", "my", "mine", "i", "myself", "self", "primary", "user"}
_CHILDREN_WORDS = {"kids", "children", "child accounts", "the kids", "the children", "my kids", "my children",
                   "our kids", "our children", "kid", "child"}
_RELATION_WORDS = {
    "partner": "partner", "wife": "partner", "husband": "partner", "spouse": "partner", "girlfriend": "partner",
    "boyfriend": "partner", "fiance": "partner", "fiancee": "partner",
    "son": "child", "daughter": "child",
}


@dataclass
class Scope:
    kind: str  # "household", "member" or "children"
    member: Optional[dict] = None

    @property
    def member_id(self) -> Optional[str]:
        return self.member["id"] if self.member else None

    def label(self) -> str:
        if self.kind == "household":
            return "the whole household"
        if self.kind == "children":
            return "the children"
        return f"{self.member['name']} (their share; joint items are split equally between owners)"


def _err(message: str) -> Dict:
    return {"error": message, "exit_code": 1}


def _clean(text) -> str:
    text = str(text or "").strip().casefold()
    text = re.sub(r"(?:'s|s'|’s|s’)\b", "", text)  # "Sarah's" -> "sarah", "kids'" -> "kid"
    return " ".join(re.sub(r"[^\w\s-]", " ", text).split())


def resolve_person(db, owner: str, value) -> tuple[Optional[Scope], Optional[Dict]]:
    """Return (scope, None) or (None, error asking the user to choose)."""
    text = _clean(value)
    if not text or text in _HOUSEHOLD_WORDS:
        return Scope("household"), None
    members = mem.list_members(db, owner, include_archived=True)
    if text in _ME_WORDS:
        primary = next((m for m in members if m["is_primary"]), None)
        return (Scope("member", primary), None) if primary else (Scope("household"), None)
    if text in _CHILDREN_WORDS or text in {"kids", "childrens"}:
        return Scope("children"), None
    words = text.split()
    relation = _RELATION_WORDS.get(words[-1]) if words and (len(words) == 1 or words[0] in ("my", "our")) else None
    if relation:
        found = [m for m in members if m["relationship"] == relation and not m["archived"]]
        if len(found) == 1:
            return Scope("member", found[0]), None
        if not found:
            return None, _err(f"No household member is recorded as the user's {words[-1]}. Members: "
                              f"{', '.join(m['name'] for m in members)}. Ask the user who they mean, or add "
                              "them with manage_members.")
        return None, _err(f"More than one member could be the user's {words[-1]}: "
                          f"{', '.join(m['name'] for m in found)}. Ask the user which one.")
    active = [m for m in members if not m["archived"]]
    match = match_name(text, active)
    if not match.ok and match.status == "missing":
        match = match_name(text, members)
    if match.ok:
        return Scope("member", match.item), None
    names = ", ".join(m["name"] for m in members)
    if match.status == "ambiguous":
        return None, _err(f"'{value}' matches more than one household member: {', '.join(match.options)}. "
                          "Ask the user which one.")
    return None, _err(f"No household member called '{value}'. Members: {names}. Ask the user who they mean.")


# ── Children ────────────────────────────────────────────────────────────


def children_text(data: dict, only: Optional[str] = None) -> tuple[list[str], list[dict]]:
    """Lines describing each child's money, Junior ISAs and pocket money."""
    kids = [c for c in data["children"] if only is None or c["member"]["id"] == only]
    if not kids:
        return ["No children are recorded in the household. They can be added with manage_members "
                "(relationship child) or in Finance settings."], kids
    lines = [f"Children (tax year {data['tax_year']}, {data['tax_year_start']} to {data['tax_year_end']}; "
             f"Junior ISA allowance {format_pence(data['junior_isa_limit_pence'])} per child):"]
    for child in kids:
        m = child["member"]
        age = f"age {m['age']}" if m.get("age") is not None else "date of birth not recorded"
        lines.append(f"- {m['name']} ({age}): {format_pence(child['net_worth_pence'])} in their name")
        for item in child["items"]:
            value = -item["value_pence"] if item["side"] == "liability" else item["value_pence"]
            share = f" (their share of {format_pence(item['full_value_pence'])})" if item.get("joint") else ""
            lines.append(f"  - {item['name']}: {format_pence(value)}{share}")
        for isa in child["junior_isas"]:
            if isa["unlock_date"] is None:
                lock = "locked until they turn 18 (date of birth not recorded)"
            elif isa["locked"]:
                lock = f"locked until {isa['unlock_date']} (their 18th birthday)"
            else:
                lock = f"unlocked since {isa['unlock_date']}"
            lines.append(f"  - Junior ISA {isa['name']}: {format_pence(isa['value_pence'])}, {lock}; "
                         f"paid in this tax year {format_pence(isa['contributed_pence'])}")
        allowance = child.get("junior_isa_allowance")
        if allowance:
            if allowance["over_pence"]:
                state = f"OVER the allowance by {format_pence(allowance['over_pence'])}"
            else:
                state = f"{format_pence(allowance['remaining_pence'])} of the allowance left"
            lines.append(f"  - Junior ISA allowance {data['tax_year']}: {format_pence(allowance['contributed_pence'])} "
                         f"paid in of {format_pence(allowance['limit_pence'])}; {state}")
        for sipp in child["junior_sipps"]:
            lines.append(f"  - Junior SIPP {sipp['name']}: {format_pence(sipp['value_pence'])} "
                         "(a pension: can't be taken until pension age)")
        for r in child["regular_transfers"]:
            lines.append(f"  - Regular transfer {r['name']}: {format_pence(r['amount_pence'])} "
                         f"{r['frequency_label'].lower()} from {r['from_account']} to {r['to_account']}")
    return lines, kids


# ── manage_members ──────────────────────────────────────────────────────


def _member_line(m: dict) -> str:
    bits = [m["relationship_label"]]
    if m.get("age") is not None:
        bits.append(f"age {m['age']}, born {m['date_of_birth']}")
    if m.get("owns_count"):
        bits.append(f"owns {m['owns_count']} item(s)")
    if m.get("archived"):
        bits.append("archived")
    return f"- {m['display_name']} ({', '.join(bits)})"


def _find_member(db, owner: str, query) -> tuple[Optional[dict], Optional[Dict]]:
    scope, error = resolve_person(db, owner, query)
    if error:
        return None, {**error, "error": "Nothing saved. " + error["error"]}
    if scope.kind != "member":
        return None, _err("Nothing saved. Name one household member. Ask the user who they mean.")
    return scope.member, None


def _find_item(db, owner: str, query, kind_hint=None) -> tuple[Optional[tuple], Optional[Dict]]:
    """An account, asset or liability by loose name: ((kind, item), None) or (None, error)."""
    from src.finance import networth as nw

    kinds = {"account": "account", "accounts": "account", "asset": "asset", "assets": "asset",
             "liability": "liability", "liabilities": "liability", "debt": "liability"}
    wanted = kinds.get(str(kind_hint or "").strip().lower())
    candidates = []
    if wanted in (None, "account"):
        candidates += [("account", a) for a in fin.list_accounts(db, owner)]
    if wanted in (None, "asset"):
        candidates += [("asset", a) for a in nw.list_items(db, owner, "asset")]
    if wanted in (None, "liability"):
        candidates += [("liability", a) for a in nw.list_items(db, owner, "liability")]
    match = match_name(query, candidates, name=lambda pair: pair[1]["name"])
    if match.ok:
        return match.item, None
    shown = ", ".join(match.options[:20]) or "none yet"
    if match.status == "ambiguous":
        return None, _err(f"Nothing saved. '{query}' matches more than one item: {shown}. Ask the user which one.")
    return None, _err(f"Nothing saved. No account, asset or debt called '{query}'. Existing: {shown}. "
                      "Ask the user which one.")


def _owner_names(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [p for p in re.split(r"\s*(?:,|\band\b|&|\+)\s*", str(value or "")) if p.strip()]


async def do_manage_members(content, owner: Optional[str] = None) -> Dict:
    """List or change household members, or set who owns an account, asset or debt."""
    from src.tools.finance import _first, _session, _today

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    action = str(_first(args, "action", default="list")).strip().lower().replace(" ", "_")
    action = {"show": "list", "view": "list", "create": "add", "new": "add", "edit": "update", "rename": "update",
              "remove": "archive", "unarchive": "restore", "owners": "set_owners", "set_owner": "set_owners",
              "assign": "set_owners", "make_joint": "set_owners"}.get(action, action)
    if action not in ("list", "add", "update", "archive", "restore", "set_owners"):
        return _err("action must be list, add, update, archive, restore or set_owners")
    try:
        owner = fin.resolve_owner(owner)
        with _session() as db:
            if action == "list":
                members = mem.list_members(db, owner, include_archived=True)
                db.commit()  # the primary member may have just been created
                return {"response": "Household members:\n" + "\n".join(_member_line(m) for m in members),
                        "data": {"members": members}, "exit_code": 0}

            if action == "add":
                name = _first(args, "name")
                if not name:
                    return _err("Nothing saved. A new member needs a name. Ask the user.")
                member = mem.create_member(db, owner, name=name,
                                           relationship=_first(args, "relationship", "relation", default="other"),
                                           date_of_birth=_first(args, "date_of_birth", "dob", "born"),
                                           color=_first(args, "color", "colour"))
                saved = mem.member_to_dict(member, today=_today())
                db.commit()
                return {"response": f"Added {saved['name']} to the household ({saved['relationship_label']}). "
                                    "They don't own anything yet; use set_owners to add them to accounts or assets.",
                        "data": {"member": saved}, "exit_code": 0}

            member, error = _find_member(db, owner, _first(args, "member", "person", "name"))
            if action in ("update", "archive", "restore") and error:
                return error

            if action == "update":
                changes = {}
                if _first(args, "new_name") is not None:
                    changes["name"] = args["new_name"]
                for key, aliases in (("relationship", ("relationship", "relation")),
                                     ("date_of_birth", ("date_of_birth", "dob", "born")),
                                     ("color", ("color", "colour"))):
                    value = _first(args, *aliases)
                    if value is not None:
                        changes[key] = value
                if not changes:
                    return _err("Nothing saved. Say what to change: new_name, relationship, date_of_birth or color.")
                updated = mem.member_to_dict(mem.update_member(db, owner, member["id"], **changes), today=_today())
                db.commit()
                return {"response": f"Updated {updated['name']}.", "data": {"member": updated}, "exit_code": 0}

            if action in ("archive", "restore"):
                updated = mem.member_to_dict(mem.update_member(db, owner, member["id"], archived=action == "archive"),
                                             today=_today())
                db.commit()
                verb = "Archived" if action == "archive" else "Restored"
                note = " They keep what they own; archived members aren't offered for new items." \
                    if action == "archive" else ""
                return {"response": f"{verb} {updated['name']}.{note}", "data": {"member": updated}, "exit_code": 0}

            # set_owners
            item_query = _first(args, "item", "account", "asset", "liability")
            if not item_query:
                return _err("Nothing saved. Name the account, asset or debt whose owners should change.")
            found, error = _find_item(db, owner, item_query, _first(args, "item_kind", "kind"))
            if error:
                return error
            kind, item = found
            names = _owner_names(_first(args, "owners", "members", "people"))
            if not names:
                return _err("Nothing saved. List who owns it, e.g. owners=['me', 'Sarah'].")
            ids = []
            for name in names:
                person, error = _find_member(db, owner, name)
                if error:
                    return error
                ids.append(person["id"])
            ordered = mem.set_owners(db, owner, kind, item["id"], ids)
            everyone = {m["id"]: m["name"] for m in mem.list_members(db, owner, include_archived=True)}
            db.commit()
            who = " and ".join(everyone[i] for i in ordered)
            note = (" Its transactions, including past ones, now belong to these owners too, so everyone's "
                    "figures have been recalculated.") if kind == "account" else ""
            split = " Joint items are split equally." if len(ordered) > 1 else ""
            return {"response": f"{item['name']} is now owned by {who}.{split}{note}",
                    "data": {"kind": kind, "id": item["id"], "owner_ids": ordered}, "exit_code": 0}
    except fin.FinanceError as exc:
        return _err(f"Nothing saved. {exc}")
    except Exception as exc:  # pragma: no cover - unexpected storage failure
        logger.exception("manage_members failed")
        return _err(f"Could not update household members: {exc}")
