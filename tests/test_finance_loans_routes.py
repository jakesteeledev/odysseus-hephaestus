"""Finance loans API: schedules, payments, links and payment rules on import."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _mortgage(client):
    item = _ok(client.post("/api/finance/liabilities", json={"name": "Home mortgage", "type": "mortgage"}))
    _ok(client.put(f"/api/finance/liabilities/{item['id']}/schedule", json={
        "start_date": "2026-01-01", "start_balance": "150000", "apr": "4.5", "term_months": 300, "payment_day": 1}))
    return item


def test_schedule_and_loan_details(client):
    item = _mortgage(client)
    details = _ok(client.get(f"/api/finance/liabilities/{item['id']}/loan"))
    assert details["balance_pence"] == 15000000
    assert details["schedule"]["apr_percent"] == 4.5 and details["schedule"]["term_months"] == 300
    assert details["projection"]["monthly_payment_pence"] > 0
    listed = _ok(client.get("/api/finance/liabilities"))["liabilities"][0]
    assert listed["has_schedule"] is True and listed["value_pence"] == 15000000
    assert client.delete(f"/api/finance/liabilities/{item['id']}").status_code == 409  # has a schedule
    _ok(client.delete(f"/api/finance/liabilities/{item['id']}/schedule"))
    _ok(client.delete(f"/api/finance/liabilities/{item['id']}"))


def test_student_loan_rate_and_manual_repayment(client):
    slc = _ok(client.post("/api/finance/liabilities", json={"name": "SLC", "type": "student_loan",
                                                             "subtype": "plan 2", "value": "30000"}))
    assert client.put(f"/api/finance/liabilities/{slc['id']}/schedule", json={
        "start_date": "2026-01-01", "start_balance": "1"}).status_code == 400
    assert _ok(client.put(f"/api/finance/liabilities/{slc['id']}/interest-rate",
                          json={"interest_rate": "7.3"}))["interest_rate_percent"] == 7.3
    pay = _ok(client.post(f"/api/finance/liabilities/{slc['id']}/repayments", json={"amount": "150", "note": "Payroll"}))
    assert _ok(client.get(f"/api/finance/liabilities/{slc['id']}/loan"))["payments"][0]["note"] == "Payroll"
    _ok(client.delete(f"/api/finance/repayments/{pay['id']}"))


def test_link_transaction_as_payment(client):
    current = _ok(client.post("/api/finance/accounts", json={"name": "Current", "opening_balance": "5000"}))
    item = _mortgage(client)
    tx = _ok(client.post("/api/finance/transactions", json={
        "account_id": current["id"], "date": "2026-02-01", "amount": "834", "direction": "out",
        "description": "NATIONWIDE MTG"}))
    _ok(client.put(f"/api/finance/transactions/{tx['id']}/payment", json={"liability_id": item["id"]}))
    details = _ok(client.get(f"/api/finance/liabilities/{item['id']}/loan"))
    assert details["payments"][0]["transaction_id"] == tx["id"]
    assert details["payments"][0]["interest_pence"] > 0 and details["balance_pence"] < 15000000
    assert _ok(client.get("/api/finance/transactions"))["transactions"][0]["payment_liability_id"] == item["id"]
    _ok(client.put(f"/api/finance/transactions/{tx['id']}/payment", json={"liability_id": None}))
    assert _ok(client.get(f"/api/finance/liabilities/{item['id']}/loan"))["payments"] == []


def test_payment_rule_links_imported_rows(client):
    current = _ok(client.post("/api/finance/accounts", json={"name": "Current"}))
    item = _mortgage(client)
    cats = {c["name"]: c["id"] for c in _ok(client.get("/api/finance/categories"))["categories"]}
    rule = _ok(client.post("/api/finance/rules", json={
        "pattern": "nationwide mtg", "category_id": cats["Rent/Mortgage"], "liability_id": item["id"]}))
    assert rule["liability_id"] == item["id"]
    csv_text = "Date,Description,Amount\n01/02/2026,NATIONWIDE MTG 123,-834.00\n02/02/2026,Tesco,-12.00\n"
    body = {"csv_text": csv_text, "account_id": current["id"],
            "mapping": {"date_column": "Date", "description_column": "Description", "amount_column": "Amount"}}
    preview = _ok(client.post("/api/finance/import/preview", json=body))
    rows = {r["description"]: r for r in preview["rows"]}
    assert rows["NATIONWIDE MTG 123"]["liability_id"] == item["id"]
    assert rows["Tesco"]["liability_id"] is None
    result = _ok(client.post("/api/finance/import/commit", json=body))
    assert result["batch"]["imported_count"] == 2
    details = _ok(client.get(f"/api/finance/liabilities/{item['id']}/loan"))
    assert len(details["payments"]) == 1 and details["payments"][0]["amount_pence"] == 83400
    # Undoing the import removes the linked payment too.
    _ok(client.post(f"/api/finance/imports/{result['batch']['id']}/undo"))
    assert _ok(client.get(f"/api/finance/liabilities/{item['id']}/loan"))["payments"] == []
    # A rule's liability can be cleared.
    cleared = _ok(client.patch(f"/api/finance/rules/{rule['id']}", json={"liability_id": None}))
    assert cleared["liability_id"] is None


def test_import_override_clears_a_suggested_link(client):
    current = _ok(client.post("/api/finance/accounts", json={"name": "Current"}))
    item = _mortgage(client)
    cats = {c["name"]: c["id"] for c in _ok(client.get("/api/finance/categories"))["categories"]}
    _ok(client.post("/api/finance/rules", json={"pattern": "nationwide", "category_id": cats["Bills"],
                                                "liability_id": item["id"]}))
    body = {"csv_text": "Date,Description,Amount\n01/02/2026,NATIONWIDE INSURANCE,-40.00\n",
            "account_id": current["id"], "liability_overrides": {"2": ""},
            "mapping": {"date_column": "Date", "description_column": "Description", "amount_column": "Amount"}}
    _ok(client.post("/api/finance/import/commit", json=body))
    assert _ok(client.get(f"/api/finance/liabilities/{item['id']}/loan"))["payments"] == []
