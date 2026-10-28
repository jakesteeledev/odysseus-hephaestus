"""Projection and salary API routes (made-up figures)."""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes
from src.finance import payroll_jobs, projections, recurring, service

CURRENT_USER = {"name": "alice"}
TODAY = date(2026, 4, 10)


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: CURRENT_USER["name"])
    for module in (service, projections, payroll_jobs, recurring):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    CURRENT_USER["name"] = "alice"
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_projection_assumptions_and_scenarios(client):
    _ok(client.post("/api/finance/accounts", json={"name": "Current", "opening_balance": "2000"}))
    car = _ok(client.post("/api/finance/assets", json={"name": "Car", "type": "vehicle", "value": "9000"}))
    data = _ok(client.get("/api/finance/insights/projection?years=3"))
    assert data["years"] == 3 and len(data["base"]["years"]) == 3 and data["scenario_list"] == []
    a = _ok(client.patch("/api/finance/projection/assumptions", json={"growth": {"vehicle": "-10"},
                                                                       "spending_override": "500"}))
    assert a["growth_percent"]["vehicle"] == -10 and a["spending_override_pence"] == 50_000
    assert client.patch("/api/finance/projection/assumptions", json={"horizon_years": 99}).status_code == 400
    s = _ok(client.post("/api/finance/projection/scenarios", json={
        "name": "Sell car", "changes": [{"type": "sell", "asset_id": car["id"], "on": "2027-01-01"}]}))
    both = _ok(client.get(f"/api/finance/insights/projection?years=2&scenario={s['id']}"))
    assert both["scenarios"][0]["name"] == "Sell car"
    assert client.post("/api/finance/projection/scenarios", json={"name": "Bad", "changes": []}).status_code == 400
    _ok(client.put(f"/api/finance/projection/scenarios/{s['id']}", json={
        "name": "Sell the car", "changes": [{"type": "sell", "asset_id": car["id"], "on": "2027-06-01"}]}))
    assert _ok(client.get("/api/finance/projection/scenarios"))["scenarios"][0]["name"] == "Sell the car"
    _ok(client.delete(f"/api/finance/projection/scenarios/{s['id']}"))
    # Another user can't see it.
    CURRENT_USER["name"] = "bob"
    assert _ok(client.get("/api/finance/projection/scenarios"))["scenarios"] == []


def test_salary_routes(client):
    account = _ok(client.post("/api/finance/accounts", json={"name": "Current"}))
    job = _ok(client.post("/api/finance/salary/jobs", json={
        "employer": "Acme", "pay_anchor": "2026-04-25", "salary": "30000", "tax_code": "1257L",
        "account_id": account["id"]}))
    assert job["recurring_id"]
    bad = client.post("/api/finance/salary/jobs", json={"employer": "X", "pay_anchor": "2026-04-25",
                                                       "salary": "1", "tax_code": "12X"})
    assert bad.status_code == 400 and "valid UK tax code" in bad.json()["detail"]
    change = _ok(client.post(f"/api/finance/salary/jobs/{job['id']}/changes", json={
        "kind": "raise", "start_date": "2026-10-01", "percent": "5"}))
    overview = _ok(client.get("/api/finance/salary"))
    j = overview["jobs"][0]
    assert j["next_period"]["net_pence"] == 209_364 and j["changes"][0]["id"] == change["id"]
    slip = _ok(client.post(f"/api/finance/salary/jobs/{job['id']}/payslips", json={
        "pay_date": "2026-04-25", "gross": "2500", "tax": "290.20", "ni": "116.16", "net": "2093.64"}))
    assert slip["check"]["matches"]
    assert _ok(client.get(f"/api/finance/salary/payslips/{slip['payslip']['id']}/check"))["matches"]
    upcoming = _ok(client.get("/api/finance/upcoming?days=30"))
    pay = next(r for r in upcoming["items"] if r["recurring_id"] == job["recurring_id"])
    assert pay["calculated"] and pay["amount_pence"] == 209_364
    edit = client.patch(f"/api/finance/recurring/{job['recurring_id']}", json={"amount": "5"})
    assert edit.status_code == 400
    _ok(client.patch(f"/api/finance/salary/jobs/{job['id']}", json={"salary": "33000"}))
    _ok(client.delete(f"/api/finance/salary/changes/{change['id']}"))
    _ok(client.delete(f"/api/finance/salary/payslips/{slip['payslip']['id']}"))
    _ok(client.delete(f"/api/finance/salary/jobs/{job['id']}"))
    assert _ok(client.get("/api/finance/salary"))["jobs"] == []
