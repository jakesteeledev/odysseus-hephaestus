"""Finance tracker API (``/api/finance``).

Thin HTTP layer over ``src.finance``: resolve the owner, validate the body,
call the service, commit. Business rules live in the service so routes,
agent tools and the codex API behave the same.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.database import SessionLocal, engine
from src.auth_helpers import require_user
from src.finance import csv_io, imports
from src.finance import loans
from src.finance import recurring as rec
from src.finance import networth as nw
from src.finance import service as fin
from src.finance.models import ACCOUNT_TYPES, CATEGORY_COLORS, ensure_tables

logger = logging.getLogger(__name__)


# ── Request models ──────────────────────────────────────────────────────


class AccountCreate(BaseModel):
    name: str
    account_type: str = "current"
    opening_balance: str = "0"


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    account_type: Optional[str] = None
    opening_balance: Optional[str] = None
    archived: Optional[bool] = None
    sort_order: Optional[int] = None


class CategoryCreate(BaseModel):
    name: str
    kind: str = "expense"
    color: str = ""


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    color: Optional[str] = None


class TransactionCreate(BaseModel):
    account_id: str
    date: str
    amount: str
    direction: str
    category_id: Optional[str] = None
    description: str = ""
    notes: str = ""


class TransactionUpdate(BaseModel):
    account_id: Optional[str] = None
    from_account_id: Optional[str] = None
    to_account_id: Optional[str] = None
    date: Optional[str] = None
    amount: Optional[str] = None
    direction: Optional[str] = None
    category_id: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None


class TransferCreate(BaseModel):
    from_account_id: str
    to_account_id: str
    date: str
    amount: str
    description: str = ""
    notes: str = ""


class ImportRequest(BaseModel):
    csv_text: str = Field(..., max_length=csv_io.MAX_IMPORT_BYTES)
    mapping: Dict[str, Any]
    account_id: str
    filename: str = ""
    mapping_name: str = ""
    category_overrides: Dict[str, str] = {}
    exclude_rows: List[int] = []
    liability_overrides: Dict[str, str] = {}


class RuleCreate(BaseModel):
    pattern: str
    category_id: str
    liability_id: Optional[str] = None


class RuleUpdate(BaseModel):
    pattern: Optional[str] = None
    category_id: Optional[str] = None
    liability_id: Optional[str] = None


class MappingSave(BaseModel):
    name: str
    config: Dict[str, Any]


class ItemCreate(BaseModel):
    name: str
    type: str = "other"
    subtype: str = ""
    provider: str = ""
    lender: str = ""
    notes: str = ""
    linked_asset_id: Optional[str] = None
    value: Optional[str] = None
    value_date: Optional[str] = None


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    subtype: Optional[str] = None
    provider: Optional[str] = None
    lender: Optional[str] = None
    notes: Optional[str] = None
    archived: Optional[bool] = None
    linked_asset_id: Optional[str] = None


class ValuationCreate(BaseModel):
    value: str
    date: Optional[str] = None
    note: str = ""


class ContributionCreate(BaseModel):
    amount: str
    date: Optional[str] = None
    note: str = ""


class ContributionLink(BaseModel):
    asset_id: Optional[str] = None


class ScheduleSet(BaseModel):
    start_date: str
    start_balance: str
    apr: str = "0"
    term_months: Optional[int] = None
    monthly_payment: Optional[str] = None
    payment_day: Optional[int] = None
    balloon: str = "0"


class RepaymentCreate(BaseModel):
    amount: str
    date: Optional[str] = None
    note: str = ""


class PaymentLink(BaseModel):
    liability_id: Optional[str] = None


class RateSet(BaseModel):
    interest_rate: Optional[str] = None


class RecurringCreate(BaseModel):
    name: str
    account_id: str
    amount: str
    direction: str = "out"
    frequency: str = "monthly"
    next_due: Optional[str] = None
    end_date: Optional[str] = None
    category_id: Optional[str] = None
    match_text: str = ""
    liability_id: Optional[str] = None
    asset_id: Optional[str] = None


class RecurringUpdate(BaseModel):
    name: Optional[str] = None
    account_id: Optional[str] = None
    amount: Optional[str] = None
    direction: Optional[str] = None
    frequency: Optional[str] = None
    next_due: Optional[str] = None
    end_date: Optional[str] = None
    category_id: Optional[str] = None
    match_text: Optional[str] = None
    liability_id: Optional[str] = None
    asset_id: Optional[str] = None
    archived: Optional[bool] = None


class OccurrenceAction(BaseModel):
    due_date: str
    date: Optional[str] = None
    amount: Optional[str] = None
    transaction_id: Optional[str] = None


class MakeRecurring(BaseModel):
    frequency: str = "monthly"
    name: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────


def _owner(request: Request) -> str:
    user = require_user(request)
    try:
        return fin.resolve_owner(user)
    except fin.FinanceError as exc:
        raise HTTPException(403, str(exc)) from exc


@contextmanager
def _session():
    """Open a session, commit on success, map finance errors to HTTP codes."""
    ensure_tables(engine)
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except fin.FinanceNotFound as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except fin.FinanceConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except fin.FinanceError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _optional_date(value: Optional[str], field: str) -> Optional[date]:
    if value in (None, ""):
        return None
    return fin.parse_date(value, field=field)


def _range(year: Optional[int], month: Optional[int], start: Optional[str], end: Optional[str]):
    """A month (year+month) or an inclusive start/end range -> (start, exclusive end)."""
    if year is not None and month is not None:
        return fin.month_range(year, month)
    start_d = _optional_date(start, "start")
    end_d = _optional_date(end, "end")
    if end_d is not None:
        end_d = end_d + timedelta(days=1)
    return start_d, end_d


def _changes(body: BaseModel) -> dict:
    return body.model_dump(exclude_unset=True)


# ── Routes ──────────────────────────────────────────────────────────────


def setup_finance_routes() -> APIRouter:
    router = APIRouter(prefix="/api/finance", tags=["finance"])

    @router.get("/meta")
    def meta(request: Request):
        _owner(request)
        return {
            "currency": "GBP",
            "account_types": list(ACCOUNT_TYPES),
            "category_colors": [c for c in CATEGORY_COLORS if c],
            "date_formats": list(csv_io.DATE_FORMATS),
            "default_date_format": csv_io.DEFAULT_DATE_FORMAT,
            "max_import_rows": csv_io.MAX_IMPORT_ROWS,
        }

    # Accounts

    @router.get("/accounts")
    def list_accounts(request: Request, include_archived: bool = False):
        owner = _owner(request)
        with _session() as db:
            accounts = fin.list_accounts(db, owner, include_archived=include_archived)
            active = [a for a in accounts if not a["archived"]]
            return {"accounts": accounts, "total_pence": sum(a["balance_pence"] for a in active)}

    @router.post("/accounts")
    def create_account(body: AccountCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            account = fin.create_account(db, owner, name=body.name, account_type=body.account_type,
                                         opening_balance=body.opening_balance)
            balance = fin.account_balances(db, owner, [account.id])[account.id]
            return fin.account_to_dict(account, balance)

    @router.patch("/accounts/{account_id}")
    def update_account(account_id: str, body: AccountUpdate, request: Request):
        owner = _owner(request)
        with _session() as db:
            account = fin.update_account(db, owner, account_id, **_changes(body))
            balance = fin.account_balances(db, owner, [account.id])[account.id]
            return fin.account_to_dict(account, balance)

    @router.delete("/accounts/{account_id}")
    def delete_account(account_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            fin.delete_account(db, owner, account_id)
            return {"ok": True}

    # Categories

    @router.get("/categories")
    def list_categories(request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"categories": fin.list_categories(db, owner)}

    @router.post("/categories")
    def create_category(body: CategoryCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return fin.category_to_dict(fin.create_category(db, owner, name=body.name, kind=body.kind,
                                                            color=body.color))

    @router.patch("/categories/{category_id}")
    def update_category(category_id: str, body: CategoryUpdate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return fin.category_to_dict(fin.update_category(db, owner, category_id, **_changes(body)))

    @router.get("/categories/{category_id}/usage")
    def category_usage(category_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            fin._get_category(db, owner, category_id)
            return {"transactions": fin.category_usage(db, owner, category_id)}

    @router.delete("/categories/{category_id}")
    def delete_category(category_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"ok": True, "reassigned": fin.delete_category(db, owner, category_id)}

    # Transactions and transfers

    @router.get("/transactions")
    def list_transactions(
        request: Request,
        year: Optional[int] = None,
        month: Optional[int] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        account_id: Optional[str] = None,
        category_id: Optional[str] = None,
        direction: Optional[str] = None,
        q: str = "",
        limit: Optional[int] = None,
    ):
        owner = _owner(request)
        with _session() as db:
            start_d, end_d = _range(year, month, start, end)
            flt = fin.TransactionFilter(start=start_d, end=end_d, account_id=account_id or None,
                                        category_id=category_id or None, direction=direction or None,
                                        search=q[:100])
            if limit is not None:
                limit = max(1, min(int(limit), 2000))
            return {"transactions": fin.list_transactions(db, owner, flt, limit=limit)}

    @router.post("/transactions")
    def create_transaction(body: TransactionCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            tx = fin.create_transaction(db, owner, **body.model_dump())
            return fin.transaction_to_dict(tx)

    @router.patch("/transactions/{transaction_id}")
    def update_transaction(transaction_id: str, body: TransactionUpdate, request: Request):
        owner = _owner(request)
        with _session() as db:
            tx = fin.update_transaction(db, owner, transaction_id, **_changes(body))
            return fin.transaction_to_dict(tx)

    @router.delete("/transactions/{transaction_id}")
    def delete_transaction(transaction_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"ok": True, "deleted": fin.delete_transaction(db, owner, transaction_id)}

    @router.post("/transfers")
    def create_transfer(body: TransferCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            out_side, in_side = fin.create_transfer(db, owner, **body.model_dump())
            return {
                "transfer_id": out_side.transfer_id,
                "out": fin.transaction_to_dict(out_side, transfer_account_id=in_side.account_id),
                "in": fin.transaction_to_dict(in_side, transfer_account_id=out_side.account_id),
            }

    # Summary and export

    @router.get("/summary")
    def summary(request: Request, year: int, month: int, account_id: Optional[str] = None):
        owner = _owner(request)
        with _session() as db:
            start, end = fin.month_range(year, month)
            return fin.summary(db, owner, start=start, end=end, account_id=account_id or None)

    @router.get("/export")
    def export(request: Request, start: Optional[str] = None, end: Optional[str] = None,
               account_id: Optional[str] = None):
        owner = _owner(request)
        with _session() as db:
            text = imports.export_transactions(
                db, owner, start=_optional_date(start, "start"), end=_optional_date(end, "end"),
                account_id=account_id or None,
            )
        parts = ["finance"] + [p for p in (start, end) if p]
        filename = "-".join(parts) + ".csv"
        return Response(
            content=text.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Import

    @router.post("/import/preview")
    def import_preview(body: ImportRequest, request: Request):
        owner = _owner(request)
        with _session() as db:
            return imports.preview_import(db, owner, data=body.csv_text, mapping=body.mapping,
                                          account_id=body.account_id)

    @router.post("/import/commit")
    def import_commit(body: ImportRequest, request: Request):
        owner = _owner(request)
        with _session() as db:
            return imports.commit_import(
                db, owner, data=body.csv_text, mapping=body.mapping, account_id=body.account_id,
                filename=body.filename, mapping_name=body.mapping_name,
                category_overrides=body.category_overrides, exclude_rows=body.exclude_rows,
                liability_overrides=body.liability_overrides,
            )

    @router.get("/imports")
    def list_imports(request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"imports": imports.list_batches(db, owner)}

    @router.post("/imports/{batch_id}/undo")
    def undo_import(batch_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"ok": True, "removed": imports.undo_import(db, owner, batch_id)}

    # Rules

    @router.get("/rules")
    def list_rules(request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"rules": imports.list_rules(db, owner)}

    @router.post("/rules")
    def create_rule(body: RuleCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return imports.rule_to_dict(imports.create_rule(db, owner, pattern=body.pattern,
                                                            category_id=body.category_id,
                                                            liability_id=body.liability_id))

    @router.patch("/rules/{rule_id}")
    def update_rule(rule_id: str, body: RuleUpdate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return imports.rule_to_dict(imports.update_rule(db, owner, rule_id, **_changes(body)))

    @router.delete("/rules/{rule_id}")
    def delete_rule(rule_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            imports.delete_rule(db, owner, rule_id)
            return {"ok": True}

    # Saved bank mappings

    @router.get("/mappings")
    def list_mappings(request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"mappings": imports.list_mappings(db, owner)}

    @router.post("/mappings")
    def save_mapping(body: MappingSave, request: Request):
        owner = _owner(request)
        with _session() as db:
            return imports.mapping_to_dict(imports.save_mapping(db, owner, name=body.name,
                                                                config=body.config))

    @router.delete("/mappings/{mapping_id}")
    def delete_mapping(mapping_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            imports.delete_mapping(db, owner, mapping_id)
            return {"ok": True}

    # Net worth, assets and liabilities (v2)

    @router.get("/networth")
    def networth(request: Request, as_of: Optional[str] = None):
        owner = _owner(request)
        with _session() as db:
            return nw.net_worth(db, owner, as_of=_optional_date(as_of, "as_of"))

    # Recurring items (v2 stage 3): specific paths before the generic routes below.

    @router.get("/upcoming")
    def upcoming(request: Request, days: int = 30):
        owner = _owner(request)
        with _session() as db:
            return rec.upcoming(db, owner, days=days)

    @router.get("/recurring")
    def list_recurring(request: Request, include_archived: bool = False):
        owner = _owner(request)
        with _session() as db:
            return {"recurring": rec.list_recurring(db, owner, include_archived=include_archived)}

    @router.get("/recurring/suggestions")
    def recurring_suggestions(request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"suggestions": rec.suggestions(db, owner)}

    @router.post("/recurring")
    def create_recurring(body: RecurringCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return rec.recurring_to_dict(rec.create_recurring(db, owner, **body.model_dump()))

    @router.patch("/recurring/{recurring_id}")
    def update_recurring(recurring_id: str, body: RecurringUpdate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return rec.recurring_to_dict(rec.update_recurring(db, owner, recurring_id, **_changes(body)))

    @router.delete("/recurring/{recurring_id}")
    def delete_recurring(recurring_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            rec.delete_recurring(db, owner, recurring_id)
            return {"ok": True}

    @router.post("/recurring/{recurring_id}/{action}")
    def occurrence_action(recurring_id: str, action: str, body: OccurrenceAction, request: Request):
        owner = _owner(request)
        with _session() as db:
            if action == "confirm":
                event = rec.confirm(db, owner, recurring_id, body.due_date, on=body.date, amount=body.amount)
            elif action == "match":
                if not body.transaction_id:
                    raise fin.FinanceError("choose the transaction to match")
                event = rec.match(db, owner, recurring_id, body.due_date, body.transaction_id)
            elif action == "skip":
                event = rec.skip(db, owner, recurring_id, body.due_date)
            elif action == "undo":
                rec.undo(db, owner, recurring_id, body.due_date)
                return {"ok": True}
            else:
                raise HTTPException(404, "unknown action")
            return {"ok": True, "status": event.status, "transaction_id": event.transaction_id}

    @router.post("/transactions/{transaction_id}/make-recurring")
    def make_recurring(transaction_id: str, body: MakeRecurring, request: Request):
        owner = _owner(request)
        with _session() as db:
            item = rec.make_recurring_from_transaction(db, owner, transaction_id, frequency=body.frequency,
                                                       name=body.name)
            return rec.recurring_to_dict(item)

    # Loans (v2 stage 2): specific paths before the generic routes below.

    @router.get("/liabilities/{liability_id}/loan")
    def loan_details(liability_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            return loans.loan_details(db, owner, liability_id)

    @router.put("/liabilities/{liability_id}/schedule")
    def set_schedule(liability_id: str, body: ScheduleSet, request: Request):
        owner = _owner(request)
        with _session() as db:
            return loans.schedule_to_dict(loans.set_schedule(db, owner, liability_id, **body.model_dump()))

    @router.delete("/liabilities/{liability_id}/schedule")
    def delete_schedule(liability_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            loans.delete_schedule(db, owner, liability_id)
            return {"ok": True}

    @router.put("/liabilities/{liability_id}/interest-rate")
    def set_rate(liability_id: str, body: RateSet, request: Request):
        owner = _owner(request)
        with _session() as db:
            item = loans.set_interest_rate(db, owner, liability_id, body.interest_rate)
            return {"interest_rate_percent": item.interest_bp / 100 if item.interest_bp is not None else None}

    @router.post("/liabilities/{liability_id}/repayments")
    def add_repayment(liability_id: str, body: RepaymentCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return loans.repayment_to_dict(loans.add_repayment(db, owner, liability_id, amount=body.amount,
                                                               on=body.date, note=body.note))

    @router.delete("/repayments/{repayment_id}")
    def delete_repayment(repayment_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            loans.delete_repayment(db, owner, repayment_id)
            return {"ok": True}

    @router.put("/transactions/{transaction_id}/payment")
    def link_payment(transaction_id: str, body: PaymentLink, request: Request):
        owner = _owner(request)
        with _session() as db:
            row = loans.link_transaction_payment(db, owner, transaction_id, body.liability_id)
            return {"payment": loans.repayment_to_dict(row) if row else None}

    # Specific paths before the generic /{collection}/{item_id} routes below.
    @router.delete("/valuations/{valuation_id}")
    def delete_valuation(valuation_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            nw.delete_valuation(db, owner, valuation_id)
            return {"ok": True}

    @router.delete("/contributions/{contribution_id}")
    def delete_contribution(contribution_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            nw.delete_contribution(db, owner, contribution_id)
            return {"ok": True}

    def _kind(collection: str) -> str:
        if collection not in ("assets", "liabilities"):
            raise HTTPException(404, "not found")
        return "asset" if collection == "assets" else "liability"

    @router.get("/{collection}")
    def list_items(collection: str, request: Request, include_archived: bool = False):
        kind = _kind(collection)
        owner = _owner(request)
        with _session() as db:
            return {collection: nw.list_items(db, owner, kind, include_archived=include_archived)}

    @router.post("/{collection}")
    def create_item(collection: str, body: ItemCreate, request: Request):
        kind = _kind(collection)
        owner = _owner(request)
        data = body.model_dump()
        if kind == "asset":
            data.pop("lender"); data.pop("linked_asset_id")
        else:
            data.pop("provider")
        with _session() as db:
            item = nw.create_item(db, owner, kind, **data)
            return nw._item_dict(db, owner, kind, item, None)

    @router.patch("/{collection}/{item_id}")
    def update_item(collection: str, item_id: str, body: ItemUpdate, request: Request):
        kind = _kind(collection)
        owner = _owner(request)
        with _session() as db:
            item = nw.update_item(db, owner, kind, item_id, **_changes(body))
            return nw._item_dict(db, owner, kind, item, None)

    @router.delete("/{collection}/{item_id}")
    def delete_item(collection: str, item_id: str, request: Request):
        kind = _kind(collection)
        owner = _owner(request)
        with _session() as db:
            nw.delete_item(db, owner, kind, item_id)
            return {"ok": True}

    @router.get("/{collection}/{item_id}/valuations")
    def list_valuations(collection: str, item_id: str, request: Request):
        kind = _kind(collection)
        owner = _owner(request)
        with _session() as db:
            return {"valuations": nw.list_valuations(db, owner, kind, item_id)}

    @router.post("/{collection}/{item_id}/valuations")
    def add_valuation(collection: str, item_id: str, body: ValuationCreate, request: Request):
        kind = _kind(collection)
        owner = _owner(request)
        with _session() as db:
            snap = nw.set_valuation(db, owner, kind, item_id, value=body.value, on=body.date, note=body.note)
            return nw.valuation_to_dict(snap)

    @router.get("/assets/{asset_id}/contributions")
    def list_contributions(asset_id: str, request: Request):
        owner = _owner(request)
        with _session() as db:
            return {"contributions": nw.list_contributions(db, owner, asset_id)}

    @router.post("/assets/{asset_id}/contributions")
    def add_contribution(asset_id: str, body: ContributionCreate, request: Request):
        owner = _owner(request)
        with _session() as db:
            return nw.contribution_to_dict(nw.add_contribution(db, owner, asset_id, amount=body.amount,
                                                               on=body.date, note=body.note))

    @router.put("/transactions/{transaction_id}/contribution")
    def link_contribution(transaction_id: str, body: ContributionLink, request: Request):
        owner = _owner(request)
        with _session() as db:
            row = nw.link_transaction_contribution(db, owner, transaction_id, body.asset_id)
            return {"contribution": nw.contribution_to_dict(row) if row else None}

    return router
