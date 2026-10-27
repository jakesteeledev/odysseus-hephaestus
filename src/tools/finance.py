"""Finance agent tools: query_finances, add_transaction, add_transfer.

Thin layer over ``src.finance.service``: parse arguments, resolve the owner
the same way the Finance routes do, match account/category names, and turn
results into short £x.xx text that small local models can relay. Business
rules (balances, transfers, totals) stay in the service.

Ambiguous or unknown names never create anything: the tool returns the
options and tells the model to ask the user.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from src.finance import service as fin
from src.finance.matching import match_name
from src.finance.money import format_pence
from src.tools._common import _parse_tool_args

logger = logging.getLogger(__name__)

QUERY_VIEWS = ("overview", "net_worth", "balances", "assets", "liabilities", "summary", "transactions")
_DEFAULT_TX_LIMIT = 10
_MAX_TX_LIMIT = 50


# ── Plumbing ────────────────────────────────────────────────────────────


@contextmanager
def _session():
    from core.database import SessionLocal, engine
    from src.finance.models import ensure_tables

    ensure_tables(engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _err(message: str) -> Dict:
    return {"error": message, "exit_code": 1}


def _today() -> date:
    try:
        from src.user_time import now_user_local

        return now_user_local().date()
    except Exception:  # pragma: no cover - timezone helper unavailable
        return date.today()


def _parse_day(value, *, field: str) -> date:
    """ISO dates, dd/mm/yyyy, or today / yesterday / tomorrow."""
    text = str(value or "").strip().lower()
    if not text or text == "today":
        return _today()
    if text == "yesterday":
        return _today() - timedelta(days=1)
    if text == "tomorrow":
        return _today() + timedelta(days=1)
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    raise fin.FinanceError(f"{field} must be a date like 2026-10-27 or 'today'")


def _first(args: dict, *keys, default=None):
    for key in keys:
        value = args.get(key)
        if value not in (None, ""):
            return value
    return default


def _resolve(db, owner, kind: str, query, *, include_archived=False):
    """Return (item, None) or (None, error dict asking the user to choose)."""
    if kind == "account":
        items = fin.list_accounts(db, owner, include_archived=include_archived)
        if not items:
            return None, _err("Nothing saved. There are no finance accounts yet. Ask the user to add one "
                              "in the Finance window first. Do not invent an account.")
    else:
        items = fin.list_categories(db, owner)
    match = match_name(query, items)
    if match.ok:
        return match.item, None
    shown = ", ".join(match.options[:15])
    if match.status == "ambiguous":
        return None, _err(f"Nothing saved. '{query}' matches more than one {kind}: {shown}. "
                          f"Ask the user which {kind} they mean.")
    return None, _err(f"Nothing saved. No {kind} called '{query}'. Existing {kind}s: {shown}. "
                      f"Ask the user which one to use. Do not create a new {kind}.")


def _range(args: dict) -> tuple[date, date]:
    """Inclusive start/end from arguments; defaults to this calendar month."""
    today = _today()
    start = _first(args, "start", "start_date", "from")
    end = _first(args, "end", "end_date", "to")
    month_start, next_month = fin.month_range(today.year, today.month)
    start_d = _parse_day(start, field="start") if start else month_start
    end_d = _parse_day(end, field="end") if end else (next_month - timedelta(days=1))
    if end_d < start_d:
        raise fin.FinanceError("end must not be before start")
    return start_d, end_d


def _period_label(start: date, end: date) -> str:
    return f"{start.isoformat()} to {end.isoformat()}"


# ── query_finances ──────────────────────────────────────────────────────


def _balances_text(accounts: list[dict]) -> list[str]:
    lines = []
    for account in accounts:
        kind = account["account_type"].replace("_", " ")
        lines.append(f"- {account['name']} ({kind}): {format_pence(account['balance_pence'])}")
    return lines


def _summary_text(s: dict) -> list[str]:
    lines = [
        f"Money in: {format_pence(s['total_in_pence'])}",
        f"Money out: {format_pence(s['total_out_pence'])}",
        f"Net: {format_pence(s['net_pence'])}",
    ]
    if s["spend_by_category"]:
        lines.append("Spending by category:")
        lines += [f"- {c['name']}: {format_pence(c['total_pence'])} ({c['count']})" for c in s["spend_by_category"]]
    else:
        lines.append("No spending in this period.")
    lines.append("(Transfers between the user's own accounts are not counted as money in or out.)")
    return lines


def _transactions_text(rows: list[dict], accounts: dict, categories: dict) -> list[str]:
    if not rows:
        return ["No matching transactions."]
    lines = []
    for tx in rows:
        sign = "+" if tx["direction"] == "in" else "-"
        if tx["transfer_id"]:
            other = accounts.get(tx.get("transfer_account_id"), "another account")
            what = f"transfer {'to' if tx['direction'] == 'out' else 'from'} {other}"
        else:
            what = categories.get(tx["category_id"], fin.UNCATEGORISED)
        desc = tx["description"] or "(no description)"
        lines.append(f"- {tx['date']} {sign}{format_pence(tx['amount_pence'])} {desc} "
                     f"[{accounts.get(tx['account_id'], '?')}, {what}]")
    return lines


async def do_query_finances(content, owner: Optional[str] = None) -> Dict:
    """Read-only view of the user's finances."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    view = str(_first(args, "view", "action", "type", default="overview")).strip().lower().replace(" ", "_").replace("-", "_")
    view = {"networth": "net_worth", "worth": "net_worth", "total": "net_worth", "balance": "balances",
            "accounts": "balances", "spending": "summary", "totals": "summary", "breakdown": "summary",
            "recent": "transactions", "list": "transactions", "transaction": "transactions",
            "asset": "assets", "investments": "assets", "pensions": "assets", "property": "assets",
            "liability": "liabilities", "debts": "liabilities", "debt": "liabilities", "loans": "liabilities",
            "owe": "liabilities", "mortgage": "liabilities"}.get(view, view)
    if view not in QUERY_VIEWS:
        return _err(f"view must be one of: {', '.join(QUERY_VIEWS)}")
    try:
        owner = fin.resolve_owner(owner)
        with _session() as db:
            result = _query(db, owner, view, args)
            db.commit()  # default categories may have been seeded
            return result
    except fin.FinanceError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - unexpected storage failure
        logger.exception("query_finances failed")
        return _err(f"Could not read finances: {exc}")


