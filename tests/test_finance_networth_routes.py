"""Finance v2 API: net worth, assets, liabilities, valuations, contributions."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes

CURRENT_USER = {"name": "alice"}


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: CURRENT_USER["name"])
    CURRENT_USER["name"] = "alice"
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_items_valuations_and_net_worth(client):
    _ok(client.post("/api/finance/accounts", json={"name": "Current", "opening_balance": "1000"}))
    house = _ok(client.post("/api/finance/assets", json={"name": "Home", "type": "property",
                                                          "value": "250000", "value_date": "2026-01-01"}))
    mortgage = _ok(client.post("/api/finance/liabilities", json={
        "name": "Nationwide mortgage", "type": "mortgage", "lender": "Nationwide",
        "linked_asset_id": house["id"], "value": "150000", "value_date": "2026-01-01"}))
    assert mortgage["lender"] == "Nationwide" and mortgage["linked_asset_id"] == house["id"]
    _ok(client.post(f"/api/finance/liabilities/{mortgage['id']}/valuations",
                    json={"value": "149000", "date": "2026-02-01", "note": "Statement"}))
    worth = _ok(client.get("/api/finance/networth"))
    assert worth["net_worth_pence"] == 100000 + 25000000 - 14900000
    groups = {g["type"]: g for g in worth["assets"]}
    assert groups["property"]["items"][0]["equity_pence"] == 25000000 - 14900000
    past = _ok(client.get("/api/finance/networth?as_of=2026-01-15"))
    assert past["liabilities_total_pence"] == 15000000

    history = _ok(client.get(f"/api/finance/liabilities/{mortgage['id']}/valuations"))["valuations"]
    assert [v["value_pence"] for v in history] == [14900000, 15000000]
    _ok(client.delete(f"/api/finance/valuations/{history[0]['id']}"))
    assert _ok(client.get("/api/finance/liabilities"))["liabilities"][0]["value_pence"] == 15000000


def test_archive_delete_and_validation(client):
    asset = _ok(client.post("/api/finance/assets", json={"name": "Watch", "type": "other", "value": "900"}))
    assert client.delete(f"/api/finance/assets/{asset['id']}").status_code == 409
    _ok(client.patch(f"/api/finance/assets/{asset['id']}", json={"archived": True}))
    assert _ok(client.get("/api/finance/assets"))["assets"] == []
    assert len(_ok(client.get("/api/finance/assets?include_archived=true"))["assets"]) == 1
    empty = _ok(client.post("/api/finance/liabilities", json={"name": "IOU Sam", "type": "other"}))
    _ok(client.delete(f"/api/finance/liabilities/{empty['id']}"))
    assert client.post("/api/finance/assets", json={"name": "Boat", "type": "yacht"}).status_code == 400
    assert client.get("/api/finance/widgets").status_code == 404


def test_contributions_and_transaction_links(client):
    current = _ok(client.post("/api/finance/accounts", json={"name": "Current", "opening_balance": "3000"}))
    isa = _ok(client.post("/api/finance/assets", json={"name": "Vanguard ISA", "type": "investment",
                                                        "subtype": "isa", "value": "5200"}))
    tx = _ok(client.post("/api/finance/transactions", json={
        "account_id": current["id"], "date": "2026-10-01", "amount": "500", "direction": "out",
        "description": "ISA top-up"}))
    _ok(client.put(f"/api/finance/transactions/{tx['id']}/contribution", json={"asset_id": isa["id"]}))
    _ok(client.post(f"/api/finance/assets/{isa['id']}/contributions", json={"amount": "4000", "date": "2026-04-06"}))
    item = _ok(client.get("/api/finance/assets"))["assets"][0]
    assert item["contributions_pence"] == 450000 and item["gain_pence"] == 520000 - 450000
    listed = _ok(client.get("/api/finance/transactions"))["transactions"][0]
    assert listed["contribution_asset_id"] == isa["id"]
    _ok(client.put(f"/api/finance/transactions/{tx['id']}/contribution", json={"asset_id": None}))
    assert _ok(client.get(f"/api/finance/assets/{isa['id']}/contributions"))["contributions"][0]["amount_pence"] == 400000


def test_owner_isolation(client):
    asset = _ok(client.post("/api/finance/assets", json={"name": "Home", "type": "property", "value": "1"}))
    CURRENT_USER["name"] = "bob"
    assert _ok(client.get("/api/finance/networth"))["net_worth_pence"] == 0
    assert client.patch(f"/api/finance/assets/{asset['id']}", json={"name": "x"}).status_code == 404
    assert client.post(f"/api/finance/assets/{asset['id']}/valuations", json={"value": "5"}).status_code == 404


def test_v1_routes_still_win_over_generic_collection_routes(client):
    assert "accounts" in _ok(client.get("/api/finance/accounts"))
    assert "categories" in _ok(client.get("/api/finance/categories"))
    assert "rules" in _ok(client.get("/api/finance/rules"))
