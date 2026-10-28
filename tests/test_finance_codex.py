"""Read-only finance API for external agents (/api/codex/finance/*)."""

from datetime import date

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as core_db
import routes.codex_routes as codex_routes
from routes.api_token_routes import _normalize_scopes
from routes.finance.codex_finance import register_codex_finance
from src.finance import networth as nw
from src.finance import service as fin
from src.finance.models import ensure_tables


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(core_db, "engine", engine)
    monkeypatch.setattr(core_db, "SessionLocal", factory)
    db = factory()
    acc = fin.create_account(db, "alice", name="Current", opening_balance="1000")
    fin.create_transaction(db, "alice", account_id=acc.id, date="2026-10-02", amount="25", direction="out",
                           description="Tesco")
    nw.create_item(db, "alice", "liability", name="Car loan", liability_type="loan", value="5000")
    fin.create_account(db, "bob", name="Bob's", opening_balance="99")
    db.commit()
    db.close()

    app = FastAPI()

    @app.middleware("http")
    async def fake_token(request: Request, call_next):
        # Mimic the auth middleware: X-Scopes simulates an API token for X-Owner.
        if "x-scopes" in request.headers:
            request.state.api_token = True
            request.state.api_token_scopes = [s for s in request.headers["x-scopes"].split(",") if s]
            request.state.api_token_owner = request.headers.get("x-owner", "alice")
        return await call_next(request)

    router = APIRouter(prefix="/api/codex")
    register_codex_finance(router, lambda request: codex_routes._scope_owner(request, codex_routes.FINANCE_READ_SCOPES))
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


READ = {"x-scopes": "finance:read"}


def test_token_with_finance_read_can_read_everything(client):
    worth = client.get("/api/codex/finance/networth", headers=READ).json()
    assert worth["net_worth_pence"] == 100000 - 2500 - 500000
    accounts = client.get("/api/codex/finance/accounts", headers=READ).json()["accounts"]
    assert [a["name"] for a in accounts] == ["Current"]
    txs = client.get("/api/codex/finance/transactions?q=tesco", headers=READ).json()["transactions"]
    assert len(txs) == 1 and txs[0]["amount_pence"] == 2500
    debts = client.get("/api/codex/finance/liabilities", headers=READ).json()["liabilities"]
    assert debts[0]["name"] == "Car loan" and debts[0]["loan"]["balance_pence"] == 500000
    assert client.get("/api/codex/finance/summary?year=2026&month=10", headers=READ).json()["total_out_pence"] == 2500
    assert "items" in client.get("/api/codex/finance/upcoming", headers=READ).json()
    assert "budgets" in client.get("/api/codex/finance/budgets", headers=READ).json()
    assert client.get("/api/codex/finance/assets", headers=READ).json() == {"assets": []}


def test_scope_is_required(client):
    assert client.get("/api/codex/finance/networth", headers={"x-scopes": "todos:read,calendar:write"}).status_code == 403
    assert client.get("/api/codex/finance/networth", headers={"x-scopes": ""}).status_code == 403


def test_each_token_sees_only_its_owner(client):
    bob = client.get("/api/codex/finance/accounts", headers={"x-scopes": "finance:read", "x-owner": "bob"}).json()
    assert [a["name"] for a in bob["accounts"]] == ["Bob's"]


def test_there_is_no_way_to_write(client):
    for path in ("/api/codex/finance/accounts", "/api/codex/finance/transactions", "/api/codex/finance/budgets"):
        assert client.post(path, headers=READ, json={}).status_code == 405


def test_finance_read_is_an_allowed_token_scope():
    assert _normalize_scopes("finance:read") == ["finance:read"]
    with pytest.raises(Exception):
        _normalize_scopes("finance:write")


def test_household_reads(client):
    """v3: members, children and ?person= are readable with finance:read too."""
    members = client.get("/api/codex/finance/members", headers=READ).json()["members"]
    assert [m["is_primary"] for m in members] == [True]
    me = members[0]["id"]
    whole = client.get("/api/codex/finance/networth", headers=READ).json()["net_worth_pence"]
    assert client.get(f"/api/codex/finance/networth?person={me}", headers=READ).json()["net_worth_pence"] == whole
    assert client.get("/api/codex/finance/networth?person=nobody", headers=READ).status_code == 404
    assert client.get(f"/api/codex/finance/summary?year=2026&month=10&person={me}", headers=READ).json()[
        "total_out_pence"] == 2500
    assert client.get("/api/codex/finance/children", headers=READ).json()["children"] == []
    assert client.get("/api/codex/finance/members", headers={"x-scopes": ""}).status_code == 403
    assert client.post("/api/codex/finance/members", headers=READ, json={}).status_code == 405