def _net_worth_text(worth: dict) -> list[str]:
    lines = [
        f"Net worth: {format_pence(worth['net_worth_pence'])}",
        f"Assets: {format_pence(worth['assets_total_pence'])}",
    ]
    for group in worth["assets"]:
        lines.append(f"- {group['label']}: {format_pence(group['total_pence'])}")
        lines += [f"  - {_item_line(i)}" for i in group["items"]]
    lines.append(f"Liabilities (owed): {format_pence(worth['liabilities_total_pence'])}")
    for group in worth["liabilities"]:
        lines.append(f"- {group['label']}: {format_pence(group['total_pence'])}")
        lines += [f"  - {_item_line(i)}" for i in group["items"]]
    if not worth["liabilities"]:
        lines.append("- none recorded")
    return lines


def _item_line(item: dict) -> str:
    text = f"{item['name']}: {format_pence(item['value_pence'])}"
    extras = []
    if item.get("value_date"):
        extras.append(f"as of {item['value_date']}")
    elif item.get("kind") in ("asset", "liability"):
        extras.append("no value recorded yet")
    if "gain_pence" in item:
        extras.append(f"contributed {format_pence(item['contributions_pence'])}, "
                      f"gain {format_pence(item['gain_pence'])}")
    if "equity_pence" in item and item.get("mortgages"):
        extras.append(f"equity {format_pence(item['equity_pence'])}")
    if item.get("archived"):
        extras.append("archived")
    return text + (f" ({'; '.join(extras)})" if extras else "")


