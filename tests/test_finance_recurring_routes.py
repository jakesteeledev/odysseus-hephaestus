"""Finance recurring API: items, upcoming, confirm/match/skip/undo, make recurring."""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes
from src.finance import recurring as rec
from src.finance import service as fin


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: "alice")
    for module in (fin, rec):
        monkeypatch.setattr(module, "_today_for_owner", lambda: date(2026, 10, 27))
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_recurring_lifecycle(client):
    acc = _ok(client.post("/api/finance/accounts", json={"name": "Current", "opening_balance": "500"}))
    item = _ok(client.post("/api/finance/recurring", json={
        "name": "Netflix", "account_id": acc["id"], "amount": "10.99", "frequency": "monthly",
        "next_due": "2026-10-20"}))
    assert item["frequency_label"] == "Monthly"
    up = _ok(client.get("/api/finance/upcoming"))
    assert [(r["due_date"], r["status"]) for r in up["items"]] == [("2026-10-20", "overdue"), ("2026-11-20", "expected")]
    assert up["projected_balances"][0]["projected_pence"] == 50000 - 1099
    tx = _ok(client.post("/api/finance/transactions", json={
        "account_id": acc["id"], "date": "2026-10-21", "amount": "10.99", "direction": "out",
        "description": "NETFLIX.COM"}))
    up = _ok(client.get("/api/finance/upcoming"))
    assert up["items"][0]["matches"][0]["id"] == tx["id"]
    _ok(client.post(f"/api/finance/recurring/{item['id']}/match", json={"due_date": "2026-10-20",
                                                                       "transaction_id": tx["id"]}))
    assert [r["due_date"] for r in _ok(client.get("/api/finance/upcoming"))["items"]] == ["2026-11-20"]
    _ok(client.post(f"/api/finance/recurring/{item['id']}/undo", json={"due_date": "2026-10-20"}))
    _ok(client.post(f"/api/finance/recurring/{item['id']}/skip", json={"due_date": "2026-10-20"}))
    assert client.post(f"/api/finance/recurring/{item['id']}/confirm",
                       json={"due_date": "2026-10-20"}).status_code == 409
    assert client.post(f"/api/finance/recurring/{item['id']}/explode", json={"due_date": "2026-10-20"}).status_code == 404
    _ok(client.patch(f"/api/finance/recurring/{item['id']}", json={"amount": "12.99"}))
    assert _ok(client.get("/api/finance/recurring"))["recurring"][0]["amount_pence"] == 1299
    _ok(client.delete(f"/api/finance/recurring/{item['id']}"))
    assert _ok(client.get("/api/finance/recurring"))["recurring"] == []


def test_make_recurring_and_suggestions(client):
    acc = _ok(client.post("/api/finance/accounts", json={"name": "Current"}))
    for day in ("2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01"):
        tx = _ok(client.post("/api/finance/transactions", json={
            "account_id": acc["id"], "date": day, "amount": "850", "direction": "out", "description": "RENT LANDLORD"}))
    suggested = _ok(client.get("/api/finance/recurring/suggestions"))["suggestions"]
    assert suggested[0]["name"] == "RENT LANDLORD" and suggested[0]["next_due"] == "2026-11-01"
    item = _ok(client.post(f"/api/finance/transactions/{tx['id']}/make-recurring", json={"name": "Rent"}))
    assert item["name"] == "Rent" and item["anchor_date"] == "2026-10-01"
    assert _ok(client.get("/api/finance/recurring/suggestions"))["suggestions"] == []


def test_recurring_routes_do_not_shadow_v1_routes(client):
    assert "accounts" in _ok(client.get("/api/finance/accounts"))
    assert "recurring" in _ok(client.get("/api/finance/recurring"))


def test_budget_routes(client, monkeypatch):
    from src.finance import budgets as bud

    monkeypatch.setattr(bud, "_today_for_owner", lambda: date(2026, 10, 27))
    acc = _ok(client.post("/api/finance/accounts", json={"name": "Current"}))
    cats = {c["name"]: c["id"] for c in _ok(client.get("/api/finance/categories"))["categories"]}
    _ok(client.put(f"/api/finance/budgets/{cats['Groceries']}", json={"limit": "300"}))
    _ok(client.post("/api/finance/transactions", json={
        "account_id": acc["id"], "date": "2026-10-03", "amount": "45", "direction": "out",
        "category_id": cats["Groceries"]}))
    status = _ok(client.get("/api/finance/budgets"))
    assert status["month"] == "2026-10" and status["budgets"][0]["remaining_pence"] == 25500
    assert _ok(client.get("/api/finance/budgets?year=2026&month=9"))["budgets"] == []
    assert client.put(f"/api/finance/budgets/{cats['Salary']}", json={"limit": "5"}).status_code == 400
    _ok(client.put(f"/api/finance/budgets/{cats['Groceries']}", json={"limit": None, "month": "2026-11"}))
    assert _ok(client.get("/api/finance/budgets?year=2026&month=11"))["budgets"] == []
