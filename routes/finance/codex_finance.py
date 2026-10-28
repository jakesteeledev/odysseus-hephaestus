"""Read-only finance endpoints for external agents (/api/codex/finance/*).

Registered on the codex router so they share its token handling: a bearer
token needs the ``finance:read`` scope and acts as its owner; a signed-in
browser session acts as its user. Nothing here can change finance data.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from src.finance import budgets as bud
from src.finance import loans
from src.finance import networth as nw
from src.finance import recurring as rec
from src.finance import service as fin

MAX_TRANSACTIONS = 500


@contextmanager
def _read_session():
    from core.database import SessionLocal, engine
    from src.finance.models import ensure_tables

    ensure_tables(engine)
    db = SessionLocal()
    try:
        yield db
        db.commit()  # first use may seed default categories
    except fin.FinanceNotFound as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except fin.FinanceError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def register_codex_finance(router: APIRouter, owner_for: Callable[[Request], str]) -> None:
    def owner(request: Request) -> str:
        try:
            return fin.resolve_owner(owner_for(request))
        except fin.FinanceError as exc:
            raise HTTPException(403, str(exc)) from exc

    @router.get("/finance/networth")
    def codex_finance_networth(request: Request, as_of: Optional[str] = None):
        who = owner(request)
        with _read_session() as db:
            return nw.net_worth(db, who, as_of=fin.parse_date(as_of, field="as_of") if as_of else None)

    @router.get("/finance/accounts")
    def codex_finance_accounts(request: Request, include_archived: bool = False):
        who = owner(request)
        with _read_session() as db:
            return {"accounts": fin.list_accounts(db, who, include_archived=include_archived)}

    @router.get("/finance/assets")
    def codex_finance_assets(request: Request):
        who = owner(request)
        with _read_session() as db:
            return {"assets": nw.list_items(db, who, "asset")}

    @router.get("/finance/liabilities")
    def codex_finance_liabilities(request: Request):
        who = owner(request)
        with _read_session() as db:
            items = nw.list_items(db, who, "liability")
            for item in items:
                item["loan"] = loans.loan_details(db, who, item["id"])
            return {"liabilities": items}

    @router.get("/finance/transactions")
    def codex_finance_transactions(request: Request, start: Optional[str] = None, end: Optional[str] = None,
                                   account_id: Optional[str] = None, category_id: Optional[str] = None,
                                   q: str = "", limit: int = 100):
        who = owner(request)
        with _read_session() as db:
            flt = fin.TransactionFilter(
                start=fin.parse_date(start, field="start") if start else None,
                end=fin.parse_date(end, field="end") + timedelta(days=1) if end else None,
                account_id=account_id or None, category_id=category_id or None, search=q[:100])
            rows = fin.list_transactions(db, who, flt, limit=max(1, min(int(limit), MAX_TRANSACTIONS)))
            return {"transactions": rows}

    @router.get("/finance/summary")
    def codex_finance_summary(request: Request, year: int, month: int, account_id: Optional[str] = None):
        who = owner(request)
        with _read_session() as db:
            start, end = fin.month_range(year, month)
            return fin.summary(db, who, start=start, end=end, account_id=account_id or None)

    @router.get("/finance/upcoming")
    def codex_finance_upcoming(request: Request, days: int = 30):
        who = owner(request)
        with _read_session() as db:
            return rec.upcoming(db, who, days=days)

    @router.get("/finance/budgets")
    def codex_finance_budgets(request: Request, year: Optional[int] = None, month: Optional[int] = None):
        who = owner(request)
        with _read_session() as db:
            return bud.budget_status(db, who, year=year, month=month)