def _query(db, owner: str, view: str, args: dict) -> Dict:
    from src.finance import networth as nw

    overview = fin.accounts_overview(db, owner)
    accounts = overview["accounts"]
    data: dict = {"view": view, "currency": "GBP"}
    lines: list[str] = []
    has_items = bool(nw.list_items(db, owner, "asset") or nw.list_items(db, owner, "liability"))
    if not accounts and not has_items and view not in ("transactions", "assets", "liabilities"):
        return {"response": "The user has no finance accounts, assets or liabilities yet. They can add them "
                            "in the Finance window.",
                "data": {"view": view, "accounts": [], "net_worth_pence": 0}, "exit_code": 0}

    if view in ("overview", "net_worth"):
        worth = nw.net_worth(db, owner)
        data["net_worth"] = worth
        data["net_worth_pence"] = worth["net_worth_pence"]
        lines += _net_worth_text(worth)

    if view == "balances":
        data["net_worth_pence"] = overview["total_pence"]
        data["accounts"] = [{"name": a["name"], "type": a["account_type"], "balance_pence": a["balance_pence"],
                             "balance": format_pence(a["balance_pence"])} for a in accounts]
        lines.append(f"Total across active accounts: {format_pence(overview['total_pence'])}")
        lines.append("Balances:")
        lines += _balances_text(accounts)

    if view in ("assets", "liabilities"):
        kind = "asset" if view == "assets" else "liability"
        items = nw.list_items(db, owner, kind)
        data[view] = items
        if not items:
            lines.append(f"No {view} recorded yet.")
        else:
            label = "Assets" if kind == "asset" else "Liabilities (owed)"
            lines.append(f"{label}: {format_pence(sum(i['value_pence'] for i in items))}")
            lines += [f"- {_item_line(i)} [{i['type'].replace('_', ' ')}]" for i in items]
        if kind == "liability":
            cards = [a for a in accounts if a["account_type"] == "credit_card"]
            if cards:
                lines.append("Credit cards (owed): " + ", ".join(
                    f"{a['name']} {format_pence(-a['balance_pence'])}" for a in cards))

    if view in ("overview", "summary"):
        account_id = None
        if _first(args, "account"):
            account, error = _resolve(db, owner, "account", args["account"], include_archived=True)
            if error:
                return {**error, "error": error["error"].replace("Nothing saved. ", "")}
            account_id = account["id"]
        start, end = _range(args)
        s = fin.summary(db, owner, start=start, end=end + timedelta(days=1), account_id=account_id)
        data["summary"] = {k: s[k] for k in ("total_in_pence", "total_out_pence", "net_pence",
                                             "spend_by_category", "income_by_category")}
        data["period"] = {"start": start.isoformat(), "end": end.isoformat()}
        lines.append(f"Period {_period_label(start, end)}" + (f" ({account['name']})" if account_id else "") + ":")
        lines += _summary_text(s)

    if view == "transactions":
        flt = fin.TransactionFilter(search=str(_first(args, "search", "query", "q", default=""))[:100])
        if _first(args, "start", "start_date", "from", "end", "end_date", "to"):
            start, end = _range(args)
            flt.start, flt.end = start, end + timedelta(days=1)
        if _first(args, "account"):
            account, error = _resolve(db, owner, "account", args["account"], include_archived=True)
            if error:
                return {**error, "error": error["error"].replace("Nothing saved. ", "")}
            flt.account_id = account["id"]
        if _first(args, "category"):
            category, error = _resolve(db, owner, "category", args["category"])
            if error:
                return {**error, "error": error["error"].replace("Nothing saved. ", "")}
            flt.category_id = category["id"]
        try:
            limit = int(_first(args, "limit", default=_DEFAULT_TX_LIMIT))
        except (TypeError, ValueError):
            limit = _DEFAULT_TX_LIMIT
        limit = max(1, min(limit, _MAX_TX_LIMIT))
        rows = fin.list_transactions(db, owner, flt, limit=limit)
        names = {a["id"]: a["name"] for a in fin.list_accounts(db, owner, include_archived=True)}
        cats = {c["id"]: c["name"] for c in fin.list_categories(db, owner)}
        data["transactions"] = rows
        lines.append(f"Most recent {len(rows)} transaction(s), newest first:")
        lines += _transactions_text(rows, names, cats)

    return {"response": "\n".join(lines), "data": data, "exit_code": 0}


# ── add_transaction ─────────────────────────────────────────────────────


def _direction(args: dict) -> str:
    raw = str(_first(args, "direction", "type", default="out")).strip().lower()
    aliases = {"in": "in", "income": "in", "incoming": "in", "credit": "in", "received": "in", "deposit": "in",
               "out": "out", "outgoing": "out", "expense": "out", "spend": "out", "spent": "out",
               "debit": "out", "payment": "out", "paid": "out"}
    if raw not in aliases:
        raise fin.FinanceError("direction must be 'in' (money received) or 'out' (money spent)")
    return aliases[raw]


def _amount_text(value) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise fin.FinanceError("amount is required, e.g. 12.50")
    # Spending is recorded with direction, so a leading minus is just a sign of "out".
    return re.sub(r"^-\s*", "", text)


async def do_add_transaction(content, owner: Optional[str] = None) -> Dict:
    """Record money in or out on one of the user's accounts."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    try:
        owner = fin.resolve_owner(owner)
        direction = _direction(args)
        amount = _amount_text(_first(args, "amount", "value"))
        day = _parse_day(_first(args, "date"), field="date")
        with _session() as db:
            account_query = _first(args, "account", "account_name")
            if account_query is None:
                active = fin.list_accounts(db, owner)
                if len(active) != 1:
                    names = ", ".join(a["name"] for a in active) or "none yet"
                    return _err(f"Nothing saved. Which account? Existing accounts: {names}. Ask the user.")
                account = active[0]
            else:
                account, error = _resolve(db, owner, "account", account_query)
                if error:
                    return error
            category = None
            category_query = _first(args, "category", "category_name")
            if category_query is not None:
                category, error = _resolve(db, owner, "category", category_query)
                if error:
                    return error
            tx = fin.create_transaction(
                db, owner, account_id=account["id"], date=day, amount=amount, direction=direction,
                category_id=category["id"] if category else None,
                description=str(_first(args, "description", "desc", "name", default="")),
                notes=str(_first(args, "notes", "note", default="")),
            )
            category_name = category["name"] if category else fin.UNCATEGORISED
            balance = fin.account_balances(db, owner, [account["id"]])[account["id"]]
            saved = fin.transaction_to_dict(tx)  # read before commit expires the row
            db.commit()
    except fin.FinanceError as exc:
        return _err(f"Nothing saved. {exc}")
    except Exception as exc:  # pragma: no cover - unexpected storage failure
        logger.exception("add_transaction failed")
        return _err(f"Nothing saved: {exc}")
    verb = "Money in" if direction == "in" else "Money out"
    text = (f"{verb} recorded: {format_pence(saved['amount_pence'])} on {saved['date']}, "
            f"{account['name']}, {category_name}"
            + (f", \"{saved['description']}\"" if saved["description"] else "")
            + f". {account['name']} balance is now {format_pence(balance)}.")
    return {"response": text, "data": saved, "exit_code": 0}


# ── add_transfer ────────────────────────────────────────────────────────


async def do_add_transfer(content, owner: Optional[str] = None) -> Dict:
    """Move money between two of the user's accounts."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    try:
        owner = fin.resolve_owner(owner)
        amount = _amount_text(_first(args, "amount", "value"))
        day = _parse_day(_first(args, "date"), field="date")
        from_query = _first(args, "from_account", "from", "source")
        to_query = _first(args, "to_account", "to", "destination")
        if from_query is None or to_query is None:
            return _err("Nothing saved. A transfer needs from_account and to_account. Ask the user.")
        with _session() as db:
            source, error = _resolve(db, owner, "account", from_query)
            if error:
                return error
            target, error = _resolve(db, owner, "account", to_query)
            if error:
                return error
            out_side, _in_side = fin.create_transfer(
                db, owner, from_account_id=source["id"], to_account_id=target["id"], date=day,
                amount=amount, description=str(_first(args, "description", "desc", default="")),
            )
            balances = fin.account_balances(db, owner, [source["id"], target["id"]])
            saved = {"transfer_id": out_side.transfer_id, "amount_pence": out_side.amount_pence,
                     "date": out_side.date.isoformat()}  # read before commit expires the row
            db.commit()
    except fin.FinanceError as exc:
        return _err(f"Nothing saved. {exc}")
    except Exception as exc:  # pragma: no cover - unexpected storage failure
        logger.exception("add_transfer failed")
        return _err(f"Nothing saved: {exc}")
    text = (f"Transfer recorded: {format_pence(saved['amount_pence'])} from {source['name']} to "
            f"{target['name']} on {saved['date']}. Balances now: {source['name']} "
            f"{format_pence(balances[source['id']])}, {target['name']} {format_pence(balances[target['id']])}. "
            "Transfers are not counted as income or spending.")
    return {"response": text, "data": {"transfer_id": saved["transfer_id"]}, "exit_code": 0}


# ── update_valuation ────────────────────────────────────────────────────


async def do_update_valuation(content, owner: Optional[str] = None) -> Dict:
    """Set a new value (asset) or balance owed (liability), dated today by default."""
    from src.finance import networth as nw

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    try:
        owner = fin.resolve_owner(owner)
        name = _first(args, "name", "item", "asset", "liability")
        value = _first(args, "value", "balance", "amount")
        if name is None:
            return _err("Nothing saved. Which asset or liability? Ask the user.")
        if value is None:
            return _err("Nothing saved. What is the new value or balance? Ask the user.")
        kind_hint = str(_first(args, "kind", default="")).strip().lower()
        day = _parse_day(_first(args, "date"), field="date")
        with _session() as db:
            candidates = []
            for kind in ("asset", "liability"):
                if kind_hint and kind_hint != kind:
                    continue
                candidates += nw.list_items(db, owner, kind)
            if not candidates:
                return _err("Nothing saved. There are no assets or liabilities yet. Ask the user to add it "
                            "in the Finance window first. Do not invent one.")
            match = match_name(name, candidates)
            if not match.ok:
                accounts = fin.list_accounts(db, owner)
                if match.status == "missing" and match_name(name, accounts).ok:
                    return _err(f"Nothing saved. '{name}' is an account; its balance comes from its "
                                "transactions. Use add_transaction or add_transfer instead.")
                labelled = [f"{c['name']} ({c['kind']})" for c in candidates if c["name"] in match.options]
                if match.status == "ambiguous":
                    return _err(f"Nothing saved. '{name}' matches more than one item: {', '.join(labelled)}. "
                                "Ask the user which they mean.")
                return _err(f"Nothing saved. No asset or liability called '{name}'. Existing: "
                            f"{', '.join(labelled[:15])}. Ask the user which one, or to add it in the "
                            "Finance window. Do not create one.")
            item = match.item
            previous = nw.latest_valuation(db, owner, item["kind"], item["id"])
            before = (previous.value_pence, previous.date.isoformat()) if previous else None
            snap = nw.set_valuation(db, owner, item["kind"], item["id"], value=_amount_text(value), on=day,
                                    note=str(_first(args, "note", default="")))
            saved = nw.valuation_to_dict(snap)
            worth = nw.net_worth(db, owner)["net_worth_pence"]
            db.commit()
    except fin.FinanceError as exc:
        return _err(f"Nothing saved. {exc}")
    except Exception as exc:  # pragma: no cover - unexpected storage failure
        logger.exception("update_valuation failed")
        return _err(f"Nothing saved: {exc}")
    what = "value" if item["kind"] == "asset" else "balance owed"
    text = f"Updated {item['name']} {what} to {format_pence(saved['value_pence'])} on {saved['date']}"
    if before:
        text += f" (was {format_pence(before[0])} on {before[1]})"
    text += f". Net worth is now {format_pence(worth)}."
    return {"response": text, "data": saved, "exit_code": 0}
